"""Run an official login flow in Terminal.app for the 'add a seat' UX (no-browser-dance promise).

IMPORTANT (invariant from docs/PLAN.md): before the official login overwrites the live creds, the
currently-active seat must be synced back, or its rotated refresh token is lost. Call
``prepare_then_login`` (which sync-backs first) rather than launching the login directly.
"""
from __future__ import annotations

import shlex
import subprocess

from acctsw import TOOLS
from acctsw.context import Context
from acctsw.switch import sync_back


def open_in_terminal(command: str) -> None:
    """Open Terminal.app and run ``command`` (best-effort; macOS only)."""
    if not command:
        return
    script = f'tell application "Terminal" to do script {json_escape(command)}\n' \
             'tell application "Terminal" to activate'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def json_escape(s: str) -> str:
    # AppleScript string literal
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def prepare_then_login(ctx: Context, tool: str, command: str | None) -> None:
    """Sync-back the active seat (so its rotated token isn't lost), then launch the login."""
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    state = ctx.load_state()
    sync_back(ctx, state, tool)
    state.save()
    if command:
        open_in_terminal(command)


def shell_quote(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
