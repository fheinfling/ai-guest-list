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
    # Scrub PYTHONPATH/PYTHONHOME etc: py2app's launcher exports them pointing at the frozen app's 3.11
    # stdlib zip, and Terminal (→ its shells → system python3) would inherit them and break with "can't
    # find module 'encodings'". env=harden_env() only cleans the osascript process — but when Terminal
    # is ALREADY running (the common case), `do script` runs in Terminal.app's own environment, not
    # osascript's, so the new login shell would still inherit the frozen vars. Prepend an `unset` to the
    # command the shell actually executes so the fix holds whether or not Terminal was already open.
    from acctsw.headroom import _PY_ENV_STRIP, harden_env
    scrubbed = f"unset {' '.join(_PY_ENV_STRIP)}; {command}"
    script = f'tell application "Terminal" to do script {json_escape(scrubbed)}\n' \
             'tell application "Terminal" to activate'
    subprocess.run(["osascript", "-e", script], capture_output=True, env=harden_env())


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
