"""Decide when a running supervisor should restart itself on a newer engine, and how.

A terminal session outlives the install that started it: the supervisor keeps running old code long
after the app or a checkout moved on. It may swap itself for the newer engine, but only when nobody
is at the keyboard (``InputTracker``), only for a strictly newer engine (``target_engine``, which is
what stops restart loops) and only if the session can be reopened without re-sending a prompt
(``restart_args``). Stdlib-only and pure apart from the injected probe; launcher.py and cli.py do
the waiting and the exec.

Option tables mirror ``claude --help`` of Claude Code 2.1.316 and ``codex --help`` / ``codex resume
--help`` of codex-cli 0.159.3. Anything else means no restart: a stale session is an inconvenience,
a guessed argv can re-send a prompt or drop the sandbox.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import __version__, engine

# Longer than the longest scheduled wakeup (an hour): a session looping on a timer never gets here,
# and those timers live only in the process a restart would end. The restart is never urgent.
RESTART_QUIET_S = 3900.0
# Started this long after the child, a process is the session's own work, not its startup's MCP servers.
STARTUP_GRACE_S = 120.0
CHECK_INTERVAL_S = 60.0
SEAT_ENV = "ACCTSW_RESTART_SEAT"

# An unfinished sequence waits this long for its other half; after that it was a real Esc press.
_SPLIT_WINDOW_S = 0.5
_MAX_CARRY = 4096

_ESC = 0x1b
_STRING_INTROS = b"]PX^_"   # OSC, DCS, SOS, PM, APC: run until BEL or ST
_PASTE_ON = b"\x1b[200~"
_PASTE_OFF = b"\x1b[201~"
_FOCUS = (b"\x1b[I", b"\x1b[O")
# A newline inside a paste is text in the box, not Enter.

# Left out on purpose, so they mean no restart: -p/--print and every print-only option, the session
# movers (--fork-session, --bg, --desktop, --cloud, --environment, --from-pr, --teleport,
# --remote-control), -w/--worktree and --tmux (a NEW worktree), --file (startup download), and the
# optional-value --debug/--prompt-suggestions, whose next word may be a prompt.
_CLAUDE_FLAGS = frozenset({
    "--allow-dangerously-skip-permissions", "--ax-screen-reader", "--bare", "--brief", "--chrome",
    "--dangerously-skip-permissions", "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections", "--ide", "--no-chrome", "--restricted",
    "--safe-mode", "--strict-mcp-config", "--verbose",
})
# Several of these are variadic in claude. Only one value is accepted here: a second word could be
# a prompt the user thought they were passing.
_CLAUDE_VALUES = frozenset({
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools",
    "--append-system-prompt", "--append-system-prompt-file", "--autocompact", "--betas",
    "--debug-file", "--disallowedTools", "--disallowed-tools", "--effort", "--fallback-model",
    "--mcp-config", "--model", "-n", "--name", "--permission-mode", "--plugin-dir", "--plugin-url",
    "--setting-sources", "--settings", "--system-prompt", "--system-prompt-file",
    "--system-prompt-snapshot", "--tools",
})
_CLAUDE_RESUME_FLAGS = frozenset({"-c", "--continue"})
_CLAUDE_RESUME_OPTIONAL = frozenset({"-r", "--resume"})

# Left out on purpose: -i/--image (first turn only) and --worktree (codex cannot resume one, see
# codexexec.py). All of these are also valid after `codex resume`.
_CODEX_FLAGS = frozenset({
    "--strict-config", "--oss", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "--search", "--no-alt-screen", "--no-daemon",
})
_CODEX_VALUES = frozenset({
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-m",
    "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir",
    "-a", "--ask-for-approval",
})
# They only choose which session `resume` opens, which the thread now decides.
_CODEX_RESUME_ONLY = frozenset({"--last", "--all", "--include-non-interactive"})


class EngineRestart(Exception):
    """Raised by run() to unwind to the cli, which execs ``argv``."""

    def __init__(self, argv: list[str], *, seat: str | None = None):
        super().__init__("restart on a newer engine")
        self.argv = list(argv)
        self.seat = seat


def _csi(data: bytes, start: int) -> tuple[int, str] | None:
    i = start + 2
    while i < len(data) and 0x30 <= data[i] <= 0x3f:
        i += 1
    params = data[start + 2:i]
    while i < len(data) and 0x20 <= data[i] <= 0x2f:
        i += 1
    if i >= len(data):
        return None
    if not 0x40 <= data[i] <= 0x7e:
        return i, "typed"  # malformed: the byte that broke it stands on its own
    end = i + 1
    seq = data[start:end]
    if seq in _FOCUS:
        return end, "passive"
    if seq == b"\x1b[M":  # legacy mouse report: three raw bytes follow
        return (end + 3, "passive") if end + 3 <= len(data) else None
    if data[i] in b"Mm" and (params.startswith(b"<") or params.count(b";") == 2):
        return end, "passive"  # SGR and urxvt mouse reports
    if seq == _PASTE_ON:
        return end, "paste_on"
    if seq == _PASTE_OFF:
        return end, "paste_off"
    if data[i] in b"Rc":
        return end, "passive"  # the terminal answering the child: cursor position, device attributes
    return end, "typed"


def _sequence(data: bytes, start: int) -> tuple[int, str] | None:
    """(end, kind) of the escape sequence at ``start``, or None if it is cut off by the chunk."""
    if start + 1 >= len(data):
        return None
    intro = data[start + 1]
    if intro == 0x5b:
        return _csi(data, start)
    if intro == 0x4f:  # SS3
        if start + 2 >= len(data):
            return None
        return (start + 3 if 0x20 <= data[start + 2] <= 0x7e else start + 2), "typed"
    if intro in _STRING_INTROS:
        ends = [(i + n) for i, n in ((data.find(b"\x07", start), 1),
                                     (data.find(b"\x1b\\", start), 2)) if i >= 0]
        return (min(ends), "passive") if ends else None  # only ever the terminal replying
    if 0x20 <= intro <= 0x7e or intro in b"\r\n":
        return start + 2, "typed"  # Alt+key. Alt+Enter is a newline in the box, not a submit.
    return start + 1, "typed"  # a lone Esc: what follows stands on its own


def _scan(data: bytes) -> tuple[list[tuple[str, bytes]], bytes]:
    """Split input into (kind, bytes) tokens, plus any sequence the chunk ends in the middle of."""
    tokens: list[tuple[str, bytes]] = []
    i = 0
    while i < len(data):
        esc = data.find(_ESC, i)
        if esc < 0:
            tokens.append(("text", data[i:]))
            break
        if esc > i:
            tokens.append(("text", data[i:esc]))
        found = _sequence(data, esc)
        if found is None:
            return tokens, data[esc:]
        i, kind = found
        tokens.append((kind, data[esc:i]))
    return tokens, b""


def visible_input(data: bytes) -> bytes:
    """``data`` without terminal escape sequences; pasted text between its markers stays."""
    tokens, _ = _scan(data)
    return b"".join(chunk for kind, chunk in tokens if kind == "text")


class InputTracker:
    """When the user last touched the keyboard, and whether unsent text may be in the prompt box."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """A new child: it starts with an empty prompt box."""
        self.last_input: float | None = None
        self._draft = False
        self._carry = b""
        self._carry_at = 0.0
        self._in_paste = False

    @property
    def draft(self) -> bool:
        return self._draft or bool(self._carry)  # a held-back Esc may yet be a key

    def note(self, data: bytes, at: float) -> None:
        if self._carry and at - self._carry_at > _SPLIT_WINDOW_S:
            self._carry = b""
        started = self._carry_at if self._carry else at
        tokens, tail = _scan(self._carry + data)
        self._carry = tail if len(tail) <= _MAX_CARRY else b""
        self._carry_at = started

        for kind, chunk in tokens:
            if kind == "paste_on":
                self._in_paste = True
            elif kind == "paste_off":
                self._in_paste = False
            elif kind == "typed":
                self._draft = True  # recalled history, completion, kitty-encoded text: can't tell
            elif kind == "text":
                # Raw mode: Enter is CR. LF is Ctrl+J, a newline inside the prompt, not a submit.
                self._draft = self._in_paste or not chunk.endswith(b"\r")
        if tail or any(kind != "passive" for kind, _ in tokens):
            self.last_input = at


def newer_writer(state_data: dict, version: str = __version__) -> str | None:
    """The newest engine that wrote this store, if it is known and strictly newer than ``version``."""
    stamp = state_data.get("engine") if isinstance(state_data, dict) else None
    newest = stamp.get("newest_writer") if isinstance(stamp, dict) else None
    return newest if engine.older_than(version, newest) else None


def target_engine(version: str = __version__, *, exe=None, probe=engine.probe) -> str | None:
    """The wrapper to exec, only if the engine behind it is strictly newer than ``version``.

    Equal must stay None: the restarted engine asks this same question, and that is the loop guard.
    """
    path = exe if exe is not None else Path.home() / ".local" / "bin" / "acctsw"
    found = probe(path)
    newer = found.get("version") if isinstance(found, dict) else None
    return str(path) if engine.older_than(version, newer) else None


def _keep(args: list[str], i: int, flags: frozenset, values: frozenset, *,
          attached_short: bool = False) -> tuple[list[str], int] | None:
    """The whitelisted option at ``args[i]`` as (tokens, next index), or None if it can't be kept."""
    tok = args[i]
    if tok in flags:
        return [tok], i + 1
    if tok in values:
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i:i + 2], i + 2
        return None
    if tok.startswith("--"):
        return ([tok], i + 1) if "=" in tok and tok.split("=", 1)[0] in values else None
    if attached_short and tok[:2] in values:
        return [tok], i + 1
    return None


