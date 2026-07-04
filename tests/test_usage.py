"""Feature tests for usage readers — defensive parsers, error classification, cached refresh.

No network: a fake `get` transport returns canned (status, body) per URL.
"""
import json
from datetime import timedelta

import pytest

from acctsw import usage as U
from acctsw import accounts as acct
from acctsw import paths as P
from acctsw.util import now, iso, parse_iso
from tests.conftest import make_codex_blob, make_claude_blob


# --- transport doubles ------------------------------------------------------------------------

def fake_get(mapping):
    """Return a get(url, headers, timeout) that yields mapping[url] = (status, body)."""
    def _get(url, headers, timeout):
        return mapping.get(url, (404, ""))
    return _get


def claude_ok_body(five=10.0, week=50.0, five_reset=None, week_reset=None):
    return json.dumps({
        "five_hour": {"utilization": five, "resets_at": five_reset or iso(now())},
        "seven_day": {"utilization": week, "resets_at": week_reset or iso(now())},
    })


def codex_ok_body(primary=20.0, secondary=70.0, p_reset=None, s_reset=None):
    """Real ChatGPT wham/usage shape: rate_limit.{primary,secondary}_window, reset_at epoch."""
    import calendar
    def to_epoch(iso_s):
        from datetime import datetime
        return calendar.timegm(datetime.fromisoformat(iso_s).utctimetuple())
    return json.dumps({"rate_limit": {
        "limit_reached": primary >= 100 or secondary >= 100,
        "primary_window": {"used_percent": primary, "reset_at": to_epoch(p_reset or iso(now()))},
        "secondary_window": {"used_percent": secondary, "reset_at": to_epoch(s_reset or iso(now()))},
    }})


# --- parsers ----------------------------------------------------------------------------------

def test_parse_claude_shapes():
    w = U.parse_claude({"five_hour": {"utilization": 12, "resets_at": "2026-01-01T00:00:00+00:00"},
                        "seven_day": {"utilization": 80, "resets_at": "2026-01-02T00:00:00+00:00"}})
    assert w["5h"].used_pct == 12.0
    assert w["weekly"].used_pct == 80.0
    assert w["weekly"].resets_at.startswith("2026-01-02")


def test_parse_codex_real_shape():
    body = json.loads(codex_ok_body(primary=20.0, secondary=70.0))
    w = U.parse_codex(body)
    assert w["5h"].used_pct == 20.0
    assert w["weekly"].used_pct == 70.0
    assert w["5h"].resets_at is not None  # epoch → iso


def test_parse_codex_alt_layout_and_epoch_reset():
    body = {"usage": {"five_hour": {"percent": 33, "reset": 1893456000}}}
    w = U.parse_codex(body)
    assert w["5h"].used_pct == 33.0
    assert w["5h"].resets_at.startswith("2030")  # epoch → iso


def test_parse_missing_windows_are_empty():
    w = U.parse_claude({})
    assert w["5h"].used_pct is None and w["5h"].resets_at is None


# --- token extraction -------------------------------------------------------------------------

def test_codex_token_account():
    tok, acc = U.codex_token_account(make_codex_blob("a@x.com"))
    assert tok == "a" and acc == "acc"


def test_account_fingerprint():
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="X9")) == "X9"
    # two seats, same underlying account → same fingerprint (this is the duplicate-account signal)
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="Z")) == \
           U.account_fingerprint("codex", make_codex_blob("a+alias@x.com", account_id="Z"))
    assert U.account_fingerprint("claude", make_claude_blob()) is None   # no claude fingerprint today
    assert U.account_fingerprint("codex", "not json") is None


def test_claude_token():
    assert U.claude_token(make_claude_blob()) == "x"
    assert U.claude_token("not json") is None


