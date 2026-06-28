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
MAX_SWITCHES = 6  # safety bound on auto-relaunches within one `run`

# Limit signals in the agents' output. Conservative; tuned/confirmed during verification (M8).
LIMIT_PATTERNS = {
    "codex": [
        r"usage limit", r"rate.?limit", r"you[''`]?ve hit your", r"limit reached",
        r"try again (?:in|at)", r"out of (?:credit|usage)", r"429",
    ],
    "claude": [
        r"usage limit", r"rate.?limit", r"5-?hour limit", r"weekly limit",
        r"limit reached", r"approaching .* limit", r"resets? (?:at|in)", r"429",
    ],
}


def _compiled(tool: str) -> list:
    return [re.compile(p, re.IGNORECASE) for p in LIMIT_PATTERNS[tool]]


def detect_limit(tool: str, text: str) -> bool:
    """True if ``text`` (a rolling buffer of recent output) looks like a usage-limit message."""
    return any(rx.search(text) for rx in _compiled(tool))


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

def _set_winsize(fd: int) -> None:
    try:
        sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
    except Exception:
        pass


def pty_spawn(argv: list, on_output: Callable[[bytes], bool]) -> int:
    """Run ``argv`` in a PTY, copying I/O to the real terminal and teeing output to ``on_output``.

    If ``on_output`` returns True, the child is terminated (SIGTERM→SIGKILL) so the caller can
    relaunch. Returns the child's exit status (or the signal-based status).
    """
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
        os._exit(127)

    stop_requested = False
    old_attrs = None
    try:
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())
        _set_winsize(master_fd)

        def _winch(_sig, _frm):
            _set_winsize(master_fd)
        try:
            signal.signal(signal.SIGWINCH, _winch)
        except ValueError:
            pass

        while True:
            try:
                rlist, _, _ = select.select([master_fd, sys.stdin], [], [])
            except (InterruptedError, OSError):
                continue
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                if on_output(data):
                    stop_requested = True
                    break
            if sys.stdin in rlist:
                try:
                    inp = os.read(sys.stdin.fileno(), 65536)
                except OSError:
                    inp = b""
                if inp:
                    os.write(master_fd, inp)
    finally:
        if old_attrs is not None:
            termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_attrs)

    if stop_requested:
        _terminate(pid)
    _, status = os.waitpid(pid, 0)
    try:
        os.close(master_fd)
    except OSError:
        pass
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status


def __pty_fork():
    import pty
    return pty.fork()


def _terminate(pid: int) -> None:
    import time
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    return
            except ChildProcessError:
                return
            time.sleep(0.05)


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
        argv = resume_cmd(ctx, tool) if resuming else build_cmd(ctx, tool, args)
        hit = {"v": False}
        buf.clear()

        def on_output(chunk: bytes) -> bool:
            buf.extend(chunk)
            del buf[:-4096]  # keep a rolling tail
            if detect_limit(tool, buf.decode("utf-8", "replace")):
                hit["v"] = True
                return True
            return False

        status = spawn(argv, on_output)

        if not hit["v"]:
            sync_back(ctx, state, tool)
            state.save()
            return status

        if switches >= max_switches:
            notify(f"hit the switch limit ({max_switches}); stopping")
            sync_back(ctx, state, tool)
            state.save()
            return status

        active = state.active(tool)
        dec = handle_limit(ctx, state, tool, get=get)
        if dec.action == "switch":
            notify(f"{active} needs a rest 💤 — hopping to {dec.email}, your work's coming with you ✨")
            switch(ctx, state, tool, dec.email)
            switches += 1
            resuming = True
            continue
        notify(f"all {tool} seats are resting"
               + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
        sync_back(ctx, state, tool)
        state.save()
        return status
