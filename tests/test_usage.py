"""Feature tests for usage readers — defensive parsers, error classification, cached refresh.

No network: a fake `get` transport returns canned (status, body) per URL.
"""
import json
import threading
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


def codex_ok_body(primary=20.0, secondary=70.0, p_reset=None, s_reset=None, *,
                  allowed=None, plan_type=None, has_credits=None, spend_control_reached=None,
                  reached_type=None, reset_after_seconds=None, omit_reset_at=False):
    """Real ChatGPT wham/usage shape: rate_limit.{primary,secondary}_window, reset_at epoch.

    The keyword-only extras mirror the rest of the live payload (plan_type, rate_limit.allowed,
    credits.has_credits, spend_control.reached, rate_limit_reached_type and the windows' RELATIVE
    reset_after_seconds as a (primary, secondary) pair). Each is emitted only when given, so every
    pre-existing caller still gets exactly the old body.
    """
    import calendar
    def to_epoch(iso_s):
        from datetime import datetime
        return calendar.timegm(datetime.fromisoformat(iso_s).utctimetuple())
    pw = {"used_percent": primary}
    sw = {"used_percent": secondary}
    if not omit_reset_at:
        pw["reset_at"] = to_epoch(p_reset or iso(now()))
        sw["reset_at"] = to_epoch(s_reset or iso(now()))
    if reset_after_seconds is not None:
        pw["reset_after_seconds"], sw["reset_after_seconds"] = reset_after_seconds
    rate = {"limit_reached": primary >= 100 or secondary >= 100,
            "primary_window": pw, "secondary_window": sw}
    if allowed is not None:
        rate["allowed"] = allowed
    body = {"rate_limit": rate}
    if plan_type is not None:
        body["plan_type"] = plan_type
    if reached_type is not None:
        body["rate_limit_reached_type"] = reached_type
    if has_credits is not None:
        body["credits"] = {"has_credits": has_credits, "unlimited": False, "balance": None}
    if spend_control_reached is not None:
        body["spend_control"] = {"reached": spend_control_reached, "individual_limit": None}
    return json.dumps(body)


def codex_credits_depleted_body(*, plan_type="team",
                                reached_type="workspace_member_credits_depleted",
                                allowed=False, windows_null=True):
    """The credits-depleted payload (verified live): the windows are NULL, so there is no percentage
    to read and only allowed/rate_limit_reached_type reveal that the seat cannot be used."""
    return json.dumps({
        "plan_type": plan_type,
        "rate_limit": {
            "allowed": allowed, "limit_reached": False,
            "primary_window": None if windows_null else {"used_percent": 0},
            "secondary_window": None if windows_null else {"used_percent": 0},
        },
        "rate_limit_reached_type": reached_type,
        "credits": {"has_credits": False, "unlimited": False, "overage_limit_reached": False,
                    "balance": None},
        "spend_control": {"reached": False, "individual_limit": None},
    })


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


def test_parse_codex_labels_pro_lite_primary_as_weekly_from_duration():
    """Pro Lite has one primary 7d window; its position must not make it look like a 5h limit."""
    body = {
        "plan_type": "self_serve_business_prolite",
        "rate_limit": {
            "allowed": True,
            "primary_window": {
                "used_percent": 3,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 602784,
                "reset_at": 1789911732,
            },
            "secondary_window": None,
        },
    }
    w = U.parse_codex(body)
    assert w["5h"].used_pct is None
    assert w["weekly"].used_pct == 3.0
    assert w["weekly"].resets_at is not None


def test_parse_codex_labels_common_team_windows_from_duration():
    body = {"rate_limit": {
        "primary_window": {"used_percent": 4, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 8, "limit_window_seconds": 604800},
    }}
    w = U.parse_codex(body)
    assert w["5h"].used_pct == 4.0
    assert w["weekly"].used_pct == 8.0


def test_parse_codex_retains_unknown_duration_without_labeling_it_as_5h():
    body = {"rate_limit": {
        "primary_window": {"used_percent": 91, "limit_window_seconds": 86400},
        "secondary_window": None,
    }}
    w = U.parse_codex(body)
    assert w["5h"].used_pct is None
    assert w["weekly"].used_pct is None
    assert w["window_86400s"].used_pct == 91.0


def test_codex_unknown_duration_at_100_percent_still_blocks():
    body = json.dumps({"rate_limit": {
        "primary_window": {"used_percent": 100, "limit_window_seconds": 3600,
                           "reset_after_seconds": 120},
        "secondary_window": None,
    }})
    u = _codex_usage(body)
    assert u.allowed is None
    assert u.windows["window_3600s"].used_pct == 100.0
    assert u.windows["window_3600s"].resets_at is not None
    assert U._is_limited(u) is True
    assert U._confirmed_healthy(u) is False


