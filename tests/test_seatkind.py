"""Business <-> personal crossings: never automatic, approvals directional, "once" spent once."""
from datetime import timedelta

import pytest

from acctsw import bridge, seatkind, session
from acctsw.selection import choose, crossing_candidate
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.conftest import make_claude_blob

pytestmark = pytest.mark.crossings

T = "claude"


def seats(ctx, plans: dict, active: str | None = None):
    state = ctx.load_state()
    for email, plan in plans.items():
        state.upsert_seat(T, email, plan=plan)
    state.set_active(T, active or next(iter(plans)))
    state.save()
    return ctx.load_state()


def rest(state, email, hours=2):
    state.set_limited_until(T, email, iso(now() + timedelta(hours=hours)), source="usage")


@pytest.mark.parametrize("plan, expected", [
    ("Team", "business"), ("Enterprise", "business"), ("Business", "business"),
    ("Free", "personal"), ("Pro", "personal"), ("Max", "personal"), ("max", "personal"),
    ("Plus", "personal"), ("Edu", "business"), ("TEAM", "business"),
    (None, "unknown"), ("", "unknown"), ("Unlimited", "unknown"),
    ("Claude_max_20x", "personal"),
])
def test_kind_comes_from_the_plan_label(plan, expected):
    assert seatkind.kind({"plan": plan}) == expected
    assert seatkind.kind(plan) == expected


def test_crosses_on_kind_change_or_any_unknown(ctx):
    state = seats(ctx, {"w@x": "Team", "w2@x": "Enterprise", "p@x": "Max", "p2@x": "Pro",
                        "u@x": None, "u2@x": None})
    assert seatkind.crosses(state, T, "w@x", "p@x")
    assert seatkind.crosses(state, T, "p@x", "w@x")
    assert not seatkind.crosses(state, T, "w@x", "w2@x")
    assert not seatkind.crosses(state, T, "p@x", "p2@x")
    assert seatkind.crosses(state, T, "p@x", "u@x")
    assert seatkind.crosses(state, T, "u@x", "p@x")
    assert seatkind.crosses(state, T, "u@x", "u2@x"), "two unknowns are not proof of sameness"
    assert not seatkind.crosses(state, T, "u@x", "u@x")
    assert not seatkind.crosses(state, T, None, "p@x")


