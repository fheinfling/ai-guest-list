"""Permanent cross-tier hops and the retained legacy pair approval/request machinery."""
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from acctsw import bridge, seatkind, seatswap, seattier
from acctsw.selection import choose
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.test_seatswap import _locked_keychain, _no_machine_policy, blob, put_seat  # noqa: F401

pytestmark = pytest.mark.crossings

T = "claude"


def state_of(ctx):
    # Exercise the retained legacy machinery below by bypassing the load policy in memory only.
    state = ctx.load_state()
    state.set_setting("in_place_cross_tier", False)
    return state


@pytest.mark.parametrize("legacy_status", [None, "open", "denied"])
def test_stored_false_allows_cross_tier_hop_without_a_request(ctx, legacy_status):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Pro")
    state = ctx.load_state()
    state.set_setting("in_place_cross_tier", False)
    if legacy_status:
        state.data["tools"][T]["tier_requests"] = {
            "max|pro": {"at": iso(now()), "status": legacy_status},
        }
    state.save()
    assert json.loads(ctx.state_file.read_text())["settings"]["in_place_cross_tier"] is False

    state = ctx.load_state()
    before = deepcopy(state.data["tools"][T].get("tier_requests", {}))
    assert seattier.allowed(state, T, "max", "pro")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok"
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") is None
    assert seattier.request(state, T, "max", "pro") is False
    assert state.data["tools"][T].get("tier_requests", {}) == before
    assert seattier.pending(state, T) == []
    assert bridge.snapshot_state(ctx)["pending_tier_requests"] == []


def test_tier_reads_seats_blobs_and_plain_plans():
    assert seattier.tier({"plan": " Team "}) == "team"
    assert seattier.tier(blob(sub="max")) == "max"
    assert seattier.tier(blob(sub=" Team ")) == "team"
    assert seattier.tier("Pro") == "pro"
    for junk in (None, {}, {"plan": ""}, {"plan": 5}, "", "{bad", json.dumps({"x": 1}),
                 '{"claudeAiOauth": []}', blob(sub=None), blob(sub=" "), 5):
        assert seattier.tier(junk) is None


def test_allowed_when_equal_or_unknown_or_setting_or_pair_approval(ctx):
    st = state_of(ctx)
    assert seattier.allowed(st, T, "max", "max")
    assert seattier.allowed(st, T, None, "max") and seattier.allowed(st, T, "max", None)
    assert not seattier.allowed(st, T, "max", "team")
    seattier.approve(st, T, "team", "max")
    assert seattier.allowed(st, T, "max", "team") and seattier.allowed(st, T, "team", "max")
    assert not seattier.allowed(st, T, "max", "pro")          # only that pair
    st.set_setting("in_place_cross_tier", True)
    assert seattier.allowed(st, T, "max", "pro")


def test_the_setting_must_be_exactly_true(ctx):
    st = state_of(ctx)
    st.set_setting("in_place_cross_tier", "yes")
    assert not seattier.allowed(st, T, "max", "pro")


def test_request_is_asked_once_per_pair_and_never_after_no(ctx):
    st = state_of(ctx)
    assert seattier.request(st, T, "max", "team") is True
    assert seattier.request(st, T, "team", "max") is False     # same pair, other order
    assert seattier.pending(st, T) == [{"a": "max", "b": "team", "at": seattier.pending(st, T)[0]["at"]}]
    seattier.deny(st, T, "max", "team")
    assert seattier.pending(st, T) == []
    assert seattier.request(st, T, "max", "team") is False     # "no" is remembered
    assert seattier.request(st, T, "max", "pro") is True


def test_nothing_to_ask_when_allowed_or_unknown(ctx):
    st = state_of(ctx)
    assert seattier.request(st, T, "max", "max") is False
    assert seattier.request(st, T, None, "max") is False
    assert seattier.pending(st, T) == []
    seattier.approve(st, T, "max", "team")
    assert seattier.request(st, T, "max", "team") is False


def test_approving_closes_the_question_and_deny_revokes(ctx):
    st = state_of(ctx)
    seattier.request(st, T, "max", "team")
    seattier.approve(st, T, "max", "team")
    assert seattier.pending(st, T) == []
    seattier.deny(st, T, "team", "max")
    assert not seattier.allowed(st, T, "max", "team")