def test_parse_codex_malformed_explicit_duration_does_not_use_legacy_label():
    body = {"rate_limit": {
        "primary_window": {"used_percent": 100, "limit_window_seconds": "unknown"},
        "secondary_window": None,
    }}
    w = U.parse_codex(body)
    assert w["5h"].used_pct is None
    assert w["primary_window"].used_pct == 100.0


def test_parse_codex_alt_layout_and_epoch_reset():
    body = {"usage": {"five_hour": {"percent": 33, "reset": 1893456000}}}
    w = U.parse_codex(body)
    assert w["5h"].used_pct == 33.0
    assert w["5h"].resets_at.startswith("2030")  # epoch → iso


def test_parse_missing_windows_are_empty():
    w = U.parse_claude({})
    assert w["5h"].used_pct is None and w["5h"].resets_at is None


@pytest.mark.parametrize("payload", [{}, {"rate_limit": "nope"}, {"credits": []}])
def test_parse_codex_flags_tolerates_garbage(payload):
    """The flag parser must never raise on an unexpected shape — it degrades to all-unknown."""
    f = U.parse_codex_flags(payload)
    assert f["plan_type"] is None and f["allowed"] is None and f["reached_type"] is None
    assert f["spend_control_reached"] is None and f["has_credits"] is None
    assert f["reset_after_seconds"] == {"5h": None, "weekly": None}


# --- token extraction -------------------------------------------------------------------------

def test_codex_token_account():
    tok, acc = U.codex_token_account(make_codex_blob("a@x.com", account_id="acc"))
    assert tok == "a" and acc == "acc"


def test_account_fingerprint():
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="X9")) == "X9"
    # two seats, same underlying account → same fingerprint (this is the duplicate-account signal)
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="Z")) == \
           U.account_fingerprint("codex", make_codex_blob("a+alias@x.com", account_id="Z"))
    assert U.account_fingerprint("claude", make_claude_blob()) is None   # no claude fingerprint today
    assert U.account_fingerprint("codex", "not json") is None


def test_fingerprint_is_the_user_not_the_workspace():
    """Team/Business colleagues share ONE chatgpt_account_id but hold separate rate-limit windows,
    so the fingerprint must be the person — otherwise two real seats read as one quota."""
    mine = make_codex_blob("me@corp.com", account_id="ws-1", user_id="user-me")
    yours = make_codex_blob("you@corp.com", account_id="ws-1", user_id="user-you")
    assert U.account_fingerprint("codex", mine) == "user-me"
    assert U.account_fingerprint("codex", mine) != U.account_fingerprint("codex", yours)
    # the workspace id is still available to whoever needs the subscription, not the person
    assert U.account_workspace("codex", mine) == U.account_workspace("codex", yours) == "ws-1"
    assert U.account_workspace("claude", make_claude_blob()) is None
    assert U.account_workspace("codex", "not json") is None


def test_fingerprint_falls_back_to_account_id_without_user_claim():
    """Old blobs and API-key auth carry no user id: keep the pre-user-id behaviour there."""
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="X9")) == "X9"
    # one person signed in twice still collapses to one fingerprint, via the shared account id
    assert U.account_fingerprint("codex", make_codex_blob("a@x.com", account_id="Z")) == \
           U.account_fingerprint("codex", make_codex_blob("a+alias@x.com", account_id="Z"))


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


def test_poll_restamps_a_pre_upgrade_workspace_fingerprint_without_a_phantom_move(ctx):
    """Seats added before the fingerprint became the user id hold the WORKSPACE id in account_id and
    no workspace_id at all. The first poll must adopt both ids quietly — reading that swap as a
    re-subscription would wipe every existing seat's rest on upgrade."""
    ctx.cred["codex"].set_live(make_codex_blob("me@corp.com", account_id="ws-1", user_id="user-me"))
    acct.add(ctx, ctx.load_state(), "codex", email="me@corp.com")
    state = ctx.load_state()
    seat = state.get_seat("codex", "me@corp.com")
    seat["account_id"] = "ws-1"            # what the old fingerprint stamped
    seat.pop("workspace_id", None)         # ...and it knew no workspace id
    state.save()

    state = ctx.load_state()
    state.set_limited_until("codex", "me@corp.com", iso(now() + timedelta(hours=4)), source="hard")
    U.refresh(ctx, state, "codex", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5, secondary=5))}))
    seat = state.get_seat("codex", "me@corp.com")
    assert seat["account_id"] == "user-me" and seat["workspace_id"] == "ws-1"
    assert seat["limited_until"] is not None      # the rest survived
    assert "moved_note" not in state.data


def test_successful_poll_rereads_claude_plan(ctx):
    """A Max→Pro subscription change must not stay frozen at the add-time plan."""
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="c@x.com")
    assert state.get_seat("claude", "c@x.com")["plan"] == "Max"

    ctx.cred["claude"].set_live(make_claude_blob("pro"))
    U.refresh(ctx, state, "claude", force=True, user_agent="claude-code/x",
              get=fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body())}))
    assert state.get_seat("claude", "c@x.com")["plan"] == "Pro"


