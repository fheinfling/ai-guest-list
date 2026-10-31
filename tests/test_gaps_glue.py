"""Gap tests from the mutation sweep of seatkind/seatswap/switch/selection/state."""
from datetime import timedelta

import pytest

from acctsw import seatkind
from acctsw.util import iso, now

pytestmark = pytest.mark.crossings

T = "claude"
W, P, P2 = "w@x", "p@x", "p2@x"


def seats(ctx):
    state = ctx.load_state()
    for email, plan in {W: "Team", P: "Max", P2: "Pro"}.items():
        state.upsert_seat(T, email, plan=plan)
    state.set_active(T, W)
    state.save()
    return ctx.load_state()


def test_live_rejects_junk_and_returns_real_booleans():
    at = now()
    for junk in (None, "always", {"scope": "forever"}, {"scope": "once"},
                 {"scope": "once", "at": "garbage"}):
        assert seatkind._live(junk, at) is False
    assert seatkind._live({"scope": "always"}, at) is True


def test_always_never_expires():
    at = now()
    old = iso(at - timedelta(days=30))
    assert seatkind._live({"scope": "always", "at": old}, at) is True
    assert seatkind._live({"scope": "always"}, at) is True


def test_once_lives_exactly_one_hour():
    at = now()
    rec = lambda age: {"scope": "once", "at": iso(at - age)}  # noqa: E731
    assert seatkind._live(rec(timedelta(minutes=59)), at) is True
    assert seatkind._live(rec(timedelta(hours=1)), at) is False
    assert seatkind._live(rec(timedelta(minutes=61)), at) is False


def test_non_crossing_and_duplicates_return_real_false(ctx):
    state = seats(ctx)
    assert seatkind.crosses(state, T, P, P2) is False
    assert seatkind.crosses(state, T, None, P) is False
    assert seatkind.request(state, T, P, P2, "1") is False
    assert seatkind.request(state, T, W, P, "1") is True
    assert seatkind.request(state, T, W, P, "1") is False
    assert seatkind.consume(state, T, W, P) is False, "no approval: nothing to spend"
    assert seatkind.consume(state, T, P, P2) is True


def test_request_dedup_needs_the_whole_triple(ctx):
    state = seats(ctx)
    assert seatkind.request(state, T, W, P, "1")
    assert seatkind.request(state, T, W, P, "2"), "another session, same move: its own question"
    assert seatkind.request(state, T, P, W, "1"), "other direction, same session"
    state.data["tools"][T]["crossings"]["requests"].clear()
    seatkind.request(state, T, W, P, "1")
    state.upsert_seat(T, "p3@x", plan="Max")
    assert seatkind.request(state, T, W, "p3@x", "1"), "same origin and session, other target"


def test_approve_and_deny_close_the_directions_requests(ctx):
    state = seats(ctx)
    seatkind.request(state, T, W, P, "1")
    seatkind.request(state, T, P, W, "1")
    seatkind.approve(state, T, W, P, "once")
    assert [(r["from"], r["to"]) for r in seatkind.pending(state, T)] == [(P, W)]
    seatkind.request(state, T, W, P, "2")
    seatkind.deny(state, T, W, P)
    assert [(r["from"], r["to"]) for r in seatkind.pending(state, T)] == [(P, W)]
    assert not seatkind.approved(state, T, W, P)


def test_chosen_and_withdraw_on_a_state_without_crossings(ctx):
    state = seats(ctx)
    seatkind.chosen(state, T, W, P)
    seatkind.withdraw(state, T, "1")
    assert "crossings" not in state.data["tools"][T]


def test_chosen_closes_open_requests_and_spends_once_only(ctx):
    state = seats(ctx)
    seatkind.request(state, T, W, P, "1")
    seatkind.approve(state, T, W, P, "once")
    seatkind.request(state, T, W, P, "2")
    seatkind.chosen(state, T, W, P)
    assert seatkind.pending(state, T) == []
    assert not seatkind.approved(state, T, W, P)
    seatkind.approve(state, T, P, W, "always")
    seatkind.chosen(state, T, P, W)
    assert seatkind.approved(state, T, P, W), "an always the user gave stays"


def test_pending_ignores_malformed_requests(ctx):
    state = seats(ctx)
    at = now()
    state.data["tools"][T]["crossings"] = {"approvals": {}, "requests": [
        "junk", None, {"to": P, "at": iso(at)}, {"from": W, "at": iso(at)},
        {"from": W, "to": P, "at": "garbage"}, {"from": W, "to": P},
    ]}
    assert seatkind.pending(state, T, at) == []


