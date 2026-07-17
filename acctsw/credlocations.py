"""Per-tool "canonical credential location" — the place the official tool reads its active account.

- Codex: the file ``~/.codex/auth.json``.
- Claude: the macOS Keychain item ``Claude Code-credentials``.

Each location knows how to read/write the live blob (atomically) and how to derive the account
email from a blob when possible. Abstracted so the engine treats both tools uniformly and tests
can point them at temp files / a fake keychain.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from .keychain import KeychainBackend
from .util import atomic_write_text, jwt_payload


class CredLocation(Protocol):
    tool: str
    def get_live(self) -> str | None: ...
    def set_live(self, blob: str) -> None: ...
    def email_of(self, blob: str) -> str | None: ...


class CodexCredLocation:
    """Active Codex account lives in a JSON file; email is in the id_token JWT."""

    tool = "codex"

    def __init__(self, auth_path: Path) -> None:
        self.auth_path = Path(auth_path)

    def get_live(self) -> str | None:
        try:
            return self.auth_path.read_text()
        except FileNotFoundError:
            return None

    def set_live(self, blob: str) -> None:
        atomic_write_text(self.auth_path, blob, mode=0o600)

    def email_of(self, blob: str) -> str | None:
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return None
        tokens = data.get("tokens") or {}
        payload = jwt_payload(tokens.get("id_token", ""))
        email = payload.get("email")
        if email:
            return email
        # Fallback: the ChatGPT account id, so seats are still distinguishable.
        auth = payload.get("https://api.openai.com/auth") or {}
        return auth.get("chatgpt_account_id") or tokens.get("account_id")


class ClaudeCredLocation:
    """Active Claude account lives in the Keychain. The blob has no email; callers supply it.

    The official Claude Code item is keyed by service ``Claude Code-credentials`` and account =
    the macOS short username (e.g. ``alice``). We must use that exact (service, account)
    pair so ``set_live`` updates the *same* item Claude reads — using a different account would
    create a duplicate item that Claude ignores.
    """

    tool = "claude"

    def __init__(self, keychain: KeychainBackend, service: str, account: str) -> None:
        self.keychain = keychain
        self.service = service
        self.account = account

    def get_live(self) -> str | None:
        return self.keychain.get(self.service, self.account)

    def set_live(self, blob: str) -> None:
        self.keychain.set(self.service, self.account, blob)

    def clear_live(self) -> None:
        """Remove the item entirely — used to roll back to 'nothing was here' when a paste fails and
        there was no prior credential to restore."""
        self.keychain.delete(self.service, self.account)

    def email_of(self, blob: str) -> str | None:
        # Not derivable from the blob (only OAuth tokens + subscriptionType). Email is captured
        # from the OAuth profile endpoint (setup-token paste) or `claude auth status` at add-time.
        return None

    @staticmethod
    def merge_token(existing: str | None, token: str, plan_raw: str | None) -> str:
        """Splice a setup-token into the keychain blob WITHOUT clobbering the rest of a shared item.

        The real ``Claude Code-credentials`` item carries more than the login: alongside
        ``claudeAiOauth`` it holds ``mcpOAuth`` (the user's MCP-server logins, e.g. a GitHub plugin).
        Constructing a fresh ``{"claudeAiOauth": …}`` blob would destroy that, so we merge into the
        existing item. Only the fields our token owns are touched:
          - ``accessToken`` ← the pasted setup-token
          - ``refreshToken`` / ``refreshTokenExpiresAt`` DROPPED (not nulled): setup-tokens don't
            rotate, and a leftover refresh token from the PREVIOUS account could rotate the item back
            to that identity.
          - ``expiresAt`` ← ~1 year out (epoch millis).
          - ``subscriptionType`` ← the profile's plan, or removed so the old tier can't linger.
        Everything else in ``claudeAiOauth`` (scopes, unknown keys) and every sibling top-level key
        (``mcpOAuth`` …) rides along untouched.
        """
        try:
            base = json.loads(existing) if existing else {}
        except (ValueError, TypeError):
            base = {}
        if not isinstance(base, dict):
            base = {}
        raw = base.get("claudeAiOauth")
        oauth = dict(raw) if isinstance(raw, dict) else {}   # corrupt/non-dict item → start clean
        oauth["accessToken"] = token
        oauth.pop("refreshToken", None)
        oauth.pop("refreshTokenExpiresAt", None)
        oauth["expiresAt"] = int(time.time() * 1000) + 365 * 24 * 3600 * 1000
        if plan_raw:
            oauth["subscriptionType"] = plan_raw
        else:
            oauth.pop("subscriptionType", None)
        oauth.setdefault("scopes", [])          # the from-empty case; matches the accepted shape
        base["claudeAiOauth"] = oauth
        return json.dumps(base)
