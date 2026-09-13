"""Expired usage credentials must not turn an otherwise signed-in Claude seat into a logout."""
import json
from datetime import timedelta

import pytest

from acctsw import accounts, usage
from acctsw.util import iso, now
from tests.conftest import make_claude_blob
from tests.test_usage import claude_ok_body


def _blob(expiry, refresh="refresh-credential"):
    value = json.loads(make_claude_blob())
    value["claudeAiOauth"].update(expiresAt=expiry, refreshToken=refresh)
    return json.dumps(value)


def test_expired_usage_token_preserves_login_credentials_usage_and_limit(ctx):
    email = "person@example.test"
    blob = _blob((now() - timedelta(minutes=1)).timestamp() * 1000)
    ctx.cred["claude"].set_live(blob)
    state = ctx.load_state()
    accounts.add(ctx, state, "claude", email=email)
    reset = iso(now() + timedelta(hours=2))
    healthy = usage.fetch_claude("token", user_agent="test", get=lambda *_: (
        200, claude_ok_body(five=100, week=75, five_reset=reset)))
    usage.store_fetch(state, "claude", email, healthy)
    fetched_at = state.get_seat("claude", email)["usage"]["fetched_at"]
    limited_until = state.get_seat("claude", email)["limited_until"]

    result = usage._fetch_for("claude", blob, lambda *_: (401, "{}"), "test")
    usage.store_fetch(state, "claude", email, result)
    view = accounts.list_seats(state, "claude")[0]

    assert result.error == "token_expired"
    assert view["needs_login"] is False
    assert view["status"] != "needs-login"
    assert view["limited"] is True and view["limited_until"] == limited_until
    assert view["usage5h"] == 100 and view["usageWeek"] == 75
    assert view["usage_fetched_at"] == fetched_at
    assert ctx.cred["claude"].get_live() == ctx.snapshot_get("claude", email) == blob


@pytest.mark.parametrize("expiry,refresh", [
    (None, "refresh"), (0, "refresh"), (True, "refresh"),
    (float("inf"), "refresh"), (1, None), (1, ""),
])
def test_unknown_expiry_or_missing_refresh_does_not_guess_token_expired(expiry, refresh):
    result = usage._fetch_for("claude", _blob(expiry, refresh), lambda *_: (401, "{}"), "test")
    assert result.error == "unauthorized"


def test_unexpired_token_rejection_is_not_called_expiry():
    blob = _blob((now() + timedelta(hours=1)).timestamp() * 1000)
    result = usage._fetch_for("claude", blob, lambda *_: (401, "{}"), "test")
    assert result.error == "unauthorized"


def test_successful_refresh_clears_expired_token_state(ctx):
    email = "person@example.test"
    old_blob = _blob(1)
    ctx.cred["claude"].set_live(old_blob)
    state = ctx.load_state()
    accounts.add(ctx, state, "claude", email=email)
    failed = usage._fetch_for("claude", old_blob, lambda *_: (401, "{}"), "test")
    usage.store_fetch(state, "claude", email, failed)
    renewed = _blob((now() + timedelta(hours=1)).timestamp() * 1000)
    result = usage._fetch_for("claude", renewed, lambda *_: (200, claude_ok_body()), "test")
    usage.store_fetch(state, "claude", email, result)
    assert state.get_seat("claude", email)["usage"]["error"] is None


@pytest.mark.parametrize("status", [200, 403, 429, 0])
def test_expiry_does_not_override_other_usage_results(status):
    result = usage._fetch_for("claude", _blob(1), lambda *_: (status, claude_ok_body()), "test")
    assert result.error != "token_expired"
