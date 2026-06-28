"""Supervised launcher: run an agent, auto-switch on usage limit, resume the same work.

Design (testability): the *decisions* (limit detection, what to do on a limit) are pure functions;
the messy PTY I/O is isolated behind an injectable ``spawn`` callable so ``run()`` is unit-tested
with a scripted fake child (no real PTY, no network).

Flow:
  1. pick a seat (prefer active; else available; else soonest-unlock + report) and switch to it
  2. spawn the agent under a PTY, teeing output while scanning for the limit signal
  3. on a mid-session limit: flag the seat (reactive), pick another seat, and relaunch with the
     tool's RESUME command so the conversation continues; repeat
  4. on normal exit: sync-back the (refreshed) creds and return the child's exit status
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from . import headroom as headroom_mod
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError
from .selection import choose
from .switch import switch, sync_back
from .util import iso, now

# A spawn function: (argv, on_output) -> exit_status.
#   on_output(chunk: bytes) -> bool ; returning True asks the supervisor to stop the child.
SpawnFn = Callable[[list, Callable[[bytes], bool]], int]
Notifier = Callable[[str], None]

# Default cooldown when a limit is caught but no authoritative reset is known.
DEFAULT_COOLDOWN = timedelta(hours=5)
MAX_SWITCHES = 6      # safety bound on auto-relaunches within one `run`
EXIT_GAVE_UP = 75     # EX_TEMPFAIL: distinguishes "we gave up / all limited" from a child failure

# Limit signals in the agents' output. Deliberately SPECIFIC: a false positive kills+restarts the
# session, so weak/ambiguous phrases (e.g. "try again", "resets at", "approaching … limit") are
# intentionally excluded. Confirmed/extended against real strings during verification (M8).
LIMIT_PATTERNS = {
    "codex": [
        r"usage limit", r"you[''`]?ve hit your (?:usage )?limit", r"limit reached",
        r"rate[ -]?limit(?:ed| reached| exceeded)", r"out of (?:credits?|usage)",
        r"too many requests",
    ],
    "claude": [
        r"usage limit", r"5-?hour limit", r"weekly limit", r"limit reached",
        r"rate[ -]?limit(?:ed| reached| exceeded)", r"out of (?:credits?|usage)",
        r"too many requests",
    ],
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _compiled(tool: str) -> list:
    return [re.compile(p, re.IGNORECASE) for p in LIMIT_PATTERNS[tool]]


def detect_limit(tool: str, text: str) -> bool:
    """True if ``text`` (a rolling buffer of recent output) looks like a usage-limit message.

    ANSI escape codes are stripped first so a TUI's color codes can't split a phrase.
    """
    return any(rx.search(_ANSI.sub("", text)) for rx in _compiled(tool))


class NoSeats(AcctswError):
    """No seats configured for a tool."""


# --- commands ---------------------------------------------------------------------------------

def build_cmd(ctx: Context, tool: str, args: list) -> list:
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    return [exe, *args]


def resume_cmd(ctx: Context, tool: str) -> list:
    """Resume the most recent session so the work continues after a swap."""
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    if tool == "codex":
        return [exe, "resume", "--last"]
    return [exe, "--continue"]


# --- decision logic (pure-ish; persists state) ------------------------------------------------

@dataclass
class Decision:
    action: str          # "switch" | "give_up"
    email: str | None
    unlocks_at: str | None = None


def handle_limit(ctx: Context, state, tool: str, *, get=usage_mod._default_get) -> Decision:
    """A limit was caught for the active seat. Flag it, then choose the next seat."""
    active = state.active(tool)
    # Authoritative reset from the usage endpoint for the seat that just hit the limit (only the
    # active seat — others keep their known state; their stale snapshot tokens would 401 anyway).
    if active:
        usage_mod.refresh(ctx, state, tool, only=active, force=True, get=get)
    seat = state.get_seat(tool, active) if active else None
    # ...else a reactive fallback so we don't immediately re-pick the maxed seat.
    if seat is not None and seat.get("limited_until") is None:
        state.set_limited_until(tool, active, iso(now() + DEFAULT_COOLDOWN), source="reactive")
    state.save()

    sel = choose(state, tool)
    if sel.email and sel.available and sel.email != active:
        return Decision("switch", sel.email)
    return Decision("give_up", sel.email,
                    sel.unlocks_at.isoformat() if sel.unlocks_at else None)


# --- real PTY supervisor ----------------------------------------------------------------------

def _real_fd(stream) -> int | None:
    """Return a stream's OS fd, or None if it has none (e.g. captured/replaced under tests)."""
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