def test_pending_request_ttl_is_twelve_hours(ctx):
    state = seats(ctx)
    at = now()
    state.data["tools"][T]["crossings"] = {"approvals": {}, "requests": [
        {"from": W, "to": P, "session_id": "1", "at": iso(at - timedelta(hours=12))}]}
    assert len(seatkind.pending(state, T, at)) == 1, "exactly at the TTL is still open"
    assert seatkind.pending(state, T, at + timedelta(minutes=30)) == [], "past it, long gone"


@pytest.mark.parametrize("newest_first", [True, False])
def test_pending_keeps_the_oldest_ask_per_direction(ctx, newest_first):
    state = seats(ctx)
    at = now()
    old, new = iso(at - timedelta(hours=2)), iso(at - timedelta(hours=1))
    reqs = [{"from": W, "to": P, "session_id": "a", "at": old},
            {"from": W, "to": P, "session_id": "b", "at": new}]
    state.data["tools"][T]["crossings"] = {
        "approvals": {}, "requests": reqs[::-1] if newest_first else reqs}
    assert [r["at"] for r in seatkind.pending(state, T, at)] == [old]


# --- seatswap: gate / eligibility / hit hand-off / maintain / pick -----------------------------
import json  # noqa: E402

from acctsw import seatswap  # noqa: E402
from acctsw.seatproxy import Hit, Seat  # noqa: E402
from tests.test_seatswap import (FakeCA, FakeProxy, _no_machine_policy, blob,  # noqa: E402,F401
                                 hit, make, put_seat, rest)


def test_flag_semantics():
    assert seatswap._flag(True) is True
    assert seatswap._flag(False) is False
    assert seatswap._flag(None) is False
    assert seatswap._flag("off") is False and seatswap._flag("1") is True


def test_quarantined_seat_is_refused_even_when_otherwise_fine(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="tok-b")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok-b"
    (ctx.claude_home("b@x") / ".acctsw-quarantined").write_text("x")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


def test_expiry_exactly_at_the_minimum_ttl_is_not_enough(ctx, monkeypatch):
    fixed = now().replace(microsecond=0)
    monkeypatch.setattr(seatswap, "now", lambda: fixed)
    put_seat(ctx, "home@x")
    for email, delta, ok in (("edge@x", seatswap.MIN_TTL, False),
                             ("past@x", seatswap.MIN_TTL + timedelta(seconds=1), True)):
        put_seat(ctx, email, token="t")
        value = json.loads(blob("t"))
        value["claudeAiOauth"]["expiresAt"] = (fixed + delta).timestamp() * 1000
        ctx.claude_cred(email).set_live(json.dumps(value))
        assert bool(seatswap.eligible(ctx, email, home_email="home@x")) is ok


def test_timing_constants():
    assert seatswap.MAINTAIN_EVERY_S == 60.0 and seatswap.HANDOVER_IDLE_S == 5.0
    assert seatswap.SELFTEST_TIMEOUT_S == 5.0 and seatswap.PREFLIGHT_TTL == timedelta(minutes=10)


def test_no_proxy_entries_that_route_the_api_around_us_are_dropped(ctx):
    swap, _, _ = make(ctx)
    swap.start()
    env = swap.env({"NO_PROXY": "api.anthropic.com,.anthropic.com,[::1]:80,::1,keep.me:8080"})
    assert env["NO_PROXY"].split(",") == ["[::1]:80", "::1", "keep.me:8080", "127.0.0.1", "localhost"]
    assert seatswap._routes_api("*.anthropic.com") and not seatswap._routes_api("foo.com")
    assert not seatswap._routes_api("[api.anthropic.com") and not seatswap._routes_api("")
    swap.stop()


def test_decide_after_stop_returns_none_and_queues_nothing(ctx):
    swap, _, _ = make(ctx)
    swap.stop()
    assert swap._decide(hit()) is None
    assert swap._asks == []


def test_timed_out_ask_is_removed_from_the_queue(ctx):
    swap, _, _ = make(ctx, decide_timeout=0.05)
    assert swap._decide(hit()) is None
    assert swap._asks == [] and swap.pending() == []
    swap.stop()


def test_answer_matches_by_identity_then_equality_and_only_once(ctx):
    swap, proxy, _ = make(ctx, decide_timeout=5)
    import threading
    got = []
    h = hit()
    t = threading.Thread(target=lambda: got.append(swap._decide(h)))
    t.start()
    while not swap.pending():
        time.sleep(0.01)
    assert not swap.answer(hit("stranger@x"), Seat("z@x", "z")), "a different hit is not ours"
    assert proxy.bound().email == "home@x"
    assert swap.answer(hit(), None), "an equal hit answers it; None means passthrough"
    t.join(5)
    assert got == [None] and proxy.bound().email == "home@x"
    assert not swap.answer(h, Seat("z@x", "z")), "already answered"
    swap.stop()


