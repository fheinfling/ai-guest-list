"""Resolve the account identity (email) of the *currently live* creds for a tool.

- Codex: decode the id_token JWT inside the live ``auth.json``.
- Claude: ask Anthropic's OAuth profile endpoint who owns the blob's access token.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import paths as P
from .context import Context

if TYPE_CHECKING:
    from .usage import HttpGet


@dataclass(frozen=True)
class ClaudeLiveIdentity:
    """One identity answer tied to the exact credential bytes sent to the profile endpoint.

    Resolving the email and reading the blob as one value lets callers do the slow HTTP request
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
        return claude_live_identity(ctx).email
    raise ValueError(f"unknown tool: {tool}")


def claude_live_identity(ctx: Context, *, email: str | None = None,
                         get: HttpGet | None = None) -> ClaudeLiveIdentity:
    """Resolve the token owner's email for the exact currently-live Claude credential blob.

    The profile request has a 10-second timeout. Callers that also need ``Context.locked()`` must
    invoke this first and pass the returned pair through to the mutation. No refresh token is used.
    """
    blob = (ctx.claude_cred(email) if email else ctx.cred["claude"]).get_live()
    email = claude_profile_email(blob, ctx.claude_bin, get=get) if blob else None
    return ClaudeLiveIdentity(blob=blob, email=email)


def claude_profile_email(blob: str, claude_bin: str | None = None,
                         *, get: HttpGet | None = None) -> str | None:
    """Read only account.email; an inconclusive profile leaves the credential owner unknown."""
    from . import usage  # lazy: usage's reconciliation path also imports identity
    try:
        data = json.loads(blob)
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token.strip():
            return None
        status, body = (get or usage._default_get)(P.CLAUDE_PROFILE_URL, {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": usage.claude_user_agent(claude_bin),
        }, 10.0)
        if status != 200:
            return None
        profile = json.loads(body)
        account = profile.get("account") if isinstance(profile, dict) else None
        email = account.get("email") if isinstance(account, dict) else None
        return email.strip() if isinstance(email, str) and email.strip() else None
    except Exception:
        # Transport exceptions can echo credentials or profile data. Discard them entirely.
        return None
