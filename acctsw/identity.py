"""Resolve the account identity (email) of the *currently live* creds for a tool.

- Codex: decode the id_token JWT inside the live ``auth.json``.
- Claude: ask the official CLI (``claude auth status --json``) since the blob carries no email.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from .context import Context


def live_email(ctx: Context, tool: str) -> str | None:
    if tool == "codex":
        blob = ctx.cred["codex"].get_live()
        return ctx.cred["codex"].email_of(blob) if blob else None
    if tool == "claude":
        return claude_status_email(ctx.claude_bin)
    raise ValueError(f"unknown tool: {tool}")


def claude_status_email(claude_bin: str | None = None) -> str | None:
    exe = claude_bin or shutil.which("claude")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=30
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
