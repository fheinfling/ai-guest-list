"""Shared fixtures: an isolated engine Context backed by a temp dir + in-memory keychain.

No unit test ever touches the real ~/.codex/auth.json or the real macOS Keychain.
"""
import base64
import json

import pytest

from acctsw.context import Context


def make_codex_blob(email: str, account_id: str | None = None) -> str:
    """A minimal auth.json whose id_token JWT carries the given email. ``account_id`` is the
    underlying ChatGPT account (two seats sharing it are the same account); it DEFAULTS to a distinct
    per-email id so separate seats model separate accounts — pass the SAME id to model a duplicate."""
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    id_token = f"header.{payload}.sig"
    return json.dumps({"auth_mode": "ChatGPT", "tokens": {"id_token": id_token, "access_token": "a",
                       "refresh_token": "r", "account_id": account_id or f"acct:{email}"}})


def make_claude_blob(sub: str = "max") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                       "expiresAt": 0, "scopes": [], "subscriptionType": sub}})


@pytest.fixture
def ctx(tmp_path):
    c = Context.for_test(tmp_path)
    c.ensure_dirs()
    return c