def test_approve_all_sets_the_setting_and_clears_questions(ctx):
    st = state_of(ctx)
    seattier.request(st, T, "max", "team")
    seattier.request(st, "codex", "plus", "pro")
    seattier.deny(st, T, "max", "pro")
    seattier.approve_all(st)
    assert st.settings()["in_place_cross_tier"] is True
    assert seattier.pending(st, T) == []
    # Turning it off later must not resurrect answered questions, including other tools'.
    st.set_setting("in_place_cross_tier", False)
    st.save()
    st = state_of(ctx)
    for tool, a, b in ((T, "max", "team"), ("codex", "plus", "pro"), (T, "max", "pro")):
        assert seattier.pending(st, tool) == []
        assert not seattier.request(st, tool, a, b)
        assert not seattier.allowed(st, tool, a, b)


def test_pair_approval_persists_with_its_timestamp_and_is_tool_specific(ctx):
    st = state_of(ctx)
    at = now() - timedelta(days=1)
    seattier.approve(st, T, "team", "max", at)
    st.save()
    st = state_of(ctx)
    assert st.data["tools"][T]["tier_approvals"] == {"max|team": {"at": iso(at)}}
    assert seattier.allowed(st, T, "team", "max")
    assert not seattier.allowed(st, "codex", "team", "max")


def test_pending_requests_persist_and_sort_by_time(ctx):
    st = state_of(ctx)
    at = now()
    assert seattier.request(st, T, "max", "team", at)
    assert seattier.request(st, T, "pro", "max", at - timedelta(hours=1))
    st.save()
    st = state_of(ctx)
    assert seattier.pending(st, T) == [
        {"a": "max", "b": "pro", "at": iso(at - timedelta(hours=1))},
        {"a": "max", "b": "team", "at": iso(at)},
    ]
    assert not seattier.request(st, T, "team", "max", at)
    assert seattier.request(st, "codex", "team", "max", at)


def test_pending_ignores_a_request_that_became_allowed(ctx):
    st = state_of(ctx)
    seattier.request(st, T, "max", "team")
    st.set_setting("in_place_cross_tier", True)
    assert seattier.pending(st, T) == []


# --- eligible / eligible_reason ---------------------------------------------------------------

