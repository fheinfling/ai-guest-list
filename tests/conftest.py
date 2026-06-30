"""Shared fixtures: an isolated engine Context backed by a temp dir + in-memory keychain.

No unit test ever touches the real ~/.codex/auth.json or the real macOS Keychain.
"""
import base64
import json

import pytest

from acctsw.context import Context


def make_codex_blob(email: str) -> str:
    """A minimal auth.json whose id_token JWT carries the given email."""
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    id_token = f"header.{payload}.sig"
    return json.dumps({"auth_mode": "ChatGPT", "tokens": {"id_token": id_token,
                       "access_token": "a", "refresh_token": "r", "account_id": "acc"}})


def make_claude_blob(sub: str = "max") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                       "expiresAt": 0, "scopes": [], "subscriptionType": sub}})


@pytest.fixture
def ctx(tmp_path):
    c = Context.for_test(tmp_path)
    c.ensure_dirs()
    return c