def test_answer_prefers_the_identical_hit_over_an_equal_one(ctx):
    swap, _, _ = make(ctx, decide_timeout=5)
    import threading
    first, second = hit(), hit()
    out = {}
    ts = [threading.Thread(target=lambda k=k, h=h: out.setdefault(k, swap._decide(h)))
          for k, h in (("first", first), ("second", second))]
    ts[0].start()
    while len(swap._asks) < 1:
        time.sleep(0.01)
    ts[1].start()
    while len(swap._asks) < 2:
        time.sleep(0.01)
    assert swap.answer(second, Seat("s@x", "s"))
    ts[1].join(5)
    assert out == {"second": Seat("s@x", "s")}
    assert swap.answer(first, None)
    ts[0].join(5)
    swap.stop()


import time  # noqa: E402


def test_maintain_does_not_rebind_an_unchanged_token_and_logs(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="same")
    logs = []
    swap, proxy, clock = make(ctx, log=lambda *a, **k: logs.append((a, k)))
    proxy.bind(Seat("b@x", "same"))
    n = len(proxy.binds)
    swap.maintain()
    assert len(proxy.binds) == n and logs == []
    ctx.claude_cred("b@x").set_live(blob("fresh"))
    clock.t += seatswap.MAINTAIN_EVERY_S
    swap.maintain()
    assert logs[-1][0][0] == "seatswap: newer token rebound"


def test_maintain_logs_moves_and_counts_third_seat_hops(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    put_seat(ctx, "c@x", token="c")
    rest(ctx, "home@x")
    logs = []
    swap, proxy, _ = make(ctx, log=lambda *a, **k: logs.append((a, k)))
    proxy.bind(Seat("b@x", "b"))
    swap.maintain()
    assert swap.third_seat_moves == 1
    assert logs[-1][0][0] == "seatswap: token lapsing, moved" and logs[-1][1]["to"] == "c@x"


def test_handover_is_asked_once_not_repeatedly(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    rest(ctx, "home@x")
    swap, proxy, clock = make(ctx)
    proxy.bind(Seat("b@x", "b"))
    proxy.idle = seatswap.HANDOVER_IDLE_S
    assert swap.maintain() == "handover"
    assert swap.maintain() is None, "the request was made; a minute must pass before looking again"


def test_unreadable_keychain_keeps_the_binding_and_logs(ctx, monkeypatch):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b")
    logs = []
    swap, proxy, _ = make(ctx, log=lambda *a, **k: logs.append((a, k)))
    proxy.bind(Seat("b@x", "b"))

    def boom(*a, **k):
        raise seatswap._Unreadable("locked")
    monkeypatch.setattr(seatswap, "_eligible", boom)
    assert swap.maintain() is None and proxy.bound().email == "b@x"
    assert logs[-1][0][0] == "seatswap: keychain unreadable, binding kept"


def test_pick_skips_an_ineligible_seat_and_takes_the_next(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=5)      # too close to expiry
    put_seat(ctx, "c@x", token="c")
    rest(ctx, "home@x")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x") == Seat("c@x", "c")


def test_pick_goes_home_when_home_is_usable_and_never_asks(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "w@x", plan="Team", token="w")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "w@x") == Seat("home@x", None)


def test_pick_honours_exclude_and_does_not_retry_a_resting_home(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b")
    rest(ctx, "home@x")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x") == Seat("b@x", "b")
    assert swap.pick(ctx, "home@x", exclude={"b@x"}) is None


def test_pick_needs_both_settings_for_a_third_seat(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b")
    rest(ctx, "home@x")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x", third_seat=False) is None
    for key in ("auto_switch", "in_place_switch"):
        state = ctx.load_state()
        state.data["settings"][key] = False
        state.save()
        assert swap.pick(ctx, "home@x") is None, key
        state = ctx.load_state()
        state.data["settings"][key] = True
        state.save()
    assert swap.pick(ctx, "home@x") == Seat("b@x", "b")


def test_a_tls_reject_is_logged_once(ctx):
    logs = []
    swap, _, _ = make(ctx, log=lambda *a, **k: logs.append((a, k)))
    swap._on_tls_reject("x" * 500)
    swap._on_tls_reject("again")
    assert swap.tls_rejected() and len(logs) == 1 and len(logs[0][1]["reason"]) == 200
    swap.stop()


def test_pick_never_returns_an_excluded_home(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b")
    put_seat(ctx, "c@x", token="c")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "c@x", exclude={"home@x"}) == Seat("b@x", "b")


def test_pick_third_seat_settings_default_to_on(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b")
    rest(ctx, "home@x")
    state = ctx.load_state()
    state.data["settings"].pop("auto_switch", None)
    state.data["settings"].pop("in_place_switch", None)
    state.save()
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x") == Seat("b@x", "b")