def test_successful_poll_detects_new_subscription_under_same_email(ctx):
    """A changed provider account id under one email is a new subscription: the old hard rest and
    auth backoff must be cleared, and the snapshot notice must tell the UI what happened."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="old-sub"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="a@x.com")
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=5)), source="hard")
    state.get_seat("codex", "a@x.com")["usage"] = {
        "error": "forbidden", "error_streak": 12, "stale": True,
        "fetched_at": None, "last_attempted_at": iso(now()),
    }
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="new-sub"))

    U.refresh(ctx, state, "codex", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5, secondary=5))}))
    seat = state.get_seat("codex", "a@x.com")
    assert seat["account_id"] == "new-sub"
    assert seat["limited_until"] is None and seat["limit_source"] is None
    assert seat["usage"]["error"] is None and seat["usage"]["error_streak"] == 0
    assert "new codex subscription" in state.data["moved_note"]
    from acctsw.bridge import snapshot_state
    assert "new codex subscription" in snapshot_state(ctx)["moved_note"]


def test_new_subscription_success_does_not_touch_discarded_usage(ctx, monkeypatch):
    """The fresh successful usage dict already clears error/streak. Account-change handling must
    not mutate the old usage object that `set_usage` immediately replaces wholesale."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="old-sub"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="a@x.com")
    state.get_seat("codex", "a@x.com")["usage"] = {
        "error": "forbidden", "error_streak": 4, "stale": True,
    }
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="new-sub"))

    def dead_write(*_args, **_kwargs):
        raise AssertionError("old usage is discarded; refreshing it here is a dead write")

    monkeypatch.setattr(acct, "_creds_refreshed", dead_write)
    U.refresh(ctx, state, "codex", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5, secondary=5))}))

    usage = state.get_seat("codex", "a@x.com")["usage"]
    assert usage["error"] is None and usage["error_streak"] == 0


# --- error classification ---------------------------------------------------------------------

@pytest.mark.parametrize("status,err", [(200, None), (401, "unauthorized"),
                                        (403, "forbidden"), (429, "rate_limited"),
                                        (0, "network"), (500, "http_500")])
def test_classify(status, err):
    assert U._classify(status) == err


def test_fetch_claude_unauthorized_sets_error():
    u = U.fetch_claude("tok", user_agent="claude-code/x",
                       get=fake_get({P.CLAUDE_USAGE_URL: (401, "")}))
    assert u.ok is False and u.error == "unauthorized"


def test_fetch_claude_forbidden_marks_lost_entitlement():
    """A 403 is a revoked/downgraded subscription, not routine parked-seat token expiry."""
    u = U.fetch_claude("tok", user_agent="claude-code/x",
                       get=fake_get({P.CLAUDE_USAGE_URL: (403, "")}))
    assert u.ok is False and u.error == "forbidden"


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
    """A still-future reactive flag must survive a poll in the lag band (≥FALSE_ALARM_MAX_PCT):
    the endpoint can trail a real limit by a few percent, so a near-max reading is inconclusive."""
    state = _seed_two_codex(ctx)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="reactive")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=95.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_refresh_clears_reactive_limit_when_confirmed_healthy(ctx):
    """A CONFIRMED-healthy fetch (clear headroom, no limit flag) cannot be endpoint lag — it clears
    a stale reactive flag so a false positive never blocks launches with 'all seats resting'."""
    state = _seed_two_codex(ctx)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="reactive")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None


def _reactive_seat(ctx):
    state = _seed_two_codex(ctx)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="reactive")
    state.save()
    return state


def test_apply_limit_keeps_near_max_reactive_when_trusting_lag(ctx):
    """Fix B (in-session/poll default): a 95%-used ok fetch is inconclusive lag — with
    trust_reactive_lag=True the reactive rest is kept, so a mid-run poll never ping-pongs."""
    state = _reactive_seat(ctx)
    u = U.Usage(ok=True, windows={"5h": U.Window(used_pct=95.0), "weekly": U.Window(used_pct=20.0)})
    U.store_fetch(state, "codex", "a@x.com", u, at=now())  # default trust_reactive_lag=True
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_apply_limit_clears_near_max_reactive_at_cold_start(ctx):
    """Fix B (cold start): the same 95%-used ok fetch (below the real 100% limit → credit exists)
    clears the weakest-evidence reactive guess when trust_reactive_lag=False, so a stale false
    positive doesn't kill a launch that still has credit."""
    state = _reactive_seat(ctx)
    u = U.Usage(ok=True, windows={"5h": U.Window(used_pct=95.0), "weekly": U.Window(used_pct=20.0)})
    U.store_fetch(state, "codex", "a@x.com", u, at=now(), trust_reactive_lag=False)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None


