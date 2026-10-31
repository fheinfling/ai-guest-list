"""Gaps found by the diff-scoped mutation sweep of acctsw/launcher.py: in-place hops, crossings,
manual choices, auth hits and handover. Reuses the fakes of test_launcher_inplace."""
import os

import pytest

from acctsw import launcher as L
from acctsw.launcher import run
from tests.test_launcher_inplace import (  # noqa: F401  (fixtures are used by name)
    C1, C2, C3, Clock, FakeKinds, FakeSwap, Seat, Spawn, _approve, _click, _drop, _plans, _rest,
    _run, _session, marks, seats, tokens)
from tests.test_usage import fake_get


def _unrest(ctx, email):
    st = ctx.load_state()
    st.get_seat("claude", email).pop("limited_until", None)
    st.save()


def _pending(ctx):
    from acctsw import seatkind
    return [(r["from"], r["to"]) for r in seatkind.pending(ctx.load_state(), "claude")]


@pytest.mark.crossings
def test_a_click_settles_the_sessions_open_crossing_questions(seats, tokens, monkeypatch):
    """c1 (business) is out and c3 (business) rests, so the session asks about c2 (personal). The
    user then clicks c3: the click answers that question (no approval spent, question retracted)."""
    _plans(seats, **{C1: "team", C2: "max", C3: "team"})
    _rest(seats, C3)
    swap = FakeSwap()
    seen = []
    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: seen.append(_pending(seats)),
                    lambda: (_unrest(seats, C3), _click(seats, C3)), None,
                    lambda: seen.append(_pending(seats))], 0))
    assert _run(seats, spawn, swap) == 0
    assert seen[0] == [(C1, C2)]
    assert swap.binds == [Seat(C3, "tok-c3")]
    assert (C1, C2) not in seen[1]   # retracted (see the BUG test below for what else follows)


def test_a_click_to_a_healthy_seat_never_rests_that_seat(seats, tokens):
    _rest(seats, C3)
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: (_unrest(seats, C3), _click(seats, C3)), None, None], 0))
    kinds = FakeKinds({C1: "business", C2: "personal", C3: "business"})
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert swap.binds == [Seat(C3, "tok-c3")]
    assert seats.load_state().get_seat("claude", C3).get("limited_until") is None


def test_every_in_place_hop_costs_one_of_the_budget(seats, tokens, monkeypatch):
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 2)
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None, lambda: swap.post("limit", C2)], 0))
    assert _run(seats, spawn, swap) == 0
    assert [seat for _hit, seat in swap.answers] == [Seat(C2, "tok-c2"), Seat(C3, "tok-c3")]
    assert spawn.stops == 0


def test_a_hop_back_to_the_clients_own_login_is_not_counted(seats, tokens, monkeypatch):
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 2)
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None,           # c1 -> c2
                    lambda: (_unrest(seats, C1), _rest(seats, C3)),
                    lambda: swap.post("limit", C2), None,           # c2 -> home c1
                    lambda: (_unrest(seats, C3), _rest(seats, C2)),
                    lambda: swap.post("limit", C1), None], 0))      # c1 -> c3: still in budget
    assert _run(seats, spawn, swap) == 0
    assert [seat for _hit, seat in swap.answers] == [Seat(C2, "tok-c2"), Seat(C1, None),
                                                     Seat(C3, "tok-c3")]
    assert spawn.stops == 0 and seats.load_state().active("claude") == C3


@pytest.mark.crossings
def test_an_approved_crossing_gone_before_the_kill_stays_and_asks_again(seats):
    """Swap off: the heartbeat sees the approval, the pre-kill check no longer does. The child is
    not stopped for a move that cannot be committed, and the user is asked again."""
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})

    def approve_once_seen():
        _approve(seats, C1, C2)
        kinds.yes_left = 1

    spawn = Spawn(([lambda: _rest(seats, C1), None, approve_once_seen, None, None], 0))
    notes = []
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=notes.append,
               swap_factory=lambda _ctx: None, seatkind=kinds) == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert [r[:2] for r in kinds.requests] == [(C1, C2), (C1, C2)]
    assert seats.load_state().active("claude") == C1
    assert sum("approve in ai guest list" in n for n in notes) == 2


def test_going_home_by_limit_with_the_toggle_off_and_no_restart_budget(seats, tokens):
    _drop(seats, C3)
    swap = FakeSwap()

    def toggle_off_and_home_back():
        st = seats.load_state()
        st.set_setting("in_place_switch", False)
        st.get_seat("claude", C1).pop("limited_until")
        st.save()

    hits = []
    spawn = Spawn(([lambda: swap.post("limit", C1), None, toggle_off_and_home_back, None,
                    lambda: hits.append(swap.post("limit", C2)), None], 0))
    assert _run(seats, spawn, swap, max_switches=0) == 0
    assert swap.answers[-1] == (hits[0], Seat(C1, None)) and spawn.stops == 0


