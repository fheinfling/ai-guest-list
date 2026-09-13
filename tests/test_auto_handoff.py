"""Desktop handoff races use fake credentials and provider responses only."""
import json
from datetime import timedelta

import pytest

from acctsw import accounts, bridge, usage
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.conftest import make_codex_blob, make_claude_blob


def seed(ctx, tool="codex", spare_count=2):
    state = ctx.load_state()
    for index in range(spare_count + 1):
        email = f"seat{index}@test.example"
        blob = (make_codex_blob(email) if tool == "codex" else
                make_claude_blob().replace('"accessToken": "x"', f'"accessToken": "x{index}"'))
        accounts.add(ctx, state, tool, email=email, blob=blob)
    active = "seat0@test.example"
    switch(ctx, state, tool, active, sync=False)
    usage.store_fetch(state, tool, active, usage.Usage(
        ok=True, fetched_at=iso(now()), windows={"5h": usage.Window(
            used_pct=100, resets_at=iso(now() + timedelta(hours=1)))}),
        blob=ctx.snapshot_get(tool, active))
    state.save()
    return {tool: {active: "ok"}}


def probe(monkeypatch, *, after=None, error_seat=None):
    real = usage.refresh_live
    calls = []

    def refresh(ctx, tool=None, *, only=None, **kwargs):
        calls.append(only)
        status = 429 if only == error_seat else 200
        payload = ({"rate_limit": {"primary_window": {"used_percent": 10}}}
                   if tool == "codex" else {"five_hour": {"utilization": 10}})
        result = real(ctx, tool, only=only, get=lambda *_: (status, json.dumps(payload)),
                      user_agent="test", **kwargs)
        if after:
            after(ctx, tool, only)
        return result

    monkeypatch.setattr(usage, "refresh_live", refresh)
    return calls


@pytest.mark.parametrize("blocked", ["duplicate", "backoff", "new_error"])
def test_handoff_skips_unusable_first_spare(ctx, monkeypatch, blocked):
    summary = seed(ctx)
    state = ctx.load_state()
    first = state.get_seat("codex", "seat1@test.example")
    if blocked == "duplicate":
        first["account_id"] = state.get_seat("codex", "seat0@test.example")["account_id"]
    if blocked == "backoff":
        first["usage"] = {"error": "rate_limited", "error_streak": 1,
                          "last_attempted_at": iso(now())}
    state.save()
    calls = probe(monkeypatch, error_seat="seat1@test.example" if blocked == "new_error" else None)
    result = bridge._auto_switch_after_usage(ctx, summary)
    assert result["to"] == "seat2@test.example"
    if blocked != "new_error":
        assert calls == ["seat2@test.example"]


@pytest.mark.parametrize("change", ["outgoing_login", "landing_login", "toggle", "supervisor"])
def test_handoff_rechecks_changes_during_probe(ctx, monkeypatch, change):
    summary = seed(ctx, spare_count=1)

    def race(ctx, tool, email):
        state = ctx.load_state()
        if change == "outgoing_login":
            ctx.cred[tool].set_live(make_codex_blob("external@test.example"))
        elif change == "landing_login":
            accounts.add(ctx, state, tool, email=email,
                         blob=make_codex_blob(email, account_id="new-subscription"))
        elif change == "toggle":
            state.set_setting("auto_switch", False)
            state.save()
        else:
            monkeypatch.setattr(bridge.session_mod, "active_session", lambda *_: {"email": "seat0@test.example"})

    probe(monkeypatch, after=race)
    assert bridge._auto_switch_after_usage(ctx, summary) is None
    if change == "outgoing_login":
        assert ctx.cred["codex"].email_of(ctx.cred["codex"].get_live()) == "external@test.example"
    elif change != "landing_login":
        assert ctx.load_state().active("codex") == "seat0@test.example"


def test_claude_handoff_checks_identity_and_switches(ctx, monkeypatch):
    summary = seed(ctx, "claude", spare_count=1)
    from acctsw.identity import ClaudeLiveIdentity
    monkeypatch.setattr(bridge.identity_mod, "claude_live_identity", lambda c:
                        ClaudeLiveIdentity(blob=c.cred["claude"].get_live(), email="seat0@test.example"))
    probe(monkeypatch)
    result = bridge._auto_switch_after_usage(ctx, summary)
    assert result["tool"] == "claude" and result["to"] == "seat1@test.example"


def test_forced_display_refresh_respects_provider_error_backoff(ctx):
    seed(ctx, spare_count=1)
    state = ctx.load_state()
    state.set_usage("codex", "seat1@test.example", {
        "error": "rate_limited", "error_streak": 1, "last_attempted_at": iso(now())})
    state.save()
    result = usage.refresh_live(ctx, "codex", only="seat1@test.example", force=True,
                              get=lambda *_: pytest.fail("must respect provider backoff"))
    assert result["codex"]["seat1@test.example"] == "cached"


@pytest.mark.parametrize("reason", ["disabled", "nearly_full", "no_spare", "expired"])
def test_handoff_requires_a_current_limit_and_enabled_spare(ctx, monkeypatch, reason):
    summary = seed(ctx, spare_count=0 if reason == "no_spare" else 1)
    state = ctx.load_state()
    if reason == "disabled":
        state.set_setting("auto_switch", False)
    elif reason == "nearly_full":
        usage.store_fetch(state, "codex", "seat0@test.example", usage.Usage(
            ok=True, fetched_at=iso(now()), windows={"5h": usage.Window(used_pct=97)}),
            blob=ctx.snapshot_get("codex", "seat0@test.example"))
    elif reason == "expired":
        state.set_limited_until("codex", "seat0@test.example", iso(now() - timedelta(seconds=1)),
                                source="usage")
    state.save()
    calls = probe(monkeypatch)
    assert bridge._auto_switch_after_usage(ctx, summary) is None
    assert calls == []
