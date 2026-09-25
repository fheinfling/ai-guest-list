"""Per-tool "canonical credential location" — the place the official tool reads its active account.

- Codex: the file ``~/.codex/auth.json``.
- Claude: the macOS Keychain item ``Claude Code-credentials``.

Each location knows how to read/write the live blob (atomically) and how to derive the account
email from a blob when possible. Abstracted so the engine treats both tools uniformly and tests
can point them at temp files / a fake keychain.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .keychain import KeychainBackend
from .util import atomic_write_text, jwt_payload


def _codex_auth_tokens(blob: str) -> dict | None:
    """Read the token object from a Codex auth blob without trusting its JSON shape."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def codex_jwt_email(blob: str) -> str | None:
    """Return the actual email claim from a Codex auth blob's id-token JWT.

    This deliberately does not fall back to account/workspace IDs: those are useful display hints,
    but cannot prove that credential bytes belong in a seat keyed by an email address.
    """
    tokens = _codex_auth_tokens(blob)
    if tokens is None:
        return None
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    payload = jwt_payload(id_token)
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


def codex_jwt_matches(email: str, blob: str) -> bool:
    """Whether a Codex credential's JWT names exactly this email (case-insensitively)."""
    actual = codex_jwt_email(blob)
    return actual is not None and actual.casefold() == email.casefold()


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
            return self.auth_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def set_live(self, blob: str) -> None:
        atomic_write_text(self.auth_path, blob, mode=0o600)

    def email_of(self, blob: str) -> str | None:
        email = codex_jwt_email(blob)
        if email:
            return email
        tokens = _codex_auth_tokens(blob)
        if tokens is None:
            return None
        id_token = tokens.get("id_token")
        payload = jwt_payload(id_token) if isinstance(id_token, str) else {}
        if not isinstance(payload, dict):
            return tokens.get("account_id") if isinstance(tokens.get("account_id"), str) else None
        # Fallback: the ChatGPT account id, so seats are still distinguishable.
        auth = payload.get("https://api.openai.com/auth")
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        if isinstance(account, str) and account:
            return account
        fallback = tokens.get("account_id")
        return fallback if isinstance(fallback, str) and fallback else None


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

    def email_of(self, blob: str) -> str | None:
        # Not derivable from the blob (only OAuth tokens + subscriptionType). Email is captured
        # from `claude auth status` at add-time and stored in state instead.
        return None