@pytest.mark.parametrize("trust", [True, False])
def test_apply_limit_genuinely_limited_rests_under_both(ctx, trust):
    """A genuinely-limited fetch (window ≥100% / limit_reached) rests the seat under BOTH modes —
    the cold-start relaxation only touches the near-max (below-limit) reactive band."""
    state = _reactive_seat(ctx)
    reset = iso(now() + timedelta(hours=3))
    u = U.Usage(ok=True, limit_reached=True,
                windows={"5h": U.Window(used_pct=100.0, resets_at=reset)})
    U.store_fetch(state, "codex", "a@x.com", u, at=now(), trust_reactive_lag=trust)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_refresh_poll_path_keeps_near_max_reactive_flag(ctx):
    """The menubar/poll path (refresh) uses the default trust_reactive_lag=True, so a 95% reading
    keeps the reactive rest — the cold-start relaxation is threaded ONLY through the launcher's
    pre-launch sweep, never through polling."""
    state = _reactive_seat(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=95.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", only="a@x.com", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_refresh_never_clears_hard_limit_early(ctx):
    """A ``hard`` flag (tool-side billing banner, e.g. codex out of credits) survives even a fully
    healthy poll: the usage windows can look fine while the workspace has no credits."""
    state = _seed_two_codex(ctx)
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="hard")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=5.0, secondary=5.0))})
    U.refresh(ctx, state, "codex", force=True, get=get)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"


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


def test_maxed_window_without_reset_still_rests_seat(ctx):
    """A window at 100% with NO reset timestamp (e.g. a workspace out of credits) must still rest
    the seat — otherwise the display shows 100% while choose() calls it available and the launcher
    picks a maxed seat. With no reset anywhere, the estimate is now + DEFAULT_COOLDOWN."""
    from acctsw.selection import choose
    state = _seed_two_codex(ctx)
    body = json.dumps({"rate_limit": {
        "primary_window": {"used_percent": 100},
        "secondary_window": {"used_percent": 40}}})   # maxed, no reset_at, no limit_reached
    U.refresh(ctx, state, "codex", only="a@x.com", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, body)}))
    seat = state.get_seat("codex", "a@x.com")
    until = parse_iso(seat["limited_until"])
    assert until is not None and seat["limit_source"] == "usage"
    assert timedelta(hours=4) < (until - now()) <= U.DEFAULT_COOLDOWN
    assert choose(state, "codex").email == "b@x.com"   # maxed seat not picked


def test_limit_reached_without_any_reset_still_rests_seat(ctx):
    """The authoritative limit_reached flag with NO reset anywhere must rest the seat too."""
    state = _seed_two_codex(ctx)
    body = json.dumps({"rate_limit": {"limit_reached": True,
        "primary_window": {"used_percent": 50},
        "secondary_window": {"used_percent": 50}}})
    U.refresh(ctx, state, "codex", force=True, get=fake_get({P.CODEX_USAGE_URL: (200, body)}))
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "usage"


def test_fallback_rest_estimate_is_stable_across_polls(ctx):
    """A no-reset limited fetch stamps its DEFAULT_COOLDOWN estimate ONCE: later polls with the
    same shape must NOT re-anchor (slide) it — a receding stamp makes the launcher's wait target
    creep forward forever, re-notifying on every poll and never reaching its moment of truth."""
    state = _seed_two_codex(ctx)
    body = json.dumps({"rate_limit": {
        "primary_window": {"used_percent": 100},
        "secondary_window": {"used_percent": 40}}})   # maxed, no reset_at
    get = fake_get({P.CODEX_USAGE_URL: (200, body)})
    U.refresh(ctx, state, "codex", only="a@x.com", force=True, get=get)
    first = state.get_seat("codex", "a@x.com")["limited_until"]
    U.refresh(ctx, state, "codex", only="a@x.com", force=True, get=get)
    assert state.get_seat("codex", "a@x.com")["limited_until"] == first


def test_maxed_usage_poll_does_not_downgrade_hard_limit(ctx):
    """A still-future ``hard`` flag must survive a poll showing a maxed window WITH a short reset:
    re-stamping it as clearable source=\"usage\" would let the next healthy-looking fetch free a
    creditless seat — the exact ping-pong the hard source exists to prevent."""
    state = _seed_two_codex(ctx)
    hard_until = iso(now() + timedelta(hours=5))
    state.set_limited_until("codex", "a@x.com", hard_until, source="hard")
    reset = iso(now() + timedelta(minutes=10))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    U.refresh(ctx, state, "codex", only="a@x.com", force=True, get=get)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limit_source"] == "hard" and seat["limited_until"] == hard_until


# --- authoritative non-percentage flags (credits-depleted case) --------------------------------