def test_crossing_auto_is_a_default_off_toggle(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    assert state.settings()["crossing_auto"] is False
    for enabled in (False, True, False):
        result = bridge.handle(ctx, {"action": "toggle", "key": "crossing_auto", "value": enabled})
        assert result["ok"]
        state = ctx.load_state()
        assert state.settings()["crossing_auto"] is enabled
        for a, b in (("w@x", "p@x"), ("p@x", "w@x")):
            assert seatkind.approved(state, T, a, b) is enabled
            assert seatkind.consume(state, T, a, b) is enabled


def test_approval_is_directional(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.approve(state, T, "w@x", "p@x", "always")
    assert seatkind.approved(state, T, "w@x", "p@x")
    assert not seatkind.approved(state, T, "p@x", "w@x")


def test_once_is_consumed_by_the_switch_that_uses_it(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.approve(state, T, "w@x", "p@x", "once")
    assert seatkind.consume(state, T, "w@x", "p@x")
    assert not seatkind.approved(state, T, "w@x", "p@x")
    assert not seatkind.consume(state, T, "w@x", "p@x")


def test_always_survives_use_until_denied(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.approve(state, T, "w@x", "p@x", "always")
    assert seatkind.consume(state, T, "w@x", "p@x")
    assert seatkind.approved(state, T, "w@x", "p@x")
    seatkind.approve(state, T, "w@x", "p@x", "once")   # a later "once" never downgrades "always"
    seatkind.consume(state, T, "w@x", "p@x")
    assert seatkind.approved(state, T, "w@x", "p@x")
    seatkind.deny(state, T, "w@x", "p@x")
    assert not seatkind.approved(state, T, "w@x", "p@x")


def test_once_lapses_when_no_switch_uses_it(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.approve(state, T, "w@x", "p@x", "once", at=now() - seatkind.ONCE_TTL - timedelta(seconds=1))
    assert not seatkind.approved(state, T, "w@x", "p@x")


def test_non_crossing_needs_no_approval_and_consumes_nothing(ctx):
    state = seats(ctx, {"p@x": "Pro", "p2@x": "Max"})
    assert seatkind.approved(state, T, "p@x", "p2@x")
    assert seatkind.consume(state, T, "p@x", "p2@x")


def test_requests_are_per_session_and_answered_by_direction(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    assert seatkind.request(state, T, "w@x", "p@x", session_id=11)
    assert not seatkind.request(state, T, "w@x", "p@x", session_id=11)
    assert seatkind.request(state, T, "w@x", "p@x", session_id=12)
    assert seatkind.request(state, T, "p@x", "w@x", session_id=13)
    shown = seatkind.pending(state, T)
    assert all(r["reason"] == "limit" for r in shown)
    assert all(r["reason"] == "limit" for r in state.data["tools"][T]["crossings"]["requests"])
    assert [(r["from"], r["to"]) for r in shown] == [("w@x", "p@x"), ("p@x", "w@x")]
    seatkind.approve(state, T, "w@x", "p@x", "once")
    assert [(r["from"], r["to"]) for r in seatkind.pending(state, T)] == [("p@x", "w@x")]
    seatkind.deny(state, T, "p@x", "w@x")
    assert seatkind.pending(state, T) == []
    seatkind.request(state, T, "p@x", "w@x", session_id=14)
    seatkind.withdraw(state, T, 14)
    assert seatkind.pending(state, T) == []


def test_stale_requests_are_not_shown(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.request(state, T, "w@x", "p@x", 1, at=now() - seatkind.REQUEST_TTL - timedelta(minutes=1))
    assert seatkind.pending(state, T) == []


# --- selection ------------------------------------------------------------------------------------

def test_choose_never_auto_picks_an_unapproved_crossing(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"}, active="w@x")
    rest(state, "w@x")
    sel = choose(state, T)
    assert not sel.available and sel.all_limited and sel.email == "w@x"
    assert crossing_candidate(state, T) == "p@x"


def test_choose_takes_a_same_kind_seat_before_asking(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max", "w2@x": "Enterprise"}, active="w@x")
    rest(state, "w@x")
    assert choose(state, T).email == "w2@x"
    assert crossing_candidate(state, T) is None


def test_choose_takes_an_approved_crossing_in_that_direction_only(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"}, active="w@x")
    rest(state, "w@x")
    seatkind.approve(state, T, "p@x", "w@x", "always")
    assert not choose(state, T).available
    seatkind.approve(state, T, "w@x", "p@x", "once")
    assert choose(state, T).email == "p@x"
    assert crossing_candidate(state, T) is None


def test_choose_origin_overrides_the_active_seat(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max", "p2@x": "Pro"}, active="p@x")
    assert choose(state, T, exclude={"w@x"}, origin="w@x").available is False
    assert choose(state, T, exclude={"w@x"}, origin="p2@x").email == "p@x"


def test_unknown_plans_are_crossings_for_choose(ctx):
    state = seats(ctx, {"a@x": None, "b@x": None}, active="a@x")
    rest(state, "a@x")
    assert not choose(state, T).available
    assert choose(state, T, allow_crossing=True).email == "b@x"
    assert crossing_candidate(state, T) == "b@x"


# --- manual switch, bridge, session ------------------------------------------------------------

def _claude_seat(ctx, email, plan):
    ctx.claude_home(email).mkdir(parents=True, exist_ok=True)
    ctx.claude_cred(email).set_live(make_claude_blob(plan.lower()))


def test_a_manual_switch_moves_whom_it_does_not_cross_and_asks_the_rest(ctx, monkeypatch):
    plans = {"w@x": "Team", "w2@x": "Team", "p@x": "Max", "p2@x": "Max", "u@x": "Pro"}
    for email, plan in plans.items():
        _claude_seat(ctx, email, plan)
    state = seats(ctx, plans, active="u@x")
    seatkind.request(state, T, "w2@x", "w@x", 9)           # an earlier same-kind question
    seatkind.approve(state, T, "p2@x", "w@x", "once")      # the user's earlier yes for p2
    seatkind.approve(state, T, "u@x", "w@x", "once")       # the app's seat runs no terminal
    state.save()
    t = iso(now())
    monkeypatch.setattr(session, "active_sessions", lambda *_a, **_k: [
        {"email": "w2@x", "pid": 1, "started_at": t},                  # same kind: moves
        {"email": "p@x", "pid": 2, "started_at": t},                   # would cross: asked
        {"email": "p2@x", "pid": 3, "started_at": t, "home": "w@x"},   # back home: moves
    ])
    switch(ctx, state, T, "w@x", sync=False, mirror=False, manual=True)
    state = ctx.load_state()
    store = state.data["tools"][T]["crossings"]
    assert [(r["from"], r["to"], r["session_id"]) for r in store["requests"]] == [
        ("p@x", "w@x", "2")]
    assert store["requests"][0]["reason"] == "switch"
    assert bridge.snapshot_state(ctx)["pending_crossings"][0]["reason"] == "switch"
    assert [(r["from"], r["to"]) for r in seatkind.pending(state, T)] == [("p@x", "w@x")]
    assert not seatkind.approved(state, T, "p@x", "w@x")
    assert not seatkind.approved(state, T, "p2@x", "w@x"), "the move home spent no new grant"
    assert seatkind.approved(state, T, "u@x", "w@x"), "no terminal left u@x, so nothing spent"


def test_a_manual_move_home_keeps_an_always_approval(ctx, monkeypatch):
    for email, plan in (("w@x", "Team"), ("p@x", "Max")):
        _claude_seat(ctx, email, plan)
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"}, active="p@x")
    seatkind.approve(state, T, "p@x", "w@x", "always")
    state.save()
    monkeypatch.setattr(session, "active_sessions", lambda *_a, **_k: [
        {"email": "p@x", "pid": 4, "started_at": iso(now()), "home": "w@x"}])
    switch(ctx, state, T, "w@x", sync=False, mirror=False, manual=True)
    assert seatkind.approved(ctx.load_state(), T, "p@x", "w@x")


def test_an_automatic_switch_approves_nothing(ctx, monkeypatch):
    for email, plan in (("w@x", "Team"), ("p@x", "Max")):
        _claude_seat(ctx, email, plan)
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"}, active="w@x")
    switch(ctx, state, T, "p@x", sync=False, mirror=False)
    state = ctx.load_state()
    assert not seatkind.approved(state, T, "w@x", "p@x")
    assert "manual_switch" not in state.data["tools"][T]


@pytest.mark.parametrize("scope", ["once", "always"])
@pytest.mark.parametrize("reason", ["limit", "switch"])
def test_bridge_approve_crossing(ctx, scope, reason):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.request(state, T, "w@x", "p@x", 7, reason=reason)
    state.save()
    shown = bridge.snapshot_state(ctx)["pending_crossings"]
    assert shown == [{"from": "w@x", "to": "p@x", "at": shown[0]["at"], "tool": T,
                      "from_name": "W", "to_name": "P", "from_kind": "business",
                      "to_kind": "personal", "reason": reason}]
    res = bridge.handle(ctx, {"action": "approve_crossing", "tool": T, "from": "w@x", "to": "p@x",
                              "scope": scope})
    assert res["ok"] and res["state"]["pending_crossings"] == []
    rec = ctx.load_state().data["tools"][T]["crossings"]["approvals"]["w@x>p@x"]
    assert rec["scope"] == scope
    assert not seatkind.approved(ctx.load_state(), T, "p@x", "w@x")


def test_bridge_deny_crossing(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    seatkind.request(state, T, "w@x", "p@x", 7)
    state.save()
    res = bridge.handle(ctx, {"action": "deny_crossing", "tool": T, "from": "w@x", "to": "p@x"})
    assert res["ok"] and res["state"]["pending_crossings"] == []
    assert not seatkind.approved(ctx.load_state(), T, "w@x", "p@x")


@pytest.mark.parametrize("message", [
    {"tool": T, "from": "w@x", "to": "p@x", "scope": "forever"},
    {"tool": "nope", "from": "w@x", "to": "p@x"},
    {"tool": T, "from": "w@x", "to": "w@x"},
    {"tool": T, "from": "w@x", "to": "gone@x"},
])
def test_bridge_rejects_bad_crossing_answers(ctx, message):
    seats(ctx, {"w@x": "Team", "p@x": "Max"})
    res = bridge.handle(ctx, {"action": "approve_crossing", **message})
    assert not res["ok"]
    assert not (ctx.load_state().data["tools"][T].get("crossings") or {}).get("approvals")


def test_in_place_switch_is_a_default_on_toggle(ctx):
    assert ctx.load_state().settings()["in_place_switch"] is True
    res = bridge.handle(ctx, {"action": "toggle", "key": "in_place_switch", "value": False})
    assert res["ok"] and ctx.load_state().settings()["in_place_switch"] is False


def test_mark_session_records_a_home_that_differs(ctx):
    ctx.ensure_dirs()
    session.mark_session(ctx.data_dir, T, "b@x", home="a@x")
    live = session.active_session(ctx.data_dir, T)
    assert live["email"] == "b@x" and live["home"] == "a@x"
    assert session.active_sessions(ctx.data_dir, T, strict=True)[0]["home"] == "a@x"
    session.mark_session(ctx.data_dir, T, "a@x", home="a@x")
    assert "home" not in session.active_session(ctx.data_dir, T)


# --- D9: back to the session's own home seat is never a crossing ------------------------------

def test_home_is_exempt_only_as_the_destination(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"})
    assert not seatkind.crosses(state, T, "p@x", "w@x", home="w@x")
    assert seatkind.approved(state, T, "p@x", "w@x", home="w@x")
    assert seatkind.consume(state, T, "p@x", "w@x", home="w@x")
    assert not seatkind.request(state, T, "p@x", "w@x", 1, home="w@x")
    assert seatkind.crosses(state, T, "w@x", "p@x", home="w@x"), "leaving home still crosses"
    assert seatkind.crosses(state, T, "p@x", "w@x")


def test_choose_and_candidate_let_a_session_go_home(ctx):
    state = seats(ctx, {"w@x": "Team", "p@x": "Max"}, active="p@x")
    rest(state, "p@x")
    assert not choose(state, T, origin="p@x").available
    assert choose(state, T, origin="p@x", home="w@x").email == "w@x"
    assert crossing_candidate(state, T, origin="p@x") == "w@x"
    assert crossing_candidate(state, T, origin="p@x", home="w@x") is None


# --- the app's own auto-switch spends a "once" ---------------------------------------------------

def test_bridge_auto_switch_spends_a_once_approval(ctx, monkeypatch):
    from tests.test_auto_handoff import probe, seed
    summary = seed(ctx, spare_count=1)
    state = ctx.load_state()
    state.get_seat("codex", "seat0@test.example")["plan"] = "Team"
    state.get_seat("codex", "seat1@test.example")["plan"] = "Plus"
    seatkind.approve(state, "codex", "seat0@test.example", "seat1@test.example", "once")
    state.save()
    probe(monkeypatch)
    result = bridge._auto_switch_after_usage(ctx, summary)
    assert result and result["status"] == "switched" and result["to"] == "seat1@test.example"
    assert not seatkind.approved(ctx.load_state(), "codex", "seat0@test.example",
                                 "seat1@test.example")


@pytest.mark.parametrize("plan,expected", [
    ("Team", "business"), ("self_serve_business_prolite", "business"), ("Self_Serve_Business_Prolite", "business"),
    ("enterprise", "business"), ("team-premium", "business"), ("business pro", "business"),
    ("Max", "personal"), ("max_5x", "personal"), ("Plus", "personal"), ("pro_lite", "personal"),
    ("free", "personal"),
    ("prolite", "unknown"), ("teammate", "unknown"), ("", "unknown"), (None, "unknown"),
])
def test_kind_reads_the_words_in_free_form_plan_names(plan, expected):
    """Providers spell plans freely; a ChatGPT "self_serve_business_prolite" seat is business, not unknown
    (unknown would make every automatic hop between a user's own business seats ask for approval)."""
    assert seatkind.kind({"plan": plan}) == expected