def _set_winsize(master_fd: int, out_fd: int) -> None:
    try:
        sz = fcntl.ioctl(out_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, sz)
    except Exception:
        pass


def _exitcode(raw_status: int) -> int:
    return (os.waitstatus_to_exitcode(raw_status)
            if hasattr(os, "waitstatus_to_exitcode") else raw_status)


def pty_spawn(argv: list, on_output: Callable[[bytes], bool]) -> int:
    """Run ``argv`` in a PTY, copying I/O to the real terminal and teeing output to ``on_output``.

    If ``on_output`` returns True, the child is terminated (SIGTERM→SIGKILL) so the caller can
    relaunch. Returns the child's exit status. The child is reaped exactly once.
    """
    # Resolve real fds up front; under test capture / non-tty these may be missing — guard them
    # so we never pass an object with a raising fileno() into select() (which would busy-loop).
    stdin_fd = _real_fd(sys.stdin)
    out_fd = _real_fd(sys.stdout)
    if out_fd is None:
        out_fd = 1
    stdin_is_tty = stdin_fd is not None and os.isatty(stdin_fd)

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
        os._exit(127)

    stop_requested = False
    old_attrs = None
    prev_winch = None
    watch = [master_fd] + ([stdin_fd] if stdin_fd is not None else [])
    try:
        if stdin_is_tty:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        _set_winsize(master_fd, out_fd)

        def _winch(_sig, _frm):
            _set_winsize(master_fd, out_fd)
        try:
            prev_winch = signal.signal(signal.SIGWINCH, _winch)
        except (ValueError, OSError):
            prev_winch = None

        while True:
            try:
                rlist, _, _ = select.select(watch, [], [])
            except InterruptedError:
                continue
            except OSError:
                break  # an fd went bad — stop the copy loop and reap
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # child closed the pty → exited
                os.write(out_fd, data)
                if on_output(data):
                    stop_requested = True
                    break
            if stdin_fd is not None and stdin_fd in rlist:
                try:
                    inp = os.read(stdin_fd, 65536)
                except OSError:
                    inp = b""
                if inp:
                    os.write(master_fd, inp)
                else:
                    watch.remove(stdin_fd)  # stdin EOF → stop watching (avoid busy-loop)
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attrs)
        if prev_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, prev_winch)
            except (ValueError, OSError):
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    # Reap exactly once. On the stop path, _terminate kills AND reaps and returns the status.
    if stop_requested:
        return _terminate(pid)
    try:
        _, status = os.waitpid(pid, 0)
        return _exitcode(status)
    except ChildProcessError:
        return 0


def _terminate(pid: int) -> int:
    """Stop the child (SIGTERM→SIGKILL) and reap it. Returns its exit/signal status.

    We signal the child's whole PROCESS GROUP: ``pty.fork`` makes the child a session leader, so
    its children (e.g. a shell's subprocesses) share its pgid and must be killed too — otherwise
    an orphan keeps the pty open and we'd hang.
    """
    def _signal(sig):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal(sig)
        for _ in range(20):
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return -signal.SIGKILL  # already reaped elsewhere
            if wpid == pid:
                return _exitcode(status)
            time.sleep(0.05)
    try:
        _, status = os.waitpid(pid, 0)
        return _exitcode(status)
    except ChildProcessError:
        return -signal.SIGKILL


# --- orchestration ----------------------------------------------------------------------------

def _noop(_msg: str) -> None:
    pass


