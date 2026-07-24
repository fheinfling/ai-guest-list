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
    needs no macOS Automation permission and respects the default ``.command`` handler."""
    if not command:
        return
    # Scrub PYTHONPATH/PYTHONHOME etc inside the script: py2app's launcher exports them pointing at the
    # frozen app's 3.11 stdlib zip; any shell that inherited them (→ system python3) would break with
    # "can't find module 'encodings'". A separate, already-running terminal generally won't inherit our
    # process env, but the `unset` is cheap belt-and-suspenders that holds regardless of how the
    # terminal was launched.
    from acctsw.procenv import _PY_ENV_STRIP, harden_env
    unset = " ".join(_PY_ENV_STRIP)
    script = f"#!/bin/zsh\nunset {unset}\n{command}\n"
    fd, path = tempfile.mkstemp(suffix=".command", prefix="ai-guest-list-signin-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(path, 0o700)  # owner-only executable; content is just the login command, not a secret
        # env=harden_env() keeps the frozen interpreter vars out of the `open` process too.
        proc = subprocess.run(["open", path], capture_output=True, text=True, env=harden_env())
    except OSError as e:
        raise RuntimeError("couldn't open a terminal for the sign-in — try again") from e
    if proc.returncode != 0:
        raise RuntimeError("couldn't open a terminal for the sign-in — try again")


def resolve_login_command(ctx: Context, tool: str) -> str:
    """Build the official sign-in command as a shell string using the CLI's ABSOLUTE path.

    A GUI-launched app inherits launchd's minimal PATH; the terminal we open runs a fresh login shell
    whose PATH comes from the user's rc — which may not list `codex`/`claude`. Passing a bare
    ``codex login`` would then hit "command not found" while ``open`` still reports success, so the app
    would falsely claim the sign-in opened. Resolve the absolute binary the engine already found
    (``ctx.codex_bin``/``ctx.claude_bin``) and fail LOUDLY here if it's missing, before launching."""
    from acctsw.bridge import LOGIN_SUBCMD
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    exe = ctx.codex_bin if tool == "codex" else ctx.claude_bin
    if not exe:
        raise RuntimeError(f"can't find the {tool} command — install {tool} first, then sign in")
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