def _codex_usage(body):
    return U.fetch_codex("tok", "acc", get=fake_get({P.CODEX_USAGE_URL: (200, body)}))


def test_codex_allowed_false_is_limited(ctx):
    """rate_limit.allowed=false with NULL windows: percentages are absent, so this flag is the ONLY
    evidence the seat is out. With no reset in the payload the rest is the default cooldown."""
    u = _codex_usage(codex_credits_depleted_body(reached_type=None))
    assert u.ok and u.allowed is False and u.reached_type is None
    assert U._is_limited(u) is True

    state = _seed_two_codex(ctx)
    at = now()
    U.store_fetch(state, "codex", "a@x.com", u, at=at)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limit_source"] == "usage"
    assert abs((parse_iso(seat["limited_until"]) - (at + U.DEFAULT_COOLDOWN)).total_seconds()) < 5


def test_codex_reached_type_is_limited_and_not_healthy():
    """rate_limit_reached_type alone (allowed still true, windows null) rests the seat and can never
    be read as confirmed-healthy — there is no window to prove headroom with."""
    u = _codex_usage(codex_credits_depleted_body(allowed=True))
    assert u.reached_type == "workspace_member_credits_depleted"
    assert U._is_limited(u) is True
    assert U._confirmed_healthy(u) is False


def test_codex_spend_control_reached_is_limited():
    """A reached spend control blocks work even while both windows look perfectly healthy."""
    u = _codex_usage(codex_ok_body(primary=5.0, secondary=5.0, allowed=True,
                                   spend_control_reached=True))
    assert u.spend_control_reached is True
    assert U._is_limited(u) is True
    assert U._confirmed_healthy(u) is False


def test_codex_has_credits_false_alone_is_not_limited(ctx):
    """THE regression that matters: credits.has_credits is false on perfectly healthy subscription
    accounts (real payload: allowed true, 0% / 57% used). It must never rest a seat."""
    u = _codex_usage(codex_ok_body(primary=0, secondary=57, allowed=True, plan_type="team",
                                   has_credits=False))
    assert u.has_credits is False and u.plan_type == "team" and u.allowed is True
    assert U._is_limited(u) is False
    assert U._confirmed_healthy(u) is True

    state = _seed_two_codex(ctx)
    U.store_fetch(state, "codex", "a@x.com", u, at=now())
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None


def test_codex_reset_after_seconds_used_when_reset_at_missing():
    """A window may carry only its RELATIVE unlock; it is turned into an absolute stamp so a maxed
    seat rests until its real reset instead of a blind DEFAULT_COOLDOWN estimate."""
    at = now()
    u = _codex_usage(codex_ok_body(primary=100.0, secondary=57.0, omit_reset_at=True,
                                   reset_after_seconds=(1800, 320961)))
    five = parse_iso(u.windows["5h"].resets_at)
    assert abs((five - (at + timedelta(seconds=1800))).total_seconds()) < 5
    assert parse_iso(u.windows["weekly"].resets_at) > five
    assert U._limit_reset(u) == u.windows["5h"].resets_at   # the maxed window's own reset


def test_codex_reset_after_seconds_follows_duration_label():
    """The relative reset must stay attached when a primary window is labeled weekly."""
    at = now()
    body = json.dumps({"rate_limit": {
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604800,
                           "reset_after_seconds": 120},
        "secondary_window": None,
    }})
    u = _codex_usage(body)
    assert u.windows["5h"].resets_at is None
    weekly = parse_iso(u.windows["weekly"].resets_at)
    assert abs((weekly - (at + timedelta(seconds=120))).total_seconds()) < 5
    assert U._limit_reset(u) == u.windows["weekly"].resets_at


def test_codex_reached_type_accepts_live_object_shape():
    body = json.dumps({
        "rate_limit": {"allowed": False, "primary_window": None, "secondary_window": None},
        "rate_limit_reached_type": {
            "type": "workspace_member_credits_depleted",
            "details": None,
        },
    })
    u = _codex_usage(body)
    assert u.reached_type == "workspace_member_credits_depleted"
    assert U._is_limited(u) is True


def test_usage_snapshot_carries_plan_type_and_reached_type(ctx):
    """The new flags must round-trip into the persisted seat so state-only callers can see them."""
    assert set(U.Usage().to_dict()) >= {"plan_type", "allowed", "reached_type",
                                        "spend_control_reached", "has_credits"}
    state = _seed_two_codex(ctx)
    U.refresh(ctx, state, "codex", only="a@x.com", force=True,
              get=fake_get({P.CODEX_USAGE_URL: (200, codex_credits_depleted_body())}))
    usage = state.get_seat("codex", "a@x.com")["usage"]
    assert usage["plan_type"] == "team"
    assert usage["reached_type"] == "workspace_member_credits_depleted"
    assert usage["allowed"] is False and usage["has_credits"] is False
    assert U.snapshot_says_out(usage) is True