def _claude_args(args: list[str], thread: str | None) -> list[str] | None:
    kept: list[str] = []
    resuming = False
    i = 0
    while i < len(args):
        tok = args[i]
        name, eq, _ = tok.partition("=")
        if tok in _CLAUDE_RESUME_FLAGS:
            resuming = True
            i += 1
        elif name in _CLAUDE_RESUME_OPTIONAL:
            resuming = True
            i += 1
            if not eq and i < len(args) and not args[i].startswith("-"):
                i += 1
        elif name == "--session-id":
            resuming = True
            i += 1
            if not eq:
                if i >= len(args) or args[i].startswith("-"):
                    return None
                i += 1
        elif not tok.startswith("-"):
            return None
        else:
            found = _keep(args, i, _CLAUDE_FLAGS, _CLAUDE_VALUES)
            if found is None:
                return None
            kept += found[0]
            i = found[1]
    if thread:
        return [*kept, "--resume", thread]
    return None if resuming else kept


def _codex_args(args: list[str], thread: str | None) -> list[str] | None:
    kept: list[str] = []
    resuming = named = last = False
    i = 0
    while i < len(args):
        tok = args[i]
        if not tok.startswith("-"):
            if not resuming and tok == "resume":
                resuming = True
            elif resuming and not named:
                named = True
            else:
                return None
            i += 1
        elif resuming and tok in _CODEX_RESUME_ONLY:
            last = last or tok == "--last"
            i += 1
        else:
            found = _keep(args, i, _CODEX_FLAGS, _CODEX_VALUES, attached_short=True)
            if found is None:
                return None
            kept += found[0]
            i = found[1]
    if last and named:
        return None  # with --last, codex reads that word as a prompt
    if thread:
        return ["resume", thread, *kept]
    return None if resuming else kept


