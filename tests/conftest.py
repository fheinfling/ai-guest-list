"""Shared fixtures: an isolated engine Context backed by a temp dir + in-memory keychain.

No unit test ever touches the real ~/.codex/auth.json or the real macOS Keychain.
"""
import base64
import json

import pytest

from acctsw import paths as P
from acctsw import install as install_mod
from acctsw.context import Context


def make_codex_blob(email: str, account_id: str | None = None, *,
                    user_id: str | None = None) -> str:
    """A minimal auth.json whose id_token JWT carries the given email. ``account_id`` is the ChatGPT
    account/workspace; it DEFAULTS to a distinct per-email id so separate seats model separate
    subscriptions — pass the SAME id to model one subscription. ``user_id`` is the PERSON inside it
    (what the fingerprint keys on): two seats with one account_id and DIFFERENT user ids model
    Team/Business colleagues, the same user_id models one login twice. Omitting it reproduces a
    pre-user-id blob, where the fingerprint falls back to the account id."""
    claims: dict = {"email": email}
    if user_id is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id,
                                                 "chatgpt_user_id": user_id}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    id_token = f"header.{payload}.sig"
    return json.dumps({"auth_mode": "ChatGPT", "tokens": {"id_token": id_token, "access_token": "a",
                       "refresh_token": "r", "account_id": account_id or f"acct:{email}"}})


def make_claude_blob(sub: str = "max") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                       "expiresAt": 0, "scopes": [], "subscriptionType": sub}})


@pytest.fixture(autouse=True)
def _isolate_tool_config_dirs(tmp_path, monkeypatch):
    """Point the TOOL config dirs at tmp for every test, not just the data store.

    `headroom._config_dir` reads the module-level `paths.CODEX_HOME` / `CLAUDE_CONFIG_DIR`, so a test
    that reaches cleanup without patching them edits the DEVELOPER'S REAL ~/.codex/config.toml and
    ~/.claude/settings.json — on a machine with legacy routing that is a real surgical cleanup of
    real files, using a tmp store that has none of the real backups. Autouse so no future test can
    forget; tests needing to inspect these dirs still monkeypatch them explicitly.
    """
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "_home_codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "_home_claude")
    # Supervision status is included in every bridge snapshot. Keep both its read-only rc probe and
    # toggle/bootstrap writes inside tmp so no test ever inspects or edits the developer's shell rc.
    monkeypatch.setattr(install_mod, "BIN_DIR", tmp_path / "_home_local" / "bin")
    monkeypatch.setattr(install_mod.Path, "home", classmethod(lambda cls: tmp_path / "_home"))


@pytest.fixture(autouse=True)
def _clear_process_start_caches():
    """Drop the pid → ``ps`` start-time caches around every test.

    Both caches are keyed on ``os.getpid()``, which in production is the supervisor's own pid and a
    value that never changes — so caching it is correct there. Under pytest every test shares one
    pid, so a test that monkeypatches ``_proc_start`` to return a fake start leaves that fake in the
    cache after its monkeypatch is undone. A later test then writes the fake into a session
    heartbeat while the real ``ps`` reports the true start, the two disagree, and a live session
    reads as dead — a failure that only appears when the files run in the same process, and points
    at the wrong module when it does.
    """
    from acctsw import appalive, session
    for cache in (session._START_CACHE, appalive._START_CACHE):
        cache.clear()
    yield
    for cache in (session._START_CACHE, appalive._START_CACHE):
        cache.clear()


@pytest.fixture
def ctx(tmp_path):
    c = Context.for_test(tmp_path)
    c.ensure_dirs()
    return c
