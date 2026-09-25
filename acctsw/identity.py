"""Resolve the account identity (email) of the *currently live* creds for a tool.

- Codex: decode the id_token JWT inside the live ``auth.json``.
- Claude: ask the official CLI (``claude auth status --json``) since the blob carries no email.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from .context import Context


@dataclass(frozen=True)
class ClaudeLiveIdentity:
    """One identity answer tied to the exact credential bytes that were live when Claude answered.

    Resolving the email and reading the blob as one value lets callers do the slow CLI subprocess
    before taking the state flock, then reject the answer if an out-of-band login changed the live
    Keychain item before the quick reconciliation write.
    """
    blob: str | None
    email: str | None


def live_email(ctx: Context, tool: str) -> str | None:
    if tool == "codex":
        blob = ctx.cred["codex"].get_live()
        return ctx.cred["codex"].email_of(blob) if blob else None
    if tool == "claude":
        return claude_status_email(ctx.claude_bin)
    raise ValueError(f"unknown tool: {tool}")


def claude_live_identity(ctx: Context) -> ClaudeLiveIdentity:
    """Resolve Claude's official identity for the exact currently-live credential blob.

    This may spend up to 30 seconds in ``claude auth status``. Callers that also need
    ``Context.locked()`` must invoke this first and pass the returned pair through to the mutation.
    With no live blob there is nothing to identify, so avoid spawning the CLI altogether.
    """
    blob = ctx.cred["claude"].get_live()
    email = claude_status_email(ctx.claude_bin) if blob else None
    return ClaudeLiveIdentity(blob=blob, email=email)


def claude_status_email(claude_bin: str | None = None) -> str | None:
    exe = claude_bin or shutil.which("claude")
    if not exe:
        return None
    try:
        # A malformed diagnostic must not turn an identity probe into a decoding crash.
        proc = subprocess.run(
            [exe, "auth", "status", "--json"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data.get("email") if data.get("loggedIn") else None