def test_store_fetch_tolerates_snapshot_without_new_fields(ctx):
    """A seat persisted before these fields existed has none of the keys — nothing may KeyError."""
    state = _seed_two_codex(ctx)
    state.get_seat("codex", "a@x.com")["usage"] = {
        "ok": True, "error": None, "limit_reached": False, "fetched_at": iso(now()),
        "windows": {"5h": {"used_pct": 10.0, "resets_at": None}},
    }
    old = state.get_seat("codex", "a@x.com")["usage"]
    assert U.snapshot_says_out(old) is False

    u = _codex_usage(codex_ok_body(primary=5.0, secondary=5.0))
    assert U.store_fetch(state, "codex", "a@x.com", u, at=now()) == "ok"
    usage = state.get_seat("codex", "a@x.com")["usage"]
    assert usage["plan_type"] is None and usage["allowed"] is None
    assert state.get_seat("codex", "a@x.com")["limited_until"] is None

    failed = U.Usage(ok=False, error="network", fetched_at=iso(now()))
    assert U.store_fetch(state, "codex", "a@x.com", failed, at=now()) == "network"


def test_snapshot_says_out_flags():
    assert U.snapshot_says_out({}) is False
    assert U.snapshot_says_out({"limit_reached": True}) is True
    assert U.snapshot_says_out({"allowed": False}) is True
    assert U.snapshot_says_out({"allowed": True}) is False
    assert U.snapshot_says_out({"reached_type": "workspace_member_credits_depleted"}) is True
    assert U.snapshot_says_out({"reached_type": ""}) is False
    assert U.snapshot_says_out({"spend_control_reached": True}) is True
    assert U.snapshot_says_out({"spend_control_reached": False}) is False
    # has_credits is false on healthy accounts — it is not an out-signal
    assert U.snapshot_says_out({"allowed": True, "has_credits": False}) is False


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


def test_error_preserves_last_success_time_for_honest_usage_age(ctx):
    """A failed fetch must not timestamp frozen windows as freshly fetched; their age starts at the
    last successful response while a separate attempt time continues driving retry backoff."""
    state = _seed_two_codex(ctx)
    success_at = now() - timedelta(minutes=20)
    good = U.Usage(ok=True, fetched_at=iso(success_at),
                   windows={"5h": U.Window(used_pct=42.0)})
    U.store_fetch(state, "codex", "a@x.com", good, at=success_at,
                  blob=ctx.cred["codex"].get_live())
    failed_at = now()
    failed = U.Usage(ok=False, error="network", fetched_at=iso(failed_at))
    U.store_fetch(state, "codex", "a@x.com", failed, at=failed_at)

    usage = state.get_seat("codex", "a@x.com")["usage"]
    assert usage["fetched_at"] == iso(success_at)
    assert usage["last_attempted_at"] == iso(failed_at)
    view = acct.list_seats(state, "codex", at=failed_at)[0]
    assert view["usage_fetched_at"] == iso(success_at)
    assert 1199 <= view["usage_age_s"] <= 1201
    assert view["usage_stale"] is True and view["usage_unknown"] is True


def test_exponential_backoff_skips_retry_after_error(ctx):
    state = _seed_two_codex(ctx)
    # an error sets streak=1 → backoff = base*2; a non-forced refresh within that window is skipped
    U.refresh(ctx, state, "codex", force=True, get=fake_get({P.CODEX_USAGE_URL: (429, "")}))
    summary = U.refresh(ctx, state, "codex", force=False, min_seconds=10,
                        get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body())}))
    assert summary["codex"]["a@x.com"] == "cached"


def test_active_seat_error_backoff_is_capped_for_prompt_recovery():
    """A recovering on-floor seat must revalidate within five minutes, not wait out the 1h cap."""
    usage = {"error_streak": 20}
    assert U._backoff_seconds(usage, 150) == U.MAX_BACKOFF_SECONDS
    assert U._backoff_seconds(usage, 150, active=True) == U.ACTIVE_MAX_BACKOFF_SECONDS == 300


def test_visible_active_cadence_does_not_shorten_error_backoff():
    usage = {"error_streak": 1}
    assert U._backoff_seconds(usage, P.USAGE_ACTIVE_REFRESH_SECONDS, active=True) == 300


def test_refresh_live_uses_30s_active_and_gentle_parked_cadence(ctx):
    state = _seed_two_codex(ctx)
    attempted = iso(now() - timedelta(seconds=31))
    for seat in state.accounts("codex").values():
        seat["usage"] = {"last_attempted_at": attempted, "error_streak": 0}
    state.save()
    calls = []

    def get(url, headers, timeout):
        calls.append(headers["Authorization"])
        return 200, codex_ok_body()

    summary = U.refresh_live(ctx, "codex", get=get)
    assert summary["codex"]["a@x.com"] == "ok"       # active: due after 30s
    assert summary["codex"]["b@x.com"] == "cached"  # parked: still inside 150s floor
    assert len(calls) == 1


