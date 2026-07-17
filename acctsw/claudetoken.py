"""Claude `setup-token` handling — validate a pasted long-lived token and name its account.

A `claude setup-token` value is a subscription **OAuth** token (`sk-ant-oat…`, "requires Claude
subscription"), NOT a console API key. The add-seat "paste a token" path installs one so a Claude
seat can be added with no browser.

Two things it does, and one it deliberately does not:
  - `looks_like_setup_token` — a cheap FORMAT check only. `sk-ant-oat` is also the prefix of an
    ordinary rotating access token, so this proves nothing about identity or longevity; the profile
    call below is the real gate.
  - `claude_identity` — hits the OAuth **profile** endpoint, which both validates the token (401 on
    a bad one) and returns ITS OWN account's email + plan. This is why we never touch
    `claude auth status` here: that reads identity from ~/.claude.json (`oauthAccount`), so a garbage
    token still reports `loggedIn:true` with the *previously* signed-in account's email — it would
    register a pasted seat under the wrong identity and never trigger a rollback.

Network access is injected (`get=`), exactly like `usage.py`, so unit tests never hit the wire.
"""
from __future__ import annotations

import json

from . import paths as P
from .usage import HttpGet, _default_get, claude_oauth_headers


def looks_like_setup_token(blob: str) -> str | None:
    """Return the stripped token if ``blob`` is shaped like a setup-token, else None. Format only."""
    tok = (blob or "").strip()
    if tok.startswith("sk-ant-oat") and len(tok) >= 30:
        return tok
    return None


def claude_identity(token: str, *, user_agent: str | None = None,
                    get: HttpGet = _default_get, timeout: float = 12.0
                    ) -> tuple[str, str | None] | None:
    """Validate ``token`` against the OAuth profile endpoint and return ``(email, plan)``.

    ``plan`` is ``"max"`` / ``"pro"`` / ``None`` (unknown) — it feeds the seat's plan chip. Returns
    None for a rejected token (401), a network failure, unparseable JSON, or a body with no email.
    """
    status, body = get(P.CLAUDE_PROFILE_URL, claude_oauth_headers(token, user_agent), timeout)
    if status != 200:
        return None
    try:
        account = (json.loads(body) or {}).get("account") or {}
        email = account.get("email")
    except (ValueError, TypeError, AttributeError):
        return None                 # non-object JSON / account-not-a-dict — same net as usage.py
    if not email:
        return None
    plan = "max" if account.get("has_claude_max") else "pro" if account.get("has_claude_pro") else None
    return email, plan