def test_refresh_backfills_account_id(ctx):
    """A usage poll self-heals the account fingerprint for a seat that predates the feature."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="ACCT7"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    state = ctx.load_state()
    state.get_seat("codex", "a@x.com").pop("account_id", None)   # simulate a pre-feature seat
    state.save()
    state = ctx.load_state()
    U.refresh(ctx, state, "codex", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body())}))
    assert ctx.load_state().get_seat("codex", "a@x.com")["account_id"] == "ACCT7"


# --- error classification ---------------------------------------------------------------------

@pytest.mark.parametrize("status,err", [(200, None), (401, "unauthorized"),
                                        (403, "unauthorized"), (429, "rate_limited"),
                                        (0, "network"), (500, "http_500")])
def test_classify(status, err):
    assert U._classify(status) == err


def test_fetch_claude_unauthorized_sets_error():
    u = U.fetch_claude("tok", user_agent="claude-code/x",
                       get=fake_get({P.CLAUDE_USAGE_URL: (401, "")}))
    assert u.ok is False and u.error == "unauthorized"


def test_fetch_claude_no_token():
    assert U.fetch_claude(None).error == "no_token"


def test_fetch_codex_ok_parses_windows():
    u = U.fetch_codex("tok", "acc", get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body())}))
    assert u.ok and u.windows["weekly"].used_pct == 70.0


def test_fetch_codex_sends_account_header():
    seen = {}
    def cap(url, headers, timeout):
        seen.update(headers)
        return 200, codex_ok_body()
    U.fetch_codex("tok", "acc-123", get=cap)
    assert seen.get(P.CODEX_ACCOUNT_ID_HEADER) == "acc-123"


def test_fetch_claude_sends_required_headers():
    seen = {}
    def cap(url, headers, timeout):
        seen.update(headers)
        return 200, claude_ok_body()
    U.fetch_claude("tok", user_agent="claude-code/9.9", get=cap)
    assert seen["anthropic-beta"] == P.CLAUDE_OAUTH_BETA
    assert seen["User-Agent"] == "claude-code/9.9"


# --- refresh orchestration --------------------------------------------------------------------

def _seed_two_codex(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="a@x.com")
    # second seat snapshot stored directly
    ctx.snapshot_set("codex", "b@x.com", make_codex_blob("b@x.com"))
    state.upsert_seat("codex", "b@x.com")
    state.save()
    return state


def test_refresh_writes_usage_and_flags_limit(ctx):
    state = _seed_two_codex(ctx)
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    summary = U.refresh(ctx, state, "codex", force=True, get=get)
    assert summary["codex"]["a@x.com"] == "ok"
    seat = state.get_seat("codex", "a@x.com")
    assert seat["usage"]["windows"]["5h"]["used_pct"] == 100.0
    # maxed window → limited_until set to its reset
    assert parse_iso(seat["limited_until"]) is not None


def test_refresh_respects_cache(ctx):
    state = _seed_two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body())})
    U.refresh(ctx, state, "codex", force=True, get=get)
    # immediate second refresh without force → cached (not re-fetched)
    summary = U.refresh(ctx, state, "codex", force=False, get=get, min_seconds=9999)
    assert summary["codex"]["a@x.com"] == "cached"


def test_refresh_rate_limited_records_error(ctx):
    state = _seed_two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    summary = U.refresh(ctx, state, "codex", force=True, get=get)
    assert summary["codex"]["a@x.com"] == "rate_limited"
    # error recorded, limited_until not falsely set
    seat = state.get_seat("codex", "a@x.com")
    assert seat["usage"]["error"] == "rate_limited"
    assert seat["limited_until"] is None


def test_refresh_clears_stale_limit_when_usage_drops(ctx):
    state = _seed_two_codex(ctx)
    # pre-mark limited (proactive source → may be cleared)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=1)), source="usage")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None


def test_refresh_does_not_clear_active_reactive_limit(ctx):
    """A still-future reactive flag must survive a proactive poll showing <100% (anti-flapping)."""
    state = _seed_two_codex(ctx)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="reactive")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_limit_reached_flag_is_authoritative(ctx):
    """rate_limit.limit_reached=True flags the seat even if used_percent < 100."""
    state = _seed_two_codex(ctx)
    reset = iso(now() + timedelta(hours=3))
    body = json.dumps({"rate_limit": {"limit_reached": True,
        "primary_window": {"used_percent": 95, "reset_at": _epoch(reset)},
        "secondary_window": {"used_percent": 40, "reset_at": _epoch(reset)}}})
    U.refresh(ctx, state, "codex", force=True, get=fake_get({P.CODEX_USAGE_URL: (200, body)}))
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_both_windows_maxed_uses_later_reset():
    early = "2026-01-01T00:00:00+00:00"
    late = "2026-01-02T00:00:00+00:00"
    u = U.Usage(ok=True, windows={
        "5h": U.Window(used_pct=100.0, resets_at=early),
        "weekly": U.Window(used_pct=100.0, resets_at=late)})
    assert U._limit_reset(u) == late  # max(), not min()


def test_error_preserves_last_known_windows(ctx):
    state = _seed_two_codex(ctx)
    # first good poll
    U.refresh(ctx, state, "codex", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=42.0))}))
    # then a 429 — windows must be preserved, marked stale
    U.refresh(ctx, state, "codex", force=True, get=fake_get({P.CODEX_USAGE_URL: (429, "")}))
    usage = state.get_seat("codex", "a@x.com")["usage"]
    assert usage["error"] == "rate_limited"
    assert usage["stale"] is True
    assert usage["windows"]["5h"]["used_pct"] == 42.0
    assert usage["error_streak"] == 1


def test_exponential_backoff_skips_retry_after_error(ctx):
    state = _seed_two_codex(ctx)
    # an error sets streak=1 → backoff = base*2; a non-forced refresh within that window is skipped
    U.refresh(ctx, state, "codex", force=True, get=fake_get({P.CODEX_USAGE_URL: (429, "")}))
    summary = U.refresh(ctx, state, "codex", force=False, min_seconds=10,
                        get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body())}))
    assert summary["codex"]["a@x.com"] == "cached"


def test_seat_blob_prefers_live_for_active(ctx):
    state = _seed_two_codex(ctx)  # active = a@x.com
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com").replace('"access_token": "a"',
                                                                  '"access_token": "LIVE"'))
    tok, _ = U.codex_token_account(U._seat_blob(ctx, state, "codex", "a@x.com"))
    assert tok == "LIVE"  # active seat uses live creds, not the snapshot
    # non-active seat uses its snapshot
    tok_b, _ = U.codex_token_account(U._seat_blob(ctx, state, "codex", "b@x.com"))
    assert tok_b == "a"


def test_claude_refresh_path(ctx):
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="c@x.com")
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=20.0, week=60.0))})
    summary = U.refresh(ctx, state, "claude", force=True, get=get, user_agent="claude-code/x")
    assert summary["claude"]["c@x.com"] == "ok"
    assert state.get_seat("claude", "c@x.com")["usage"]["windows"]["weekly"]["used_pct"] == 60.0


def test_refresh_codex_blob_success():
    blob = make_codex_blob("a@x.com")
    def post(url, payload, timeout):
        assert payload["grant_type"] == "refresh_token"
        return 200, json.dumps({"access_token": "NEW", "id_token": "h.e.s", "refresh_token": "rt2"})
    new, err = U.refresh_codex_blob(blob, post=post)
    assert err is None
    import json as _j
    assert _j.loads(new)["tokens"]["access_token"] == "NEW"


def test_refresh_codex_blob_invalidated():
    new, err = U.refresh_codex_blob(make_codex_blob("a@x.com"),
                                    post=lambda u, p, t: (401, '{"error":{"code":"refresh_token_invalidated"}}'))
    assert new is None and err == "invalidated"


def test_refresh_does_not_rotate_token_on_401(ctx):
    """KR-B2: a 401 must NOT auto-rotate the token (codex owns the single-use refresh token)."""
    state = _seed_two_codex(ctx)  # active a@x.com
    before = ctx.cred["codex"].get_live()
    U.refresh(ctx, state, "codex", only="a@x.com", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (401, '{"error":{"code":"token_expired"}}')}))
    assert ctx.cred["codex"].get_live() == before          # live creds untouched
    assert state.get_seat("codex", "a@x.com")["usage"]["error"] == "unauthorized"


def test_claude_user_agent_fallback(monkeypatch):
    import acctsw.usage as um
    monkeypatch.setattr(um.shutil, "which", lambda _: None)
    assert U.claude_user_agent(None) == P.CLAUDE_USER_AGENT_FALLBACK


def _epoch(iso_s):
    import calendar
    from datetime import datetime
    return calendar.timegm(datetime.fromisoformat(iso_s).utctimetuple())
