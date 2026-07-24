"""Run an official login flow in a terminal for the 'add a seat' UX (no-browser-dance promise).

IMPORTANT (invariant from docs/PLAN.md): before the official login overwrites the live creds, the
currently-active seat must be synced back, or its rotated refresh token is lost. Call
``prepare_then_login`` (which sync-backs first) rather than launching the login directly.

We launch the login by writing a tiny ``*.command`` script and handing it to LaunchServices via
``open`` — NOT by driving Terminal.app with AppleEvents (``osascript … tell application "Terminal"``).
AppleEvents are gated by macOS TCC "Automation" permission: on a fresh machine that consent hasn't
been granted, ``osascript`` fails and the sign-in silently never opens (the field bug we're fixing).
``open`` needs no Automation grant and honours the user's DEFAULT handler for ``.command`` (Terminal
out of the box, iTerm/Ghostty/etc. if they set it) instead of being hard-wired to Terminal.app.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile

from acctsw import TOOLS
from acctsw.context import Context
from acctsw.switch import sync_back


def open_in_terminal(command: str) -> None:
    """Launch ``command`` in the user's terminal via a ``*.command`` script + ``open`` (LaunchServices).

    Raises if the launch fails, so the caller can tell the user the sign-in didn't actually open
    instead of waiting on a window that never came. Uses ``open``, not AppleEvents/osascript, so it
    needs no macOS Automation permission and respects the default ``.command`` handler.

    The command runs through a LOGIN + INTERACTIVE shell (``$SHELL -lic``): a bare ``#!/bin/zsh``
    script is neither, so it would get launchd's minimal PATH and miss both ``node`` (which
    ``codex``/``claude`` need) and any version-manager shim (asdf/volta/nvm) the user's CLI lives on.
    Sourcing the user's profile+rc reproduces the PATH they have in their own terminal — matching the
    old osascript ``do script`` behaviour this replaced. The script deletes ITSELF once the login shell
    returns (``rm -- "$0"``), so no temp file is left behind and none is ever swept out from under an
    in-flight sign-in."""
    if not command:
        return
    from acctsw.procenv import _PY_ENV_STRIP, harden_env
    # Scrub PYTHONPATH/PYTHONHOME etc before handing off to the login shell: py2app's launcher exports
    # them pointing at the frozen app's stdlib zip and a child python3 would break with "can't find
    # module 'encodings'". (A separately-launched terminal generally won't inherit our env, but this is
    # cheap belt-and-suspenders that holds regardless.)
    unset = " ".join(_PY_ENV_STRIP)
    # NOTE: no `exec` — the login shell runs as a child so control returns to delete this script.
    script = (f"#!/bin/zsh\nunset {unset}\n"
              f'"${{SHELL:-/bin/zsh}}" -lic {shell_quote([command])}\n'
              f'rm -f -- "$0"\n')
    fd, path = tempfile.mkstemp(suffix=".command", prefix="ai-guest-list-signin-")
    launched = False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(path, 0o700)  # owner-only executable; content is just the login command, not a secret
        # env=harden_env() keeps the frozen interpreter vars out of the `open` process too.
        proc = subprocess.run(["open", path], capture_output=True, text=True, env=harden_env())
        launched = proc.returncode == 0
        if not launched:
            detail = (proc.stderr or "").strip()   # surface the real LaunchServices reason if any
            base = "couldn't open a terminal for the sign-in — try again"
            raise RuntimeError(f"{base} ({detail})" if detail else base)
    except OSError as e:
        raise RuntimeError("couldn't open a terminal for the sign-in — try again") from e
    finally:
        if not launched:
            # The script self-deletes only once a terminal RUNS it; if the launch failed, nothing will,
            # so remove it here (both the RuntimeError and OSError paths) rather than leak an executable.
            try:
                os.unlink(path)
            except OSError:
                pass


def resolve_login_command(ctx: Context, tool: str) -> str:
    """Build the official sign-in command as a shell string, preferring the CLI's ABSOLUTE path.

    Prefer the absolute binary the engine already found (``ctx.codex_bin``/``ctx.claude_bin``), so a
    GUI app's minimal PATH can't hide a CLI that IS installed. Otherwise fall back to the BARE command
    name — never hard-fail here: the login runs in a login+interactive shell (see ``open_in_terminal``)
    that sources the user's rc, so a version-manager shim (asdf/volta/nvm) on the rc-only PATH still
    resolves. If the CLI is genuinely absent the bare command surfaces a visible "command not found" in
    the terminal we opened (same as the previous osascript path) — we deliberately do NOT probe the
    login shell to pre-detect that: an interactive-shell probe misreads aliases/wrappers, can false-
    fail a TTY-guarded shim, and adds seconds of latency, all to improve one rare error message."""
    from acctsw.bridge import LOGIN_SUBCMD
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    exe = (ctx.codex_bin if tool == "codex" else ctx.claude_bin) or tool
    return shell_quote([exe, *LOGIN_SUBCMD[tool]])


def json_escape(s: str) -> str:
    # AppleScript string literal (retained for any external callers; the login path no longer uses it).
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def prepare_then_login(ctx: Context, tool: str, command: str | None = None) -> None:
    """Sync-back the active seat (so its rotated token isn't lost), then launch the login.

    ``command`` defaults to the resolved absolute-path login command (see ``resolve_login_command``);
    tests may inject an explicit one. The resolution runs BEFORE the sync-back so a missing CLI is
    reported without side effects.

    The sync-back reads the active seat + live creds and writes the seat's snapshot, so it holds the
    cross-process lock (the 180s usage poll writes the same store). It does NOT mutate state.json —
    so no state.save(). The terminal is launched AFTER releasing the lock: a subprocess must never be
    held under the lock, and the login itself is what overwrites the live creds next.
    """
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    if command is None:
        command = resolve_login_command(ctx, tool)
    with ctx.locked():
        state = ctx.load_state()
        sync_back(ctx, state, tool)
    if command:
        open_in_terminal(command)


def shell_quote(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