def test_seat_blob_prefers_live_for_active(ctx):
    state = _seed_two_codex(ctx)  # active = a@x.com
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com").replace('"access_token": "a"',
                                                                  '"access_token": "LIVE"'))
    tok, _ = U.codex_token_account(U._seat_blob(ctx, state, "codex", "a@x.com"))
    assert tok == "LIVE"  # active seat uses live creds, not the snapshot
    # non-active seat uses its snapshot
    tok_b, _ = U.codex_token_account(U._seat_blob(ctx, state, "codex", "b@x.com"))
    assert tok_b == "a"


def test_seat_blob_rejects_mismatched_live_identity_for_active_codex(ctx):
    state = _seed_two_codex(ctx)  # state still says a@x.com is active
    wrong = make_codex_blob("b@x.com").replace('"access_token": "a"',
                                                '"access_token": "WRONG"')
    ctx.cred["codex"].set_live(wrong)
    blob = U._seat_blob(ctx, state, "codex", "a@x.com")
    assert ctx.cred["codex"].email_of(blob) == "a@x.com"
    assert U.codex_token_account(blob)[0] == "a"  # the rightful private snapshot


def test_seat_blob_uses_fresh_private_snapshot_for_supervised_codex(ctx, monkeypatch):
    from acctsw import session
    email = "a@x.com"
    stale = make_codex_blob(email).replace('"access_token": "a"',
                                             '"access_token": "stale"')
    fresh = make_codex_blob(email).replace('"access_token": "a"',
                                             '"access_token": "fresh"')
    ctx.cred["codex"].set_live(stale)
    acct.add(ctx, ctx.load_state(), "codex", email=email)
    ctx.snapshot_set("codex", email, fresh)
    state = ctx.load_state()
    monkeypatch.setattr(session, "active_session",
                        lambda _data_dir, _tool: {"email": email, "pid": 1, "started_at": "x"})
    assert U.codex_token_account(U._seat_blob(ctx, state, "codex", email))[0] == "fresh"


def test_claude_refresh_path(ctx):
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="c@x.com")
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=20.0, week=60.0))})
    summary = U.refresh(ctx, state, "claude", force=True, get=get, user_agent="claude-code/x")
    assert summary["claude"]["c@x.com"] == "ok"
    assert state.get_seat("claude", "c@x.com")["usage"]["windows"]["weekly"]["used_pct"] == 60.0


def test_refresh_live_fetches_outside_lock_and_commits_fresh_state(ctx, monkeypatch):
    state = _seed_two_codex(ctx)
    held = {"value": False}
    real_locked = ctx.locked

    from contextlib import contextmanager
    @contextmanager
    def tracked_locked():
        with real_locked():
            held["value"] = True
            try:
                yield
            finally:
                held["value"] = False

    def get(url, headers, timeout):
        assert held["value"] is False
        return 200, codex_ok_body(primary=31, secondary=62)

    monkeypatch.setattr(ctx, "locked", tracked_locked)
    summary = U.refresh_live(ctx, "codex", only=state.active("codex"), force=True, get=get)
    assert summary["codex"][state.active("codex")] == "ok"
    stored = ctx.load_state().get_seat("codex", state.active("codex"))["usage"]
    assert stored["windows"]["5h"]["used_pct"] == 31


def test_refresh_live_rejects_result_after_credentials_change(ctx):
    state = _seed_two_codex(ctx)
    email = state.active("codex")

    def get(url, headers, timeout):
        changed = make_codex_blob(email).replace('"access_token": "a"',
                                                  '"access_token": "new"')
        ctx.cred["codex"].set_live(changed)
        return 200, codex_ok_body(primary=88)

    summary = U.refresh_live(ctx, "codex", only=email, force=True, get=get)
    assert summary["codex"][email] == "stale"
    assert ctx.load_state().get_seat("codex", email)["usage"]["last_attempted_at"] is None


def test_refresh_live_rejects_result_older_than_stored_attempt(ctx):
    state = _seed_two_codex(ctx)
    email = state.active("codex")

    def get(url, headers, timeout):
        with ctx.locked():
            current = ctx.load_state()
            current.get_seat("codex", email)["usage"] = {
                "ok": True, "error": None, "windows": {},
                "fetched_at": iso(now() + timedelta(minutes=1)),
                "last_attempted_at": iso(now() + timedelta(minutes=1)),
            }
            current.save()
        return 200, codex_ok_body(primary=88)

    summary = U.refresh_live(ctx, "codex", only=email, force=True, get=get)
    assert summary["codex"][email] == "stale"
    stored = ctx.load_state().get_seat("codex", email)["usage"]
    assert stored["windows"] == {}