def test_the_toggle_off_with_no_restart_budget_leaves_the_child_alone(seats, tokens):
    swap = FakeSwap()

    def toggle_off():
        st = seats.load_state()
        st.set_setting("in_place_switch", False)
        st.save()

    hits = []
    spawn = Spawn(([toggle_off, lambda: hits.append(swap.post("limit", C1)), None], 0))
    assert _run(seats, spawn, swap, max_switches=0) == 0
    assert swap.answers == [(hits[0], None)] and swap.binds == []
    assert spawn.stops == 0 and seats.load_state().active("claude") == C1


def test_a_401_on_the_home_seat_moves_to_another_seat_in_place(seats, tokens):
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("auth", C1)), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.answers == [(hits[0], Seat(C2, "tok-c2"))]
    assert seats.load_state().active("claude") == C2 and spawn.stops == 0


def test_a_401_with_auto_switch_off_passes_through_on_the_home_seat(seats, tokens):
    swap = FakeSwap()

    def auto_off():
        st = seats.load_state()
        st.set_setting("auto_switch", False)
        st.save()

    hits = []
    spawn = Spawn(([auto_off, lambda: hits.append(swap.post("auth", C1)), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.answers == [(hits[0], None)] and swap.binds == []
    assert seats.load_state().active("claude") == C1


def test_maintain_moving_to_a_seat_the_state_does_not_know_still_says_so(seats, tokens):
    swap = FakeSwap()
    notes = []
    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: setattr(swap, "maintain_moves_to", "ghost@x.com")], 0))
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert any("ghost@x.com takes over" in n for n in notes)
    assert seats.load_state().active("claude") == C2   # the hop already made; nothing else moved


def _handover(seats, swap, clock, *, max_switches=6, extra=(), second=True):
    def token_running_out():
        swap.maintain_result = "handover"
        clock.advance(L.HANDOVER_QUIET_S + 1)

    scripts = [([lambda: swap.post("limit", C1), None, token_running_out, *extra], 1)]
    if second:
        scripts.append(([None], 0))
    spawn = Spawn(*scripts)
    notes = []
    rc = run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=notes.append,
             swap_factory=lambda _ctx: swap, max_switches=max_switches)
    return rc, spawn, notes


def test_a_handover_says_so_and_counts_as_one_relaunch(seats, tokens, monkeypatch):
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    rc, spawn, notes = _handover(seats, swap, clock, max_switches=2)
    assert rc == 0 and len(spawn.calls) == 2
    assert any("moving this session onto c2@x.com's own login" in n for n in notes)


def test_a_handover_uses_up_exactly_one_restart(seats, tokens, monkeypatch):
    """Budget 2: the handover is restart one, so one more restart is still allowed."""
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    tokens.pop(C3)   # c2's limit below can only be served by a restart onto c3
    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: setattr(swap, "maintain_result", "handover") or
                    clock.advance(L.HANDOVER_QUIET_S + 1)], 1),
                  ([lambda: _rest(seats, C2), None], 1),
                  ([None], 0))
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=lambda m: None,
               swap_factory=lambda _ctx: swap, max_switches=2) == 0
    assert len(spawn.calls) == 3
    assert seats.load_state().active("claude") == C3


def test_no_handover_restart_without_restart_budget(seats, tokens, monkeypatch):
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    rc, spawn, _ = _handover(seats, swap, clock, max_switches=0, extra=(None,), second=False)
    assert rc == 1 and spawn.stops == 0


def test_a_handover_goes_ahead_when_the_proxy_has_been_idle_for_exactly_the_quiet_time(
        seats, tokens, monkeypatch):
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    swap.idle = L.HANDOVER_QUIET_S
    rc, spawn, _ = _handover(seats, swap, clock)
    assert rc == 0 and spawn.stops == 1


def test_a_hop_in_place_never_touches_the_clients_own_login(seats, tokens):
    """The child's own login stays where it is: only the proxy carries it to c2, and the stored
    seats are not rewritten into the live credential."""
    from tests.conftest import make_claude_blob
    for em in (C1, C2):
        seats.snapshot_set("claude", em, make_claude_blob("pro" if em == C2 else "max"))
    seats.cred["claude"].set_live(make_claude_blob("max"))
    live_before = seats.cred["claude"].get_live()
    snapshots = {e: seats.snapshot_get("claude", e) for e in (C1, C2, C3)}
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert seats.load_state().active("claude") == C2
    assert seats.cred["claude"].get_live() == live_before
    assert {e: seats.snapshot_get("claude", e) for e in (C1, C2, C3)} == snapshots


