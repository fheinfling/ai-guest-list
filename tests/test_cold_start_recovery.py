"""Fresh launches must recover from stale rests without installing an exhausted login."""
import json
from datetime import timedelta

import pytest

from acctsw import launcher as L
from acctsw import usage as U
from acctsw.util import now, iso
from tests.test_launcher import _two_codex, FakeSpawn
from tests.test_usage import codex_ok_body


def _rest_both(ctx):
    state = _two_codex(ctx)
    for email, hours in [("a@x.com", 3), ("b@x.com", 1)]:
        state.set_limited_until("codex", email, iso(now() + timedelta(hours=hours)), source="hard")
    state.save()
    return state


def test_cold_start_uses_confirmed_healthy_account_without_switching_to_resting_seat(ctx):
    _rest_both(ctx)
    original = ctx.cred["codex"].get_live()
    calls = []

    def get(url, headers, timeout):
        # Old selection installed b (the earlier reset) before checking either account.
        assert ctx.load_state().active("codex") == "a@x.com"
        assert ctx.cred["codex"].get_live() == original
        account = headers["ChatGPT-Account-Id"]
        calls.append(account)
        if account == "acct:a@x.com":
            return 200, json.dumps({
                "plan_type": "self_serve_business_prolite",
                "rate_limit": {"allowed": True, "limit_reached": False,
                    "primary_window": {"used_percent": 3, "limit_window_seconds": 604800,
                                       "reset_after_seconds": 602784}, "secondary_window": None},
            })
        return 200, codex_ok_body(primary=100, allowed=False,
                                  p_reset=iso(now() + timedelta(hours=2)))

    sleeps = []
    spawn = FakeSpawn([(b"codex-cli test-version\n", 0)])
    assert L.run(ctx, "codex", ["--version"], spawn=spawn, get=get,
                 notify=lambda _: None, sleep=sleeps.append) == 0
    assert calls == ["acct:a@x.com", "acct:b@x.com"]
    assert sleeps == []
    assert spawn.calls == [L.build_cmd(ctx, "codex", ["--version"])]
    state = ctx.load_state()
    assert state.active("codex") == "a@x.com"
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None
    assert state.get_seat("codex", "b@x.com")["limited_until"] is not None
    assert ctx.cred["codex"].get_live() == original


@pytest.mark.parametrize("status,allowed,pct", [(0, True, 3), (200, False, 3),
                                                (200, None, 3), (200, True, 95)])
def test_inconclusive_cold_start_preserves_hard_blocks_and_active_credentials(
        ctx, monkeypatch, status, allowed, pct):
    _rest_both(ctx)
    original = ctx.cred["codex"].get_live()
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    spawn = FakeSpawn([])
    get = lambda *_: (status, codex_ok_body(primary=pct, secondary=3, allowed=allowed))
    assert L.run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda _: None) != 0
    assert spawn.calls == []
    assert ctx.load_state().active("codex") == "a@x.com"
    assert ctx.cred["codex"].get_live() == original
    assert all(s["limit_source"] == "hard" for s in ctx.load_state().accounts("codex").values())


def test_in_session_explicit_allowed_does_not_erase_just_observed_billing_limit(ctx):
    state = _rest_both(ctx)
    u = U.fetch_codex("token", "account", get=lambda *_: (
        200, codex_ok_body(primary=3, secondary=3, allowed=True)))
    U.store_fetch(state, "codex", "a@x.com", u)
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"


def test_capacity_probe_does_not_apply_result_after_credentials_change(ctx):
    from tests.conftest import make_codex_blob
    _rest_both(ctx)
    def get(url, headers, timeout):
        if headers["ChatGPT-Account-Id"] == "acct:a@x.com":
            replacement = make_codex_blob("a@x.com", account_id="new-subscription")
            ctx.snapshot_set("codex", "a@x.com", replacement)
            ctx.set_live("codex", replacement)
        return 200, codex_ok_body(primary=3, secondary=3, allowed=True)

    L._verify_capacity(ctx, "codex", get, at=now(), force=True, exclude={"b@x.com"},
                       trust_reactive_lag=False)
    assert ctx.load_state().get_seat("codex", "a@x.com")["limit_source"] == "hard"


def test_capacity_probe_does_not_replace_newer_limit_observation(ctx):
    _rest_both(ctx)
    def get(*_):
        with ctx.locked():
            state = ctx.load_state()
            newer = U.Usage(ok=True, allowed=False, limit_reached=True,
                            fetched_at=iso(now() + timedelta(seconds=1)))
            U.store_fetch(state, "codex", "a@x.com", newer)
            state.save()
        return 200, codex_ok_body(primary=3, secondary=3, allowed=True)

    L._verify_capacity(ctx, "codex", get, at=now(), force=True, exclude={"b@x.com"},
                       trust_reactive_lag=False)
    seat = ctx.load_state().get_seat("codex", "a@x.com")
    assert seat["limit_source"] == "hard"
    assert seat["usage"]["allowed"] is False
