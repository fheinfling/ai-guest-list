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

    def email_of(self, blob: str) -> str | None:
        # Not derivable from the blob (only OAuth tokens + subscriptionType). Email is captured
        # from `claude auth status` at add-time and stored in state instead.
        return None