def run(ctx: Context, tool: str, args: list, *, spawn: SpawnFn = pty_spawn,
        notify: Notifier = _noop, get=usage_mod._default_get,
        max_switches: int = MAX_SWITCHES) -> int:
    """Launch ``tool`` with the best seat, auto-switching + resuming on limits. Returns exit code."""
    state = ctx.load_state()
    if not state.accounts(tool):
        raise NoSeats(f"no {tool} seats yet — add one first")

    # Recover from any prior wrapped session that was killed mid-flight before it could restore the
    # tool config (otherwise stale Headroom injection would linger / become permanent).
    headroom_mod.recover_stale(ctx.data_dir / "headroom-backup")

    save_credit = bool(state.settings().get("headroom"))
    hr_exe = headroom_mod.headroom_path() if save_credit else None
    if save_credit and not hr_exe:
        notify("save-credit is on but headroom isn't installed — running without it")
        save_credit = False
    elif save_credit:
        # ensure the spawned child can resolve `headroom` even if the venv bin isn't on PATH
        os.environ["PATH"] = headroom_mod.venv_bin_dir() + os.pathsep + os.environ.get("PATH", "")

    # Headroom's `wrap` injects (persistently) into the tool's config; scope it to this session so
    # the user's setup is restored exactly afterwards (non-destructive).
    hr_scope = headroom_mod.scoped(tool, ctx.data_dir / "headroom-backup") if save_credit else None
    if hr_scope:
        hr_scope.__enter__()

    try:
        # Initial selection + switch, under the state lock (brief; never held across a spawn).
        with ctx.locked():
            state = ctx.load_state()
            sel = choose(state, tool)
            if sel.email and sel.email != state.active(tool):
                switch(ctx, state, tool, sel.email)
        if sel.all_limited and sel.unlocks_at:
            notify(f"all {tool} seats are resting — {sel.email} unlocks at "
                   f"{sel.unlocks_at:%H:%M}; starting anyway")

        switches = 0
        resuming = False
        buf = bytearray()
        while True:
            base = resume_cmd(ctx, tool) if resuming else build_cmd(ctx, tool, args)
            # headroom launches the tool itself via its per-tool subcommand
            # (headroom wrap codex -- resume --last); base[1:] drops our tool binary.
            argv = headroom_mod.wrap(tool, base[1:], enabled=save_credit,
                                     is_available=save_credit, exe=hr_exe or "headroom") or base
            hit = {"v": False}
            buf.clear()

            def on_output(chunk: bytes) -> bool:
                buf.extend(chunk)
                del buf[:-4096]  # keep a rolling tail
                if detect_limit(tool, buf.decode("utf-8", "replace")):
                    hit["v"] = True
                    return True
                return False

            status = spawn(argv, on_output)  # NO lock held during the session

            if not hit["v"]:
                return status  # clean exit — child's real exit code

            if switches >= max_switches:
                notify(f"hit the switch limit ({max_switches}); stopping")
                return EXIT_GAVE_UP

            with ctx.locked():
                state = ctx.load_state()
                active = state.active(tool)
                dec = handle_limit(ctx, state, tool, get=get)
                if dec.action == "switch":
                    switch(ctx, state, tool, dec.email)
                    state.data["last_switch_at"] = iso(now())
                    state.save()
            if dec.action == "switch":
                notify(f"{active} needs a rest 💤 — hopping to {dec.email}, "
                       f"your work's coming with you ✨")
                switches += 1
                resuming = True
                continue
            notify(f"all {tool} seats are resting"
                   + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
            return EXIT_GAVE_UP
    finally:
        # Mandatory sync-back on EVERY exit path (incl. exceptions): the just-run seat may carry a
        # rotated refresh token. The Codex live-vs-active guard prevents clobbering on a half-swap.
        try:
            with ctx.locked():
                st = ctx.load_state()
                if sync_back(ctx, st, tool):
                    st.save()
        except Exception:
            pass
        # restore any Headroom config injection → user's setup byte-identical to before
        if hr_scope:
            hr_scope.__exit__(None, None, None)