def test_a_401_on_a_renewed_token_binds_it_when_the_answer_came_too_late(seats, tokens):
    swap = FakeSwap()
    hits = []

    def renewed_then_401_that_timed_out():
        tokens[C2] = "tok-c2-renewed"
        swap.refuse_answers = True
        hits.append(swap.post("auth", C2))

    spawn = Spawn(([lambda: swap.post("limit", C1), None, renewed_then_401_that_timed_out,
                    None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds[-1] == Seat(C2, "tok-c2-renewed") and swap.bound() == C2
    assert seats.load_state().active("claude") == C2 and spawn.stops == 0


def test_the_banner_echoing_a_limit_the_proxy_already_hopped_is_not_decided_again(
        seats, tokens, monkeypatch):
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    swap = FakeSwap()
    fetches = []

    def get(url, headers, timeout):
        fetches.append(url)
        return 404, ""

    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    b"You've hit your usage limit \xc2\xb7 resets 8pm\n"], 1), ([None], 0))
    assert run(seats, "claude", [], spawn=spawn, get=get, notify=lambda m: None,
               swap_factory=lambda _ctx: swap) == 1
    assert fetches == [] and spawn.stops == 0 and len(spawn.calls) == 1
    assert seats.load_state().active("claude") == C2


def test_a_second_limit_inside_the_block_window_is_passed_straight_through(seats, tokens):
    """Nowhere to land: the first Hit is decided, the next ones within TICK_BLOCK_S are answered
    with a pass-through without deciding (and notifying) again."""
    _drop(seats, C3)
    _rest(seats, C2)
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None,
                    lambda: hits.append(swap.post("limit", C1)), None], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert swap.answers == [(hits[0], None), (hits[1], None)]
    assert sum("no other claude seat is ready" in n for n in notes) == 1


# --- round 3: a click across business/personal spends the approval it rode on ---------------------

@pytest.mark.crossings
def test_a_click_across_kinds_on_a_once_approval_spends_it(seats, tokens):
    """The session asked to cross earlier and the user said "once"; then they clicked that seat.
    The click moves the session in place and the "once" is gone: a later automatic move from the
    same seat across the line asks again."""
    from acctsw import seatkind
    _plans(seats, **{C1: "team", C2: "max", C3: "max"})
    _approve(seats, C1, C2, "once")
    swap = FakeSwap()
    spawn = Spawn(([lambda: _click(seats, C2), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds == [Seat(C2, "tok-c2")]
    assert not seatkind.approved(seats.load_state(), "claude", C1, C2)


def _codex_kinds(ctx, approve=True):
    from acctsw import seatkind
    from tests.test_launcher import _two_codex
    st = _two_codex(ctx)
    st.get_seat("codex", "a@x.com")["plan"] = "team"
    st.get_seat("codex", "b@x.com")["plan"] = "plus"
    if approve:
        seatkind.approve(st, "codex", "a@x.com", "b@x.com", "once")
    st.save()


def _codex_click_spawn(ctx, calls, between=lambda: None):
    from acctsw.switch import switch

    def spawn(argv, on_output, on_tick=None, *, env=None):
        calls.append(list(argv))
        if len(calls) == 1:
            with ctx.locked():
                switch(ctx, ctx.load_state(), "codex", "b@x.com", manual=True)
            assert on_tick()
            between()
            return -15
        return 0
    return spawn


@pytest.mark.crossings
def test_a_codex_click_across_kinds_spends_the_once_approval_at_the_commit(ctx):
    from acctsw import seatkind
    _codex_kinds(ctx)
    calls = []
    assert run(ctx, "codex", [], spawn=_codex_click_spawn(ctx, calls)) == 0
    st = ctx.load_state()
    assert len(calls) == 2 and st.active("codex") == "b@x.com"
    assert not seatkind.approved(st, "codex", "a@x.com", "b@x.com")


@pytest.mark.crossings
def test_a_codex_click_whose_approval_lapses_while_stopping_resumes_where_it_was(ctx):
    _codex_kinds(ctx)
    homes = []

    def lapse():
        st = ctx.load_state()
        st.data["tools"]["codex"]["crossings"]["approvals"].clear()
        st.save()

    calls = []
    inner = _codex_click_spawn(ctx, calls, between=lapse)

    def spawn(argv, on_output, on_tick=None, *, env=None):
        homes.append(os.environ.get("CODEX_HOME"))
        return inner(argv, on_output, on_tick, env=env)

    notes = []
    assert run(ctx, "codex", [], spawn=spawn, notify=notes.append) == 0
    assert len(calls) == 2 and homes[1] == str(ctx.codex_home("a@x.com"))
    assert "resume" in calls[1]   # the session carries on, not afresh
    assert any("continue this session on b@x.com" in n for n in notes)   # asked again


@pytest.mark.crossings
def test_a_codex_click_across_kinds_without_approval_asks_and_never_restarts(ctx):
    """N8 where in place can't serve the click: the child is not stopped for a move that would
    only be refused at the commit. The session asks instead."""
    from acctsw.switch import switch
    _codex_kinds(ctx, approve=False)
    calls, stopped = [], []

    def spawn(argv, on_output, on_tick=None, *, env=None):
        calls.append(list(argv))
        if len(calls) > 1:
            return 0   # restarted: the guard below fails, and nothing clicks again
        with ctx.locked():
            switch(ctx, ctx.load_state(), "codex", "b@x.com", manual=True)
        stopped.append(on_tick())
        return 0

    notes = []
    assert run(ctx, "codex", [], spawn=spawn, notify=notes.append) == 0
    assert stopped == [False] and len(calls) == 1
    assert any("continue this session on b@x.com" in n for n in notes)