def test_tier_mismatch_is_allowed_by_default(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    assert seattier.SETTING == "in_place_cross_tier"
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok"
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") is None


def test_allow_tier_true_lets_it_through_and_false_refuses_despite_approval(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x", allow_tier=True) == "tok"
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x", allow_tier=True) is None
    st = ctx.load_state()
    st.set_setting("in_place_cross_tier", True)
    st.save()
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok"
    assert seatswap.eligible(ctx, "b@x", home_email="home@x", allow_tier=False) is None
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x", allow_tier=False) == "tier"


def test_a_legacy_pair_approval_does_not_restrict_other_tier_hops(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    put_seat(ctx, "c@x", plan="Pro")
    st = ctx.load_state()
    seattier.approve(st, T, "team", "max")
    st.save()
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok"
    assert seatswap.eligible(ctx, "c@x", home_email="home@x") == "tok"


def test_reason_is_seat_when_something_else_blocks_and_tier_is_not_blamed(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team", expiry="none")
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") == "seat"
    assert seatswap.eligible_reason(ctx, "ghost@x", home_email="home@x") == "seat"
    put_seat(ctx, "c@x", plan="Max")
    assert seatswap.eligible_reason(ctx, "c@x", home_email="home@x") is None


def test_the_home_seat_itself_never_needs_a_tier_answer(ctx):
    put_seat(ctx, "home@x", plan="Max")
    assert seatswap.eligible_reason(ctx, "home@x", home_email="home@x") is None


@pytest.mark.parametrize("locked", ["home@x", "b@x"])
def test_unreadable_credentials_are_not_a_tier_refusal(ctx, monkeypatch, locked):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    _locked_keychain(monkeypatch, locked)
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") == "unreadable"


@pytest.mark.parametrize("blocker", ["quarantine", "expiry", "token"])
def test_tier_permission_cannot_bypass_seat_requirements(ctx, blocker):
    put_seat(ctx, "home@x", plan="Max")
    kw = {"minutes": 5} if blocker == "expiry" else {"token": ""} if blocker == "token" else {}
    put_seat(ctx, "b@x", plan="Team", **kw)
    if blocker == "quarantine":
        (ctx.claude_home("b@x") / ".acctsw-quarantined").touch()
    assert seatswap.eligible(ctx, "b@x", home_email="home@x", allow_tier=True) is None
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x", allow_tier=True) == "seat"


@pytest.mark.parametrize("denied", [False, True])
def test_tier_permission_never_blocks_selection_or_a_restart_switch(ctx, denied):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Pro")
    st = ctx.load_state()
    if denied:
        seattier.deny(st, T, "max", "pro")
    st.set_limited_until(T, "home@x", iso(now() + timedelta(hours=1)), source="usage")
    st.save()
    selection = choose(st, T)
    assert selection.available and selection.email == "b@x"
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") is None
    assert seattier.request(st, T, "max", "pro") is False
    switch(ctx, st, T, "b@x", sync=False, mirror=False)
    assert ctx.load_state().active(T) == "b@x"


# --- bridge -----------------------------------------------------------------------------------

def ask(ctx, a="max", b="team"):
    # A request left on disk by an older engine; new engines no longer create these.
    st = ctx.load_state()
    st.data["tools"][T].setdefault("tier_requests", {})[seattier.pair(a, b)] = {
        "at": iso(now()), "status": "open",
    }
    st.save()


def test_snapshot_hides_legacy_open_tier_requests_without_rewriting_them(ctx):
    ask(ctx)
    before = ctx.state_file.read_bytes()
    snap = bridge.snapshot_state(ctx)
    assert snap["pending_tier_requests"] == []
    assert ctx.state_file.read_bytes() == before


def test_approve_tier_pair_scope_records_the_pair_and_answers(ctx):
    ask(ctx)
    res = bridge.handle(ctx, {"action": "approve_tier", "tool": T, "a": "Team", "b": "max"})
    assert res["ok"] and res["state"]["pending_tier_requests"] == []
    st = ctx.load_state()
    assert seattier.allowed(st, T, "max", "team") and st.settings()["in_place_cross_tier"]
    assert "max|team" in st.data["tools"][T]["tier_approvals"]
    assert "max|team" not in st.data["tools"][T]["tier_requests"]


def test_approve_tier_all_sets_the_setting(ctx):
    ask(ctx)
    res = bridge.handle(ctx, {"action": "approve_tier", "tool": T, "a": "max", "b": "team", "scope": "all"})
    assert res["ok"] and ctx.load_state().settings()["in_place_cross_tier"] is True
    assert res["state"]["pending_tier_requests"] == []


def test_deny_tier_is_remembered(ctx):
    ask(ctx)
    res = bridge.handle(ctx, {"action": "deny_tier", "tool": T, "a": "max", "b": "team"})
    assert res["ok"] and res["state"]["pending_tier_requests"] == []
    st = ctx.load_state()
    assert seattier.request(st, T, "max", "team") is False
    assert st.data["tools"][T]["tier_requests"]["max|team"]["status"] == "denied"
    assert seattier.allowed(st, T, "max", "team")


@pytest.mark.parametrize("message", [
    {"tool": "nope", "a": "max", "b": "team"}, {"tool": T, "a": "max", "b": "max"},
    {"tool": T, "a": "", "b": "team"}, {"tool": T, "a": 5, "b": "team"},
    {"tool": T, "a": " Max ", "b": "MAX"}, {"tool": T, "a": " ", "b": "team"},
    {"tool": T, "a": "max|pro", "b": "team"}, {"tool": T, "a": "max", "b": "team|pro"},
    {"tool": T, "a": "max", "b": "team", "scope": "forever"}])
def test_bad_tier_answers_are_refused(ctx, message):
    res = bridge.handle(ctx, {"action": "approve_tier", **message})
    assert not res["ok"]
    st = ctx.load_state()
    assert st.settings()["in_place_cross_tier"] is True
    assert not seattier._peek(st, T, "tier_approvals")


@pytest.mark.parametrize("scope", ["pair", "all"])
def test_a_tier_approval_does_not_approve_a_business_personal_crossing(ctx, scope):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    st = ctx.load_state()
    seatkind.request(st, T, "home@x", "b@x", "session-1")
    crossings = deepcopy(st.data["tools"][T]["crossings"])
    st.set_limited_until(T, "home@x", iso(now() + timedelta(hours=1)), source="usage")
    st.save()
    res = bridge.handle(ctx, {"action": "approve_tier", "tool": T, "a": "max", "b": "team", "scope": scope})
    assert res["ok"]
    st = ctx.load_state()
    assert st.data["tools"][T]["crossings"] == crossings
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok"
    assert seatkind.crosses(st, T, "home@x", "b@x")
    assert not seatkind.approved(st, T, "home@x", "b@x")
    assert not choose(st, T).available


def test_crossing_approval_does_not_need_a_separate_tier_approval(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Team")
    st = ctx.load_state()
    seatkind.approve(st, T, "home@x", "b@x", "always")
    st.save()
    assert seattier.allowed(st, T, "max", "team")
    assert not seattier._peek(st, T, "tier_approvals")
    assert seatswap.eligible_reason(ctx, "b@x", home_email="home@x") is None