def restart_args(tool: str, args: list[str], thread: str | None) -> list[str] | None:
    """Arguments for ``acctsw run <tool> ...`` that reopen this session with the user's flags.

    None means do not restart: this invocation has a prompt, or something we can't vouch for.
    """
    if thread and thread.startswith("-"):
        return None
    if tool == "claude":
        return _claude_args(args, thread)
    if tool == "codex":
        return _codex_args(args, thread)
    return None


def _elapsed(text: str) -> int | None:
    """``ps`` etime, ``[[dd-]hh:]mm:ss``, in seconds."""
    try:
        days, _, clock = text.rpartition("-")
        total = int(days) * 24 if days else 0
        parts = [int(part) for part in clock.split(":")]
        if len(parts) == 2:
            parts.insert(0, 0)
        hours, minutes, seconds = parts
    except ValueError:
        return None
    return ((total + hours) * 60 + minutes) * 60 + seconds


def _late_descendants(root: int, table: str, grace: float) -> bool:
    children: dict[int, list[tuple[int, int]]] = {}
    for line in table.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        elapsed = _elapsed(fields[2])
        if elapsed is None:
            return True
        children.setdefault(int(fields[1]), []).append((int(fields[0]), elapsed))
    for child, child_age in children.get(root, []):
        pending = [pid for pid, _ in children.get(child, [])]
        ages = dict(children.get(child, []))
        while pending:
            pid = pending.pop()
            if ages[pid] < child_age - grace:
                return True
            ages.update(children.get(pid, []))
            pending.extend(next_pid for next_pid, _ in children.get(pid, []))
    return False


def has_late_descendants(root: int, *, grace: float = STARTUP_GRACE_S, run=subprocess.run) -> bool:
    """Did the child start anything after its own startup? A background shell or a dev server is
    silent work a restart would kill. Anything unclear counts as yes: the session just keeps running.
    (A later-started MCP server also defers the restart, which only costs the banner staying.)"""
    try:
        table = run(["ps", "-axo", "pid=,ppid=,etime="], capture_output=True, text=True,
                    timeout=5.0, check=True).stdout
        return _late_descendants(root, table, grace)
    except (OSError, subprocess.SubprocessError, ValueError):
        return True
