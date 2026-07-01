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
from .headroom import harden_env
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
MAX_FALSE_ALARMS = 3  # stdout said "limit" but usage disagreed this many times → stop supervising
# A stdout limit-signal is dismissed as a false positive (model prose, not a real banner) when the
# usage endpoint says the active seat's busiest window is still below this. Well clear of a real
# ~100% limit even if the endpoint lags a few percent behind stdout.
FALSE_ALARM_MAX_PCT = 90.0
EXIT_GAVE_UP = 75     # EX_TEMPFAIL: distinguishes "we gave up / all limited" from a child failure

# Limit signals in the agents' output. This buffer ALSO carries the model's own generated prose —
# which, especially when THIS repo is the thing under development, routinely mentions "usage limit"
# and "out of credits" in passing. A false positive kills+restarts a healthy session, so every phrase
# here must be the tool's actual out-of-quota BANNER (committal wording), never a word the model can
# utter mid-sentence. Bare "usage limit" / "limit reached" / "out of credits" are intentionally NOT
# here for exactly this reason. A missed real limit is corroborated separately by the usage endpoint
# (handle_limit), so erring toward specificity here is safe.
_LIMIT_SHARED = [
    r"usage limit (?:reached|exceeded)",
    r"you[''`]?ve (?:hit|reached) your (?:usage )?limit",
    r"your limit will reset",
    r"rate[ -]?limit(?:ed| reached| exceeded)",
    r"too many requests",
]
LIMIT_PATTERNS = {
    # "out of credits" is real ChatGPT/Codex wording, but it's also exactly what the model narrates
    # about Codex — so it stays out of the CLAUDE list (a Claude session never emits it as a banner).
    "codex": [*_LIMIT_SHARED, r"out of (?:credits?|usage)"],
    # "5-hour limit" / "weekly limit" are Claude's OWN window-limit banners (e.g. a status line
    # "5-hour limit · resets 8pm" that omits "reached"). They're specific enough to rarely appear in
    # the model's prose, and the corroboration guard (handle_limit) vetoes any that slip through — so
    # we keep them loose here to catch the real banner regardless of its exact committal wording.
    "claude": [*_LIMIT_SHARED, r"5-?hour limit", r"weekly limit"],
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Auth-death signals (token revoked / signed out). DISTINCT from a usage limit: switching+resuming on
# the SAME seat can't help — the seat needs re-login — so we hop to a DIFFERENT seat. Kept to the
# tools' SPECIFIC error wording (not loose phrases like "sign in again") because this buffer also
# carries the model's own generated text; a loose match would kill a healthy session on benign output.
# Shared base + per-tool extras (one source of truth, so the common patterns can't drift apart).
_AUTH_DEAD_SHARED = [r"refresh token (?:was |is )?revoked", r"log ?out and sign in again"]
AUTH_DEAD_PATTERNS = {
    "codex": _AUTH_DEAD_SHARED,
    "claude": [*_AUTH_DEAD_SHARED, r"oauth token (?:has )?expired"],
}

# Compile once at import — detect_* runs once per PTY output chunk (a hot interactive path).
_LIMIT_RE = {t: [re.compile(p, re.IGNORECASE) for p in pats] for t, pats in LIMIT_PATTERNS.items()}
_AUTH_RE = {t: [re.compile(p, re.IGNORECASE) for p in pats] for t, pats in AUTH_DEAD_PATTERNS.items()}


def detect_limit(tool: str, text: str) -> bool:
    """True if ``text`` (a rolling buffer of recent output) looks like a usage-limit message.

    ANSI escape codes are stripped first so a TUI's color codes can't split a phrase.
    """
    clean = _ANSI.sub("", text)
    return any(rx.search(clean) for rx in _LIMIT_RE[tool])


def detect_auth_dead(tool: str, text: str) -> bool:
    """True if recent output says the seat's credentials are dead (revoked / signed out)."""
    clean = _ANSI.sub("", text)
    return any(rx.search(clean) for rx in _AUTH_RE[tool])


def detect_event(tool: str, text: str) -> str | None:
    """Classify a PTY output chunk in ONE ANSI strip (the hot path runs this per chunk): returns
    "auth" (creds dead), "limit" (usage limit), or None. Auth is checked first — it's not a usage
    limit and needs a DIFFERENT seat, not a resume on the same one."""
    clean = _ANSI.sub("", text)
    if any(rx.search(clean) for rx in _AUTH_RE[tool]):
        return "auth"
    if any(rx.search(clean) for rx in _LIMIT_RE[tool]):
        return "limit"
    return None


class NoSeats(AcctswError):
    """No seats configured for a tool."""


# --- commands ---------------------------------------------------------------------------------

def build_cmd(ctx: Context, tool: str, args: list) -> list:
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    return [exe, *args]


def exec_stock(ctx: Context, tool: str, args: list) -> int:
    """Replace this process with the STOCK tool — no supervision, no auto-switch.

    Used when the menubar app is closed: terminal ``codex``/``claude`` must then behave exactly
    like the real tool (against whatever account is currently in ~/.codex / the keychain). We
    ``execvp`` rather than spawn so the tool fully owns the terminal (TTY, signals, exit code).
    Returns 127 only if exec fails (binary missing); on success it never returns.
    """
    argv = build_cmd(ctx, tool, args)
    try:
        os.execvpe(argv[0], argv, harden_env())
    except OSError:
        return 127
    return 127


def resume_cmd(ctx: Context, tool: str) -> list:
    """Resume the most recent session so the work continues after a swap."""
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    if tool == "codex":
        return [exe, "resume", "--last"]
    return [exe, "--continue"]


# --- decision logic (pure-ish; persists state) ------------------------------------------------

@dataclass
class Decision:
    action: str          # "switch" | "resume" | "give_up"
    email: str | None
    unlocks_at: str | None = None


def _seat_confirmed_healthy(state, tool: str, email: str, summary: dict) -> bool:
    """True only when a FRESH usage fetch confirmed this seat has clear headroom — so a stdout
    limit-signal can be safely dismissed as a false positive (the model narrating about limits rather
    than a real banner). Unknown/errored/absent usage → False: we can't confirm, so we fall back to
    trusting the stdout signal exactly as before."""
    if (summary.get(tool) or {}).get(email) != "ok":
        return False  # cached / unauthorized / rate_limited / network / no_creds → can't tell
    u = (state.get_seat(tool, email) or {}).get("usage") or {}
    if u.get("limit_reached"):
        return False  # authoritative API flag says the seat really is out
    pcts = [w.get("used_pct") for w in (u.get("windows") or {}).values()
            if isinstance(w, dict) and w.get("used_pct") is not None]
    return bool(pcts) and max(pcts) < FALSE_ALARM_MAX_PCT


def handle_limit(ctx: Context, state, tool: str, *, get=usage_mod._default_get,
                 exclude: set | frozenset = frozenset()) -> Decision:
    """A limit was caught for the active seat. Flag it, then choose the next seat. ``exclude`` carries
    seats that already failed auth this run, so a limit never re-selects a known-dead-token seat."""
    active = state.active(tool)
    # Authoritative reset from the usage endpoint for the seat that just hit the limit (only the
    # active seat — others keep their known state; their stale snapshot tokens would 401 anyway).
    summary: dict = {}
    if active:
        summary = usage_mod.refresh(ctx, state, tool, only=active, force=True, get=get)
    # Corroboration guard: the stdout match can be a false positive (the model discussing limits —
    # routine when THIS repo is under development). If the endpoint FRESHLY confirms the active seat
    # still has clear headroom, don't rest a healthy seat — resume the same work instead.
    if active and _seat_confirmed_healthy(state, tool, active, summary):
        return Decision("resume", active)
    seat = state.get_seat(tool, active) if active else None
    # ...else a reactive fallback so we don't immediately re-pick the maxed seat.
    if seat is not None and seat.get("limited_until") is None:
        state.set_limited_until(tool, active, iso(now() + DEFAULT_COOLDOWN), source="reactive")
    state.save()

    sel = choose(state, tool, exclude=exclude)
    if sel.email and sel.available and sel.email != active:
        return Decision("switch", sel.email)
    return Decision("give_up", sel.email,
                    sel.unlocks_at.isoformat() if sel.unlocks_at else None)


def handle_auth_dead(ctx: Context, state, tool: str, *, exclude: set | frozenset = frozenset()) -> Decision:
    """The active seat's credentials are dead (revoked/signed out) for THIS run. Choose a DIFFERENT
    seat, skipping the active one plus any that already failed auth this session (``exclude``).

    We deliberately do NOT persist a "dead" flag on the seat: a usage-poll ``unauthorized`` is not a
    reliable health signal (a non-active seat shows it from a stale cached access token), and a benign
    output match shouldn't disable a seat beyond the current run. Re-login is detected fresh next time.
    """
    active = state.active(tool)
    skip = set(exclude) | ({active} if active else set())
    sel = choose(state, tool, exclude=skip)
    if sel.email and sel.available:
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


# Terminal private modes a TUI child (claude/codex) turns on but cannot reset when we KILL it:
# on a limit/auth hop the child gets SIGTERM→SIGKILL and never runs its own cleanup, so the shell
# that inherits the terminal is left in mouse-reporting mode and spews coordinates (e.g. the
# "\e[<35;86;2M" garbage seen at the prompt after a session ends). We disable mouse tracking and
# bracketed paste and re-show the cursor. Alt-screen is deliberately left alone so an inline
# session's visible output stays in the scrollback.
_TERM_RESET = (
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"  # all mouse-tracking modes off
    b"\x1b[?2004l"  # bracketed paste off
    b"\x1b[?25h"    # cursor visible
)


def _reset_terminal(out_fd: int) -> None:
    """Undo the terminal private modes a TUI leaves set. No-op unless out_fd is a real terminal."""
    try:
        if os.isatty(out_fd):
            os.write(out_fd, _TERM_RESET)
    except OSError:
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
        os.execvpe(argv[0], argv, harden_env())
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
        # Re-assert the terminal's default private modes the child TUI may have left set (mouse
        # tracking especially) — on the kill path the child never got to do this itself.
        _reset_terminal(out_fd)
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

    # Headroom is GLOBAL/app-managed now (the app toggle starts our own proxy + writes provider
    # routing, so plain codex, the GUI, AND cx all route through the proxy). cx/cl therefore do NOT
    # per-session-wrap — global mode owns Headroom — so the launcher just runs the tool plain.
    # Self-heal: if a force-quit/crash left routing injected but the proxy dead, reconcile() strips
    # the dangling injection (and clears the setting) so the tool runs plain instead of hitting a
    # dead proxy. A cheap, subprocess-free pre-check (needs_reconcile) keeps this off the hot path
    # when save-credit was never used; reconcile()'s restore backstop works even if the headroom
    # binary is gone, so it is NOT gated on headroom_path().
    try:
        from . import headroom as _hr
        if _hr.needs_reconcile(ctx):
            # blocking=False: if the GUI is mid-enable holding the lock (starting the proxy + waiting
            # on /readyz can take ~30s), skip self-heal rather than hang the launch — the GUI owns it.
            changed, _ = _hr.reconcile(ctx, blocking=False)
            if changed:
                notify("Headroom's proxy wasn't running — removed its routing so this runs directly. "
                       "Open the ai guest list app to turn save-credit back on.")
            elif state.settings().get("headroom") and not _hr.available():
                # setting persisted on but Headroom is gone (venv rebuilt / uninstalled): tell the
                # user they're NOT saving tokens rather than silently running plain.
                notify("save-credit is on but Headroom isn't installed — running without it. "
                       "Reinstall with the ai guest list app, or turn save-credit off.")
    except Exception:
        pass

    def _activate_codex_home(email):
        """Point codex at the account's own home so it maintains that account's tokens in place."""
        if tool == "codex" and email:
            from . import codexhome
            codexhome.ensure_home(email, codex_home=ctx._codex_real, root=ctx._homes_root)
            os.environ["CODEX_HOME"] = str(ctx.codex_home(email))

    try:
        # Initial selection + switch, under the state lock (brief; never held across a spawn).
        with ctx.locked():
            state = ctx.load_state()
            if tool == "codex":
                from . import accounts as _acct
                _acct.reconcile_codex(ctx, state)   # freshen home(s) from ~/.codex before using them
            sel = choose(state, tool)
            if sel.email and sel.email != state.active(tool):
                switch(ctx, state, tool, sel.email, sync=(tool != "codex"))
            _activate_codex_home(state.active(tool))
        if sel.all_limited and sel.unlocks_at:
            notify(f"all {tool} seats are resting — {sel.email} unlocks at "
                   f"{sel.unlocks_at:%H:%M}; starting anyway")

        switches = 0
        false_alarms = 0           # stdout said "limit" but usage disagreed — bounded to avoid a loop
        resuming = False
        auth_failed: set = set()   # seats whose token died THIS run — skip them for the rest of it
        buf = bytearray()
        while True:
            # Headroom is applied globally (app toggle), not per-session, so we run the tool plain.
            argv = resume_cmd(ctx, tool) if resuming else build_cmd(ctx, tool, args)
            hit = {"reason": None}  # None | "limit" | "auth"
            buf.clear()

            def on_output(chunk: bytes) -> bool:
                buf.extend(chunk)
                del buf[:-4096]  # keep a rolling tail
                reason = detect_event(tool, buf.decode("utf-8", "replace"))  # one ANSI strip per chunk
                if reason is not None:
                    hit["reason"] = reason
                    return True
                return False

            status = spawn(argv, on_output)  # NO lock held during the session

            if hit["reason"] is None:
                return status  # clean exit — child's real exit code

            with ctx.locked():
                state = ctx.load_state()
                active = state.active(tool)
                if hit["reason"] == "auth":
                    if active:
                        auth_failed.add(active)
                    dec = handle_auth_dead(ctx, state, tool, exclude=auth_failed)
                else:
                    dec = handle_limit(ctx, state, tool, get=get, exclude=auth_failed)
                # The switch cap gates only an actual seat hop. Classify FIRST: a false-alarm
                # "resume" (usage says the active seat is healthy) must not be terminated just
                # because earlier genuine switches used up the budget — that would kill a healthy
                # session, the very bug this supervisor is meant to avoid.
                hop_capped = dec.action == "switch" and switches >= max_switches
                if dec.action == "switch" and not hop_capped:
                    switch(ctx, state, tool, dec.email, sync=(tool != "codex"))
                    _activate_codex_home(dec.email)
                    state.data["last_switch_at"] = iso(now())
                    state.save()
            if hop_capped:
                notify(f"hit the switch limit ({max_switches}); stopping")
                return EXIT_GAVE_UP
            if dec.action == "switch":
                reason_msg = ("needs you to sign in again 🔑" if hit["reason"] == "auth"
                              else "needs a rest 💤")
                notify(f"{active} {reason_msg} — hopping to {dec.email}, "
                       f"your work's coming with you ✨")
                switches += 1
                resuming = True
                continue
            if dec.action == "resume":
                # False alarm: usage confirms the active seat is healthy, so the stdout match was the
                # model's own prose, not a real limit. Resume the SAME seat and carry the work on.
                false_alarms += 1
                if false_alarms > MAX_FALSE_ALARMS:
                    notify(f"{active} kept looking limited but usage says it's fine — stopping "
                           f"supervision so the session doesn't loop")
                    return EXIT_GAVE_UP
                resuming = True
                continue
            if hit["reason"] == "auth":
                if dec.unlocks_at:
                    notify(f"{active} needs you to sign in again 🔑 — the only other {tool} seat is "
                           f"resting until {dec.unlocks_at}")
                else:
                    notify(f"{active} needs you to sign in again (token revoked) and no other "
                           f"{tool} seat is ready — re-add it via the app or `acctsw add {tool}`")
                return EXIT_GAVE_UP
            notify(f"all {tool} seats are resting"
                   + (f"; soonest unlocks at {dec.unlocks_at}" if dec.unlocks_at else ""))
            return EXIT_GAVE_UP
    finally:
        # On exit, reconcile the active account's creds (the just-run seat may carry a rotated token).
        #  - codex: it maintained its own home via CODEX_HOME → mirror the home into ~/.codex so
        #    plain codex / the GUI follow the active account.
        #  - claude: sync the live keychain item back into the account's snapshot.
        try:
            with ctx.locked():
                st = ctx.load_state()
                active = st.active(tool)
                if tool == "codex" and active:
                    blob = ctx.snapshot_get("codex", active)   # home = source of truth
                    if blob:
                        ctx.cred["codex"].set_live(blob)        # mirror → ~/.codex
                elif sync_back(ctx, st, tool):
                    st.save()
        except Exception:
            pass