def test_late_transport_exception_cannot_overwrite_a_newer_success(ctx):
    state = _seed_two_codex(ctx)
    email = state.active("codex")

    def get(*_):
        current = ctx.load_state()
        U.store_fetch(current, "codex", email, U.Usage(
            ok=True, fetched_at=iso(now()), windows={"5h": U.Window(used_pct=12)}),
            blob=ctx.cred["codex"].get_live())
        current.save()
        raise TimeoutError("the older request timed out after another poll completed")

    result = U.refresh_live(ctx, "codex", only=email, force=True, get=get)
    assert result["codex"][email] == "stale"
    stored = ctx.load_state().get_seat("codex", email)["usage"]
    assert stored["ok"] is True and stored["error"] is None
    assert stored["windows"]["5h"]["used_pct"] == 12


def test_refresh_live_rejects_mutation_between_credential_check_and_commit(ctx, monkeypatch):
    state = _seed_two_codex(ctx)
    email = state.active("codex")
    real_blob = U._detached_blob
    calls = {"n": 0}

    def racing_blob(*args, **kwargs):
        blob = real_blob(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:  # commit-time read completed; mutate before its next lock acquisition
            with ctx.locked():
                current = ctx.load_state()
                current.get_seat("codex", email)["plan"] = "new subscription"
                current.save()
        return blob

    monkeypatch.setattr(U, "_detached_blob", racing_blob)
    summary = U.refresh_live(
        ctx, "codex", only=email, force=True,
        get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=88))}),
    )
    assert summary["codex"][email] == "stale"
    assert ctx.load_state().get_seat("codex", email)["plan"] == "new subscription"


def test_refresh_live_rejects_removed_and_readded_same_email(ctx):
    state = _seed_two_codex(ctx)
    email = state.active("codex")

    def get(url, headers, timeout):
        with ctx.locked():
            current = ctx.load_state()
            current.remove_seat("codex", email)
            seat = current.upsert_seat("codex", email)
            seat["added_at"] = "new-seat-incarnation"
            current.set_active("codex", email)
            current.save()
        return 200, codex_ok_body(primary=88)

    summary = U.refresh_live(ctx, "codex", only=email, force=True, get=get)
    assert summary["codex"][email] == "stale"
    assert ctx.load_state().get_seat("codex", email)["usage"] is None


def test_refresh_live_starts_both_active_providers_before_either_finishes(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["claude"].set_live(make_claude_blob())
    acct.add(ctx, ctx.load_state(), "claude", email="c@x.com")
    both_started = threading.Event()
    seen = []
    guard = threading.Lock()

    def get(url, headers, timeout):
        with guard:
            seen.append(url)
            if len(seen) == 2:
                both_started.set()
        assert both_started.wait(1), "the other active provider was postponed"
        body = codex_ok_body() if url == P.CODEX_USAGE_URL else claude_ok_body()
        return 200, body

    summary = U.refresh_live(ctx, active_only=True, force=True, get=get,
                             user_agent="claude-code/x")
    assert summary["codex"]["a@x.com"] == "ok"
    assert summary["claude"]["c@x.com"] == "ok"


def test_refresh_live_reads_supervised_codex_home_instead_of_shared_mirror(ctx, monkeypatch):
    from acctsw import session
    email = "a@x.com"
    old = make_codex_blob(email).replace('"access_token": "a"', '"access_token": "old"')
    fresh = make_codex_blob(email).replace('"access_token": "a"', '"access_token": "fresh"')
    ctx.cred["codex"].set_live(old)
    acct.add(ctx, ctx.load_state(), "codex", email=email)
    ctx.snapshot_set("codex", email, fresh)
    monkeypatch.setattr(session, "active_session",
                        lambda _data_dir, _tool: {"email": email, "pid": 1, "started_at": "x"})
    seen = {}

    def get(url, headers, timeout):
        seen["authorization"] = headers["Authorization"]
        return 200, codex_ok_body()

    assert U.refresh_live(ctx, "codex", only=email, force=True, get=get)["codex"][email] == "ok"
    assert seen["authorization"] == "Bearer fresh"


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


def test_claude_user_agent_caches_version_subprocess(monkeypatch):
    import acctsw.usage as um
    um._claude_user_agent_for_exe.cache_clear()
    calls = []

    class Result:
        returncode = 0
        stdout = "9.9.1 (Claude Code)\n"

    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())
    assert U.claude_user_agent("/fake/claude") == "claude-code/9.9.1"
    assert U.claude_user_agent("/fake/claude") == "claude-code/9.9.1"
    assert len(calls) == 1
    um._claude_user_agent_for_exe.cache_clear()


def _epoch(iso_s):
    import calendar
    from datetime import datetime
    return calendar.timegm(datetime.fromisoformat(iso_s).utctimetuple())
