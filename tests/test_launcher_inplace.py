"""In-place seat switching in the launcher: the live child moves to another seat through the proxy
inside the launcher process, with restart as the fallback.

The proxy (acctsw.seatproxy), its main-thread glue (acctsw.seatswap) and the business/personal
rules (acctsw.seatkind) are separate modules; here they are FAKES built on their frozen
interfaces, so these tests pin the launcher's side of the contract only: which seat is billed,
what the one decision tail does with a proxy Hit, budgets, crossings, and teardown.
"""
import os
import subprocess
import sys
import threading
import time
import types
from collections import namedtuple
from dataclasses import dataclass
from datetime import timedelta

import pytest

import acctsw
from acctsw import accounts as acct
from acctsw import launcher as L
from acctsw.launcher import run
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.conftest import make_claude_blob
from tests.test_usage import fake_get

C1, C2, C3 = "c1@x.com", "c2@x.com", "c3@x.com"
Seat = namedtuple("Seat", "email token")


@dataclass(frozen=True)
class Hit:
    kind: str
    seat: str
    reset_at: str | None = None
    detail: str | None = None
    claim: str | None = None


class FakeSwap:
    """SeatSwap's frozen surface. ``post`` stands in for the proxy thread queueing a Hit."""

    wake_fd = 4242
    PROXY_URL = "http://acctsw:secret@127.0.0.1:9"   # the test process may have its own proxy

    def __init__(self):
        self.queue = []
        self.answers = []
        self.binds = []
        self.homes = []
        self.maintain_result = None
        self.maintain_moves_to = None
        self.refuse_answers = False   # every Hit timed out before the launcher answered
        self.preflight_says = None    # a reason string: the proxy can't reach the API
        self.used = True              # the child has sent /v1/messages through the proxy
        self.raising = set()          # method names that blow up
        self.tls_says_no = False      # a client in the child rejected the session CA
        self.in_flight = 0            # the proxy's own view of the child's traffic
        self.idle = 999.0
        self.proxy = types.SimpleNamespace(in_flight=lambda: self.in_flight,
                                           idle_for=lambda: self.idle)
        self.stopped = False
        self._bound = None

    def __getattribute__(self, name):
        if name in object.__getattribute__(self, "raising"):
            raise RuntimeError(f"swap.{name} exploded")
        return object.__getattribute__(self, name)

    def preflight(self):
        return self.preflight_says

    def proxy_used(self):
        return self.used

    def tls_rejected(self):
        return self.tls_says_no

    def post(self, kind, seat, **kw):
        hit = Hit(kind, seat, **kw)
        self.queue.append(hit)
        return hit

    def env(self, base):
        # Transport v2: the child keeps talking to api.anthropic.com through an HTTPS proxy.
        return {**base, "HTTPS_PROXY": self.PROXY_URL,
                "NODE_EXTRA_CA_CERTS": "/private/session-ca/node.pem"}

    def set_home(self, email):
        self.homes.append(email)
        self._bound = None

    def bound(self):
        return self._bound

    def pending(self):
        hits, self.queue = self.queue, []
        return hits

    def _take(self, seat):
        self._bound = seat.email if seat.token else None

    def answer(self, hit, seat):
        if self.refuse_answers:
            return False
        self.answers.append((hit, seat))
        if seat is not None:
            self._take(seat)
        return True

    def bind(self, seat):
        self.binds.append(seat)
        self._take(seat)

    def maintain(self, ctx, *, third_seat=True):
        self.third_seat = third_seat
        if self.maintain_moves_to:
            self._bound, self.maintain_moves_to = self.maintain_moves_to, None
        return self.maintain_result

    def stop(self):
        self.stopped = True


class FakeKinds:
    """seatkind's frozen API over ``tools.claude.crossings`` in the state file, as the app writes."""

    def __init__(self, kinds):
        self.kinds = kinds
        self.requests = []
        self.consumed = []
        self.withdrawn = []
        self.lapse = False   # approved() says yes, but it is gone by the commit
        self.yes_left = None  # approved() says yes this many more times, then no

    def kind(self, seat):
        return "personal"

    def crosses(self, state, tool, a, b, *, home=None):
        if a == b or (home and b == home):
            return False
        return self.kinds.get(a, "unknown") != self.kinds.get(b, "unknown")

    def _approvals(self, state, tool):
        return state.data["tools"][tool].setdefault("crossings", {}).setdefault("approvals", {})

    def approved(self, state, tool, a, b, at=None, *, home=None):
        if not self.crosses(state, tool, a, b, home=home):
            return True
        if self.yes_left is not None:
            self.yes_left -= 1
            return self.yes_left >= 0
        return f"{a}>{b}" in self._approvals(state, tool)

    def request(self, state, tool, a, b, session_id, at=None, *, home=None):
        self.requests.append((a, b, session_id))
        state.data["tools"][tool].setdefault("crossings", {}).setdefault("requests", []).append(
            {"from": a, "to": b, "session_id": session_id, "at": iso(at)})

    def consume(self, state, tool, a, b, at=None, *, home=None):
        if not self.crosses(state, tool, a, b, home=home):
            return True
        if self.lapse or f"{a}>{b}" not in self._approvals(state, tool):
            return False
        self.consumed.append((a, b))
        approvals = self._approvals(state, tool)
        if approvals[f"{a}>{b}"].get("scope") == "once":
            del approvals[f"{a}>{b}"]
        return True

    def withdraw(self, state, tool, session_id):
        self.withdrawn.append(session_id)
        crossings = state.data["tools"][tool].setdefault("crossings", {})
        crossings["requests"] = [r for r in crossings.get("requests", [])
                                 if r["session_id"] != session_id]


class Spawn:
    """One script per child: a list of steps, each bytes (output) or a callable/None run just before
    a heartbeat tick. Records argv, env and the wake fd of every launch."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls, self.envs, self.wakes = [], [], []
        self.stops = 0

    def __call__(self, argv, on_output, on_tick=None, *, env=None, wake_fd=None):
        steps, status = self.scripts[len(self.calls)]
        self.calls.append(list(argv))
        self.envs.append(env)
        self.wakes.append(wake_fd)
        for step in steps:
            if isinstance(step, bytes):
                stop = on_output(step)
            else:
                if step is not None:
                    step()
                stop = on_tick()
            if stop:
                self.stops += 1
                return status
        return status


@pytest.fixture
def seats(ctx):
    for em in (C1, C2, C3):
        ctx.cred["claude"].set_live(make_claude_blob())
        acct.add(ctx, ctx.load_state(), "claude", email=em)
    switch(ctx, ctx.load_state(), "claude", C1)
    return ctx


@pytest.fixture
def tokens(monkeypatch):
    """Seats with an in-place-eligible token; drop one to make it ineligible."""
    have = {C2: "tok-c2", C3: "tok-c3"}

    def eligible(_ctx, email, *, home_email):
        assert email != home_email   # the home seat never needs a token: the client holds it
        return have.get(email)
    monkeypatch.setattr(L, "_inplace_eligible", eligible)
    monkeypatch.setattr(L, "_inplace_seat", Seat)
    return have


@pytest.fixture(autouse=True)
def marks(monkeypatch):
    """Session marks; ``home=`` is the seatswap side's extension of mark_session."""
    seen = []
    real = L.mark_session

    def mark(data_dir, tool, email, *, home=None, **kw):
        seen.append((email, home))
        real(data_dir, tool, email, **kw)
    monkeypatch.setattr(L, "mark_session", mark)
    return seen


def _rest(ctx, email, until=None, source="usage"):
    st = ctx.load_state()
    st.set_limited_until("claude", email, until or iso(now() + timedelta(hours=2)), source=source)
    st.save()


class Clock:
    """The launcher's monotonic clock, advanced by hand (TICK_BLOCK_S waits in tests)."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        monkeypatch.setattr(L, "time", types.SimpleNamespace(
            monotonic=lambda: time.monotonic() + self.offset, sleep=time.sleep))

    def advance(self, seconds):
        self.offset += seconds


class Wall:
    """The wall clock the launcher and choose() read, advanced by hand: a rest that "runs out"
    does so exactly when the test says, however slow the machine is."""

    def __init__(self, monkeypatch):
        from acctsw import selection
        self.offset = timedelta(0)
        shifted = lambda: now() + self.offset   # noqa: E731
        monkeypatch.setattr(L, "now", shifted)
        monkeypatch.setattr(selection, "now", shifted)

    def advance(self, delta):
        self.offset += delta


def _drop(ctx, email):
    st = ctx.load_state()
    st.remove_seat("claude", email)
    st.save()


def _run(ctx, spawn, swap, **kw):
    kw.setdefault("notify", lambda m: None)
    return run(ctx, "claude", [], spawn=spawn, get=fake_get({}),
               swap_factory=(lambda _ctx: swap), **kw)


def _session(spawn):
    first = spawn.calls[0]
    return first[first.index("--session-id") + 1]


# --- the in-place hop -----------------------------------------------------------------------------

def test_limit_hit_moves_the_live_child_without_a_restart(seats, tokens, marks):
    swap = FakeSwap()
    reset = iso(now() + timedelta(hours=3))
    posted = []
    spawn = Spawn(([lambda: posted.append(swap.post("limit", C1, reset_at=reset)), None], 0))
    notes = []

    assert _run(seats, spawn, swap, notify=notes.append) == 0

    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert swap.answers == [(posted[0], Seat(C2, "tok-c2"))]   # replayed on c2, never passed on
    st = seats.load_state()
    assert st.active("claude") == C2
    assert st.get_seat("claude", C1)["limited_until"] == reset
    assert st.get_seat("claude", C1)["limit_source"] == "usage"
    assert st.get_seat("claude", C2).get("limited_until") is None
    assert (C2, C1) in marks                                   # billed c2, client home c1
    assert spawn.envs[0]["HTTPS_PROXY"].startswith("http://acctsw:")
    assert "ANTHROPIC_BASE_URL" not in spawn.envs[0]
    assert spawn.wakes == [FakeSwap.wake_fd]
    assert swap.homes == [C1]
    assert swap.stopped
    assert any("same session, no restart" in n for n in notes)


def test_a_second_limit_rests_the_bound_seat_not_the_launch_seat(seats, tokens):
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None,
                    lambda: hits.append(swap.post("limit", C2)), None], 0))

    assert _run(seats, spawn, swap) == 0

    assert spawn.stops == 0
    assert [seat for _hit, seat in swap.answers] == [Seat(C2, "tok-c2"), Seat(C3, "tok-c3")]
    st = seats.load_state()
    assert st.get_seat("claude", C2)["limited_until"] is not None
    assert st.active("claude") == C3


def test_a_late_answer_still_moves_the_proxy_for_the_requests_to_come(seats, tokens):
    swap = FakeSwap()
    swap.refuse_answers = True
    spawn = Spawn(([lambda: swap.post("limit", C1)], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert swap.binds == [Seat(C2, "tok-c2")] and swap.bound() == C2
    assert sum("takes over" in n for n in notes) == 1   # one hop, not a second to repair it
    assert seats.load_state().active("claude") == C2


def test_hits_queued_before_a_hop_replay_on_the_new_seat(seats, tokens):
    swap = FakeSwap()
    hits = []

    def two_requests_hit_c1():
        hits.extend([swap.post("limit", C1), swap.post("limit", C1)])

    spawn = Spawn(([two_requests_hit_c1, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.answers == [(hits[0], Seat(C2, "tok-c2")), (hits[1], Seat(C2, "tok-c2"))]
    assert seats.load_state().get_seat("claude", C2).get("limited_until") is None


def test_heartbeat_reads_the_bound_seat_after_an_in_place_hop(seats, tokens):
    """After c1 -> c2 in place, c1 stays rested (by us) and c2 is healthy: later state changes must
    be judged against c2. Reading the launch seat would re-decide c1's rest and leave c2."""
    swap = FakeSwap()

    def unrelated_write():
        st = seats.load_state()
        st.set_setting("notify", False)
        st.save()

    spawn = Spawn(([lambda: swap.post("limit", C1), None, unrelated_write, None], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0

    assert len(swap.answers) == 1 and spawn.stops == 0
    st = seats.load_state()
    assert st.active("claude") == C2
    assert st.get_seat("claude", C2).get("limited_until") is None
    assert not any("next session" in n for n in notes)


def test_another_process_resting_the_bound_seat_moves_on_in_place(seats, tokens):
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None, lambda: _rest(seats, C2), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 0
    assert swap.binds == [Seat(C3, "tok-c3")]   # no Hit to answer: the proxy is rebound
    assert seats.load_state().active("claude") == C3


def test_in_place_hops_do_not_need_or_spend_the_restart_budget(seats, tokens):
    swap = FakeSwap()
    tokens.pop(C3)   # the second landing must restart
    spawn = Spawn(([lambda: swap.post("limit", C1), None, lambda: swap.post("limit", C2)], 1),
                  ([None], 0))

    assert _run(seats, spawn, swap, max_switches=1) == 0

    assert spawn.stops == 1 and len(spawn.calls) == 2
    assert spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert seats.load_state().active("claude") == C3
    assert swap.homes == [C1, C3]   # the restarted child holds c3's own login


def test_in_place_hop_with_no_restart_budget_at_all(seats, tokens):
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None], 0))
    assert _run(seats, spawn, swap, max_switches=0) == 0
    assert swap.answers[0][1] == Seat(C2, "tok-c2")
    assert seats.load_state().active("claude") == C2


def test_in_place_budget_is_enforced(seats, tokens, monkeypatch):
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 1)
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None, lambda: swap.post("limit", C2)], 1),
                  ([None], 0))

    assert _run(seats, spawn, swap) == 0

    assert [seat for _hit, seat in swap.answers] == [Seat(C2, "tok-c2"), None]
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert seats.load_state().active("claude") == C3


def test_ineligible_landing_falls_back_to_restart(seats, tokens):
    tokens.clear()
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1))], 1), ([None], 0))

    assert _run(seats, spawn, swap) == 0

    assert swap.answers == [(hits[0], None)]   # passed through: this child is stopping
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert seats.load_state().active("claude") == C2
    assert swap.homes == [C1, C2]


def test_limit_with_nowhere_to_land_passes_through_and_mutes_the_banner(seats, tokens,
                                                                         monkeypatch):
    """The child prints the 429 it was passed as a banner; the proxy's structured Hit already
    decided it, so the echo must not cost a usage probe (even past the probe cooldown)."""
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    _drop(seats, C3)
    _rest(seats, C2)
    swap = FakeSwap()
    fetches = []

    def get(url, headers, timeout):
        fetches.append(url)
        return 404, ""

    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None,
                    b"You've hit your usage limit \xc2\xb7 resets 8pm\n"], 0))
    notes = []
    assert run(seats, "claude", [], spawn=spawn, get=get, notify=notes.append,
               swap_factory=lambda _ctx: swap) == 0

    assert swap.answers == [(hits[0], None)]
    assert spawn.stops == 0 and fetches == []          # the echoed banner is not re-probed
    assert sum("no other claude seat is ready" in n for n in notes) == 1


def test_auth_hit_on_a_borrowed_seat_falls_back_to_the_clients_own_login(seats, tokens):
    _drop(seats, C3)
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: _click(seats, C2), None,
                    lambda: hits.append(swap.post("auth", C2)), None], 0))

    assert _run(seats, spawn, swap) == 0

    assert swap.answers[-1] == (hits[0], Seat(C1, None))
    assert swap.bound() is None and seats.load_state().active("claude") == C1
    assert spawn.stops == 0


def test_handover_restarts_on_the_bound_seats_own_login(seats, tokens, monkeypatch):
    clock = Clock(monkeypatch)
    swap = FakeSwap()

    def token_running_out():
        swap.maintain_result = "handover"
        clock.advance(L.HANDOVER_QUIET_S + 1)

    spawn = Spawn(([lambda: swap.post("limit", C1), None, token_running_out], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert swap.homes == [C1, C2]
    assert seats.load_state().active("claude") == C2


def test_handover_waits_while_the_child_is_busy(seats, tokens, monkeypatch):
    """A tool running inside the child redraws its spinner: never restart under it."""
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    seen = []

    def token_running_out():
        swap.maintain_result = "handover"

    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: clock.advance(L.HANDOVER_QUIET_S + 1),
                    b"\xe2\xa0\x8b Running tests\xe2\x80\xa6", token_running_out,
                    lambda: seen.append(spawn.stops),
                    lambda: clock.advance(L.HANDOVER_QUIET_S + 1)], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert seen == [0] and spawn.stops == 1


def test_maintain_moving_the_proxy_on_its_own_is_recorded_as_a_hop(seats, tokens, marks):
    swap = FakeSwap()

    def token_lapses_and_swap_moves():
        swap.maintain_moves_to = C3

    spawn = Spawn(([lambda: swap.post("limit", C1), None, token_lapses_and_swap_moves], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 0
    assert seats.load_state().active("claude") == C3
    assert (C3, C1) in marks


# --- feature gate and teardown --------------------------------------------------------------------

@pytest.mark.parametrize("on", [False, True])
def test_default_factory_follows_seatswap_enabled(seats, monkeypatch, on):
    built = []

    class RealishSwap(FakeSwap):
        def __init__(self, ctx):
            super().__init__()
            built.append(ctx)

    fake = types.ModuleType("acctsw.seatswap")
    fake.enabled = lambda ctx, environ=os.environ, **kw: on
    fake.SeatSwap = RealishSwap
    monkeypatch.setitem(sys.modules, "acctsw.seatswap", fake)
    monkeypatch.setattr(acctsw, "seatswap", fake, raising=False)
    spawn = Spawn(([None], 0))

    assert run(seats, "claude", [], spawn=spawn, get=fake_get({})) == 0

    assert (spawn.envs[0].get("HTTPS_PROXY") == FakeSwap.PROXY_URL) is on
    assert spawn.wakes == [FakeSwap.wake_fd if on else None]
    assert len(built) == int(on)


def test_swap_is_stopped_even_when_the_child_blows_up(seats):
    swap = FakeSwap()

    def spawn(*_a, **_kw):
        raise RuntimeError("pty gone")

    with pytest.raises(RuntimeError):
        _run(seats, spawn, swap)
    assert swap.stopped


def test_a_failing_factory_falls_back_to_restart_only(seats):
    def factory(_ctx):
        raise OSError("bind: operation not permitted")

    spawn = Spawn(([None], 0))
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), swap_factory=factory) == 0
    assert spawn.envs[0].get("HTTPS_PROXY") != FakeSwap.PROXY_URL and spawn.wakes == [None]


def test_pty_spawn_wake_fd_rings_the_heartbeat_at_once(monkeypatch):
    """The proxy thread waits on a decision; the wake fd must run the tick now, not a full
    tick_interval later. A pipe and a plain child stand in for the PTY (none in the sandbox)."""
    procs = []

    def fork():
        r, w = os.pipe()
        procs.append(subprocess.Popen(["/bin/sleep", "3"], stdout=w, start_new_session=True))
        os.close(w)
        return procs[0].pid, r

    monkeypatch.setattr(L.pty, "fork", fork)
    wake_r, wake_w = os.pipe()
    ticks = []

    def ring():
        time.sleep(0.2)
        os.write(wake_w, b"!")

    threading.Thread(target=ring, daemon=True).start()
    started = time.monotonic()
    try:
        L.pty_spawn(["unused"], lambda chunk: False, on_tick=lambda: ticks.append(1) or True,
                    tick_interval=60.0, wake_fd=wake_r)
    finally:
        os.close(wake_r)
        os.close(wake_w)
    assert ticks == [1]
    assert time.monotonic() - started < 2.5


# --- business <-> personal crossings --------------------------------------------------------------

def _approve(ctx, a, b, scope="once"):
    st = ctx.load_state()
    st.data["tools"]["claude"].setdefault("crossings", {}).setdefault("approvals", {})[
        f"{a}>{b}"] = {"scope": scope, "at": iso(now())}
    st.save()


def test_unapproved_crossing_never_switches_and_asks_once(seats, tokens, monkeypatch):
    monkeypatch.setattr(L, "TICK_BLOCK_S", 0.0)   # let the second Hit re-decide
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None,
                    lambda: hits.append(swap.post("limit", C1)), None], 0))
    notes = []

    assert _run(seats, spawn, swap, seatkind=kinds, notify=notes.append) == 0

    assert swap.answers == [(hits[0], None), (hits[1], None)]
    assert spawn.stops == 0
    assert seats.load_state().active("claude") == C1
    assert kinds.requests == [(C1, C2, str(os.getpid()))]
    asks = [n for n in notes if "approve in ai guest list" in n]
    assert len(asks) == 1 and C2 in asks[0]
    assert not any("no other claude seat is ready" in n for n in notes)


def test_approved_crossing_is_taken_in_place_on_the_next_tick(seats, tokens):
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None, lambda: _approve(seats, C1, C2),
                    None], 0))

    assert _run(seats, spawn, swap, seatkind=kinds) == 0

    assert spawn.stops == 0
    assert swap.binds == [Seat(C2, "tok-c2")]
    assert kinds.consumed == [(C1, C2)]
    st = seats.load_state()
    assert st.active("claude") == C2
    assert "c1@x.com>c2@x.com" not in st.data["tools"]["claude"]["crossings"]["approvals"]
    assert st.data["tools"]["claude"]["crossings"]["requests"] == []   # answered: retracted


def test_an_approval_refused_at_the_commit_never_kills_and_asks_again(seats, tokens):
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()

    def approve_then_lapse():
        _approve(seats, C1, C2)
        kinds.yes_left = 1   # seen once by the heartbeat, gone by the commit
        kinds.lapse = True

    spawn = Spawn(([lambda: swap.post("limit", C1), None, approve_then_lapse, None, None], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert spawn.stops == 0 and swap.binds == [] and kinds.consumed == []
    assert seats.load_state().active("claude") == C1
    assert [r[:2] for r in kinds.requests] == [(C1, C2), (C1, C2)]   # asked again


def test_a_limit_never_restarts_for_an_approval_that_is_gone(seats, tokens):
    """The decision saw the approval; by the stop check it is gone. No kill for that move."""
    _drop(seats, C3)
    tokens.clear()   # not servable in place: the only way would be a restart
    kinds = FakeKinds({C1: "business", C2: "personal"})
    kinds.yes_left = 1   # the decision's check says yes, the pre-kill check says no
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert spawn.stops == 0 and swap.answers == [(hits[0], None)]
    assert seats.load_state().active("claude") == C1


def test_an_approval_lapsing_while_the_child_stops_resumes_on_the_same_seat(seats):
    """Swap off: the approved crossing restarts; the approval is gone by the commit. The work is
    resumed where it was, not ended."""
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})

    def approve():
        _approve(seats, C1, C2)

    spawn = Spawn(([lambda: _rest(seats, C1), None, approve, None], 1), ([None], 0))
    real = spawn.__call__

    def spawning(*a, **kw):
        status = real(*a, **kw)
        if len(spawn.calls) == 1:
            kinds.lapse, kinds.yes_left = True, 0   # expired: no longer approved, nothing to spend
        return status

    notes = []
    assert run(seats, "claude", [], spawn=spawning, get=fake_get({}), notify=notes.append,
               swap_factory=lambda _ctx: None, seatkind=kinds) == 0
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert seats.load_state().active("claude") == C1
    assert any("lapsed" in n and f"resuming your work on {C1}" in n for n in notes)


def test_open_crossing_questions_are_withdrawn_when_the_session_ends(seats, tokens):
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()
    spawn = Spawn(([lambda: swap.post("limit", C1), None], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert kinds.withdrawn == [str(os.getpid())]
    assert seats.load_state().data["tools"]["claude"]["crossings"]["requests"] == []


def test_approved_crossing_restarts_when_the_swap_is_off(seats):
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    spawn = Spawn(([lambda: _rest(seats, C1), None, lambda: _approve(seats, C1, C2), None], 1),
                  ([None], 0))

    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=lambda m: None,
               swap_factory=lambda _ctx: None, seatkind=kinds) == 0

    assert kinds.requests and kinds.consumed == [(C1, C2)]
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert seats.load_state().active("claude") == C2


def _plans(ctx, **plans):
    st = ctx.load_state()
    for email, plan in plans.items():
        st.get_seat("claude", email)["plan"] = plan
    st.save()


@pytest.mark.crossings
def test_real_rules_ask_then_carry_the_session_over_once_approved(seats, tokens):
    """The real seatkind and choose(): business c1 -> personal c2 is held back, the launcher asks
    about the candidate choose() left out, and an app approval moves the live child in place."""
    from acctsw import seatkind
    _drop(seats, C3)
    _plans(seats, **{C1: "team", C2: "max"})
    swap = FakeSwap()
    asked = []

    def app_sees_and_approves():
        st = seats.load_state()
        asked.extend(seatkind.pending(st, "claude"))
        seatkind.approve(st, "claude", C1, C2, "once")
        st.save()

    hits = []
    still_asked = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1)), None, app_sees_and_approves,
                    lambda: still_asked.extend(seatkind.pending(seats.load_state(), "claude"))],
                   0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0

    assert swap.answers == [(hits[0], None)] and swap.binds == [Seat(C2, "tok-c2")]
    assert [(r["from"], r["to"]) for r in asked] == [(C1, C2)]
    assert any("approve in ai guest list" in n and "(personal)" in n for n in notes)
    st = seats.load_state()
    assert st.active("claude") == C2
    assert not seatkind.approved(st, "claude", C1, C2)   # "once" was spent by this move
    assert still_asked == []   # the move answered it, before the session ended


@pytest.mark.crossings
def test_a_question_is_withdrawn_once_the_session_moves_elsewhere(seats, tokens, monkeypatch):
    """c1 (business) is out, c3 (business) is resting, so the launcher asks about c2 (personal).
    When c3 frees first, the session moves there and the question about c2 is moot."""
    from acctsw import seatkind
    monkeypatch.setattr(L, "TICK_BLOCK_S", 0.0)
    _plans(seats, **{C1: "team", C2: "max", C3: "team"})
    _rest(seats, C3)
    swap = FakeSwap()
    asked, still_asked = [], []

    def c3_frees():
        asked.extend(seatkind.pending(seats.load_state(), "claude"))
        st = seats.load_state()
        st.get_seat("claude", C3).pop("limited_until")
        st.save()

    spawn = Spawn(([lambda: swap.post("limit", C1), None, c3_frees, None,
                    lambda: still_asked.extend(seatkind.pending(seats.load_state(), "claude"))],
                   0))
    assert _run(seats, spawn, swap) == 0
    assert [(r["from"], r["to"]) for r in asked] == [(C1, C2)]
    assert swap.binds == [Seat(C3, "tok-c3")]
    assert still_asked == []


@pytest.mark.crossings
def test_recovery_looks_for_a_landing_from_the_billed_seat(seats, tokens, monkeypatch):
    """Another terminal moved ``active`` to personal c2 (resting) while this one is billed to
    business c1. When business c3's rest runs out on the clock, recovery must ask from c1 (c3 is
    same-kind), not from c2 (c3 would be a crossing, and the session would stay stuck)."""
    clock, wall = Clock(monkeypatch), Wall(monkeypatch)
    _plans(seats, **{C1: "team", C2: "max", C3: "team"})
    _rest(seats, C2, until=iso(now() + timedelta(hours=10)))
    _rest(seats, C3, until=iso(now() + timedelta(hours=1)))
    swap = FakeSwap()

    def other_terminal_moves_active():
        st = seats.load_state()
        st.set_active("claude", C2)
        st.save()

    def c3_rest_runs_out():   # c1's own rest (DEFAULT_COOLDOWN) is still on
        wall.advance(timedelta(hours=2))
        clock.advance(L.TICK_BLOCK_S + 1)

    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    other_terminal_moves_active, None, c3_rest_runs_out], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds == [Seat(C3, "tok-c3")]
    assert seats.load_state().active("claude") == C3


@pytest.mark.crossings
def test_the_question_is_withdrawn_when_the_sessions_own_seat_comes_back(seats, tokens,
                                                                          monkeypatch):
    from acctsw import seatkind
    clock, wall = Clock(monkeypatch), Wall(monkeypatch)
    _drop(seats, C3)
    _plans(seats, **{C1: "team", C2: "max"})
    swap = FakeSwap()
    asked, still_asked = [], []

    def c1_resets():
        asked.extend(seatkind.pending(seats.load_state(), "claude"))
        wall.advance(timedelta(hours=2))
        clock.advance(L.TICK_BLOCK_S + 1)

    spawn = Spawn(([lambda: swap.post("limit", C1,
                                      reset_at=iso(now() + timedelta(hours=1))), None,
                    c1_resets,
                    lambda: still_asked.extend(seatkind.pending(seats.load_state(), "claude"))],
                   0))
    assert _run(seats, spawn, swap) == 0
    assert [(r["from"], r["to"]) for r in asked] == [(C1, C2)]
    assert still_asked == [] and swap.binds == []
    assert seats.load_state().active("claude") == C1


# --- a seat the user picked -----------------------------------------------------------------------

def _click(ctx, email):
    """The app's click: active moves and a manual_switch request is recorded."""
    with ctx.locked():
        switch(ctx, ctx.load_state(), "claude", email, sync=False, mirror=False, manual=True)


def test_manual_switch_moves_claude_in_place(seats, tokens):
    swap = FakeSwap()
    spawn = Spawn(([lambda: _click(seats, C2), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 0 and swap.binds == [Seat(C2, "tok-c2")]


def test_a_click_never_restarts_claude_when_it_cannot_move_in_place(seats):
    spawn = Spawn(([lambda: _click(seats, C2), None, None], 0))
    notes = []
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=notes.append,
               swap_factory=lambda _ctx: None) == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert sum("applies to the next session" in n for n in notes) == 1


@pytest.mark.crossings
def test_a_click_across_kinds_asks_this_session_instead_of_moving_it(seats, tokens):
    """Review N8: a click that would carry THIS session business -> personal raises a question
    for it; once approved, the session moves in place."""
    from acctsw import seatkind
    _plans(seats, **{C1: "team", C2: "max", C3: "max"})
    swap = FakeSwap()
    asked = []

    def approve():
        st = seats.load_state()
        asked.extend((r["from"], r["to"]) for r in seatkind.pending(st, "claude"))
        seatkind.approve(st, "claude", C1, C2, "once")
        st.save()

    spawn = Spawn(([lambda: _click(seats, C2), lambda: asked.append(list(swap.binds)), approve,
                    None], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert asked[0] == [] and (C1, C2) in asked[1:]   # no move on the click, a question instead
    assert any("approve in ai guest list" in n for n in notes)
    assert swap.binds == [Seat(C2, "tok-c2")]


def test_a_click_still_lets_the_heartbeat_see_a_rest_in_the_same_write(seats):
    """Review N12: a click claude can't take in place must not swallow the rest of the tick."""
    def click_and_rest():
        _click(seats, C2)
        _rest(seats, C1)

    spawn = Spawn(([click_and_rest, None], 1), ([None], 0))
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=lambda m: None,
               swap_factory=lambda _ctx: None) == 0
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]




# --- review round: robustness, gates, origins -----------------------------------------------------

@pytest.mark.parametrize("method", ["maintain", "pending", "bind", "answer", "set_home", "env"])
def test_no_swap_error_ever_leaves_run_or_stops_the_child(seats, tokens, method):
    swap = FakeSwap()
    swap.raising = {method}
    spawn = Spawn(([lambda: swap.post("limit", C1), None, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1


def test_a_failed_preflight_runs_the_session_without_the_proxy(seats):
    swap = FakeSwap()
    swap.preflight_says = "TLS: certificate verify failed"
    spawn = Spawn(([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.envs[0].get("HTTPS_PROXY") != FakeSwap.PROXY_URL and spawn.wakes == [None]
    assert swap.stopped and swap.homes == []


def test_no_hop_in_place_before_the_child_has_used_the_proxy(seats, tokens):
    """A client that never talks through the proxy bills its own login whatever we bind."""
    swap = FakeSwap()
    swap.used = False
    spawn = Spawn(([lambda: _rest(seats, C1), None], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds == [] and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["--resume", _session(spawn)]


def test_switching_the_setting_off_mid_run_stops_hops_in_place_at_once(seats, tokens):
    swap = FakeSwap()

    def setting_off():
        st = seats.load_state()
        st.set_setting("in_place_switch", False)
        st.save()

    spawn = Spawn(([setting_off, None, lambda: swap.post("limit", C1)], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds == [] and [seat for _h, seat in swap.answers] == [None]
    assert spawn.stops == 1


def test_a_401_on_a_renewed_token_replays_on_the_new_one(seats, tokens):
    swap = FakeSwap()
    hits = []

    def renewed_then_401():
        tokens[C2] = "tok-c2-renewed"
        hits.append(swap.post("auth", C2))

    spawn = Spawn(([lambda: swap.post("limit", C1), None, renewed_then_401, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.answers[-1] == (hits[0], Seat(C2, "tok-c2-renewed"))
    assert seats.load_state().active("claude") == C2 and spawn.stops == 0


def test_a_401_never_restarts_the_child(seats, tokens, monkeypatch):
    """No other seat in place (budget spent): the 401 goes home, the child lives on."""
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 1)
    swap = FakeSwap()
    hits = []

    def click_away_from_a_reactively_rested_c1():
        _rest(seats, C1, source="reactive")   # out of choose(); not a signal the tick acts on
        _click(seats, C2)

    spawn = Spawn(([click_away_from_a_reactively_rested_c1, None,
                    lambda: hits.append(swap.post("auth", C2)), None], 0))
    assert _run(seats, spawn, swap) == 0
    # Going home is no switch, so it is never out of budget: the child's own login takes it.
    assert spawn.stops == 0 and swap.answers[-1] == (hits[0], Seat(C1, None))


def test_a_401_does_not_write_the_seat_off_for_the_run(seats, tokens):
    """c2's token got a 401 and the session went home; once c2's login renews, a later limit
    may land on c2 in place again."""
    _drop(seats, C3)
    swap = FakeSwap()

    def renewed():
        tokens[C2] = "tok-c2-renewed"

    spawn = Spawn(([lambda: _click(seats, C2), None, lambda: swap.post("auth", C2), None,
                    renewed, lambda: swap.post("limit", C1), None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.answers[-1][1] == Seat(C2, "tok-c2-renewed")
    assert seats.load_state().active("claude") == C2 and spawn.stops == 0


def test_a_dead_borrowed_token_is_not_bound_again(seats, tokens):
    """After a 401 sent the session home, the same dead c2 token must not be re-bound for the
    next limit (that would ping-pong between 401s and 429s): restart onto c2's own login."""
    _drop(seats, C3)
    swap = FakeSwap()
    spawn = Spawn(([lambda: _click(seats, C2), None, lambda: swap.post("auth", C2), None,
                    lambda: swap.post("limit", C1)], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 1 and swap.homes == [C1, C2]


def test_a_restart_launches_on_the_seat_it_committed(seats, tokens):
    """Another terminal moves active between our commit and the relaunch: ours still goes to c2."""
    tokens.clear()
    swap = FakeSwap()

    def notify(message):
        if "hopping to" in message:
            st = seats.load_state()
            st.set_active("claude", C3)
            st.save()

    spawn = Spawn(([lambda: swap.post("limit", C1)], 1), ([None], 0))
    assert _run(seats, spawn, swap, notify=notify) == 0
    assert swap.homes == [C1, C2]


def test_the_exit_time_net_judges_the_billed_seat(seats, tokens):
    """The child exits after an in-place hop to c2 while another terminal points active at c3:
    the post-exit usage check is about c2, the seat this child was billed to."""
    from tests.test_usage import claude_ok_body
    from acctsw import paths as P
    swap = FakeSwap()

    def other_terminal():
        st = seats.load_state()
        st.set_active("claude", C3)
        st.save()

    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(
        five=100.0, five_reset=iso(now() + timedelta(hours=2))))})
    spawn = Spawn(([lambda: _click(seats, C2), None, other_terminal], 1))
    assert run(seats, "claude", [], spawn=spawn, get=get, notify=lambda m: None,
               swap_factory=lambda _ctx: swap) == 1
    st = seats.load_state()
    assert st.get_seat("claude", C2).get("limited_until") is not None
    assert st.get_seat("claude", C3).get("limited_until") is None


@pytest.mark.crossings
def test_going_back_home_is_never_a_crossing(seats, tokens):
    """Business c1 is home; after an approved move to personal c2, c2's limit takes the session
    back to c1 without asking."""
    from acctsw import seatkind
    _drop(seats, C3)
    _plans(seats, **{C1: "team", C2: "max"})
    swap = FakeSwap()

    def approve():
        st = seats.load_state()
        seatkind.approve(st, "claude", C1, C2, "once")
        st.save()

    def c1_back():
        st = seats.load_state()
        st.get_seat("claude", C1).pop("limited_until")
        st.save()

    spawn = Spawn(([lambda: swap.post("limit", C1), None, approve, None, c1_back,
                    lambda: swap.post("limit", C2), None], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert swap.answers[-1][1] == Seat(C1, None)
    assert seats.load_state().active("claude") == C1
    assert sum("approve in ai guest list" in n for n in notes) == 1   # only the first move asked


@pytest.mark.crossings
def test_a_cold_start_whose_only_free_seat_crosses_asks_and_starts_there_once_approved(
        seats, tokens):
    from acctsw import seatkind
    _drop(seats, C3)
    _plans(seats, **{C1: "team", C2: "max"})
    _rest(seats, C1)
    swap = FakeSwap()
    notes = []

    def sleep(_seconds):
        st = seats.load_state()
        seatkind.approve(st, "claude", C1, C2, "once")
        st.save()

    spawn = Spawn(([None], 0))
    assert _run(seats, spawn, swap, notify=notes.append, sleep=sleep) == 0
    assert any("approve in ai guest list" in n for n in notes)
    assert swap.homes == [C2] and seats.load_state().active("claude") == C2
    assert not seatkind.approved(seats.load_state(), "claude", C1, C2)   # spent by the start


def test_a_dead_borrowed_token_falls_back_to_the_clients_own_login(seats, tokens):
    """c1 is resting and c2's token got a 401: the only place left is the child's own login."""
    _drop(seats, C3)
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: swap.post("limit", C1), None,
                    lambda: hits.append(swap.post("auth", C2))], 1), ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert (hits[0], Seat(C1, None)) in swap.answers


def test_a_limit_whose_approval_is_refused_at_the_commit_stays_and_asks_again(seats, tokens):
    """The decision saw a live approval for c1 -> c2; the commit finds nothing to spend."""
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    _approve(seats, C1, C2)
    kinds.lapse = True   # approved() still says yes; consume() finds nothing to spend
    swap = FakeSwap()
    hits = []
    spawn = Spawn(([lambda: hits.append(swap.post("limit", C1))], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert spawn.stops == 0 and swap.answers == [(hits[0], None)]
    assert {r[:2] for r in kinds.requests} == {(C1, C2)}   # asked again, never killed


# --- review round 2 -------------------------------------------------------------------------------

def test_a_failing_in_place_commit_stays_put_and_never_restarts(seats, tokens, monkeypatch):
    """N2: switch() blowing up inside the hop is logged; the child is neither moved nor killed
    (a restart would only run into the same failure)."""
    swap = FakeSwap()

    def broken_switch(*a, **kw):
        raise OSError("keychain locked")

    hits = []

    def hit_with_switch_broken():
        monkeypatch.setattr(L, "switch", broken_switch)
        hits.append(swap.post("limit", C1))

    spawn = Spawn(([hit_with_switch_broken, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 0 and swap.answers == [(hits[0], None)] and swap.binds == []


@pytest.mark.parametrize("where", ["output", "state"])
def test_a_decision_that_raises_never_unwinds_the_child(seats, monkeypatch, where):
    """N2: whatever path a decision comes from, an exception in it leaves the child running."""
    from tests.test_usage import claude_ok_body
    from acctsw import paths as P

    def boom(*a, **kw):
        raise RuntimeError("decision bug")

    monkeypatch.setattr(L, "handle_limit", boom)
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(
        five=100.0, five_reset=iso(now() + timedelta(hours=2))))})
    step = (b"You've hit your usage limit \xc2\xb7 resets 8pm\n" if where == "output"
            else (lambda: _rest(seats, C1)))
    spawn = Spawn(([step, None], 0))
    assert run(seats, "claude", [], spawn=spawn, get=get, notify=lambda m: None,
               swap_factory=lambda _ctx: None) == 0
    assert spawn.stops == 0


def test_an_auth_banner_restarts_instead_of_moving_in_place(seats, tokens):
    """N3: the child's own login is dead; only a restart gives it another one."""
    swap = FakeSwap()
    spawn = Spawn(([b"API Error: OAuth token has expired. Please obtain a new token.\n"], 1),
                  ([None], 0))
    assert _run(seats, spawn, swap) == 0
    assert swap.binds == [] and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["--resume", _session(spawn)]


def test_an_approved_crossing_waits_while_auto_switch_is_off(seats, tokens):
    """N4: an approved crossing is still an automatic move: auto-switch off means no move."""
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()

    def auto_off_then_approve():
        st = seats.load_state()
        st.set_setting("auto_switch", False)
        st.save()
        _approve(seats, C1, C2)

    spawn = Spawn(([lambda: swap.post("limit", C1), None, auto_off_then_approve, None], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert swap.binds == [] and seats.load_state().active("claude") == C1


def test_an_approved_crossing_to_a_seat_that_rested_since_is_not_taken(seats, tokens):
    """N4: the approval arrives after the target itself hit its limit: nothing to take."""
    _drop(seats, C3)
    kinds = FakeKinds({C1: "business", C2: "personal"})
    swap = FakeSwap()

    def c2_rests_then_approve():
        _rest(seats, C2)
        _approve(seats, C1, C2)

    spawn = Spawn(([lambda: swap.post("limit", C1), None, c2_rests_then_approve, None], 0))
    assert _run(seats, spawn, swap, seatkind=kinds) == 0
    assert swap.binds == [] and seats.load_state().active("claude") == C1


def _handover_run(seats, swap, clock, *, args=(), extra=(), second=True):
    def token_running_out():
        swap.maintain_result = "handover"
        clock.advance(L.HANDOVER_QUIET_S + 1)

    scripts = [([lambda: swap.post("limit", C1), None, token_running_out, *extra], 1)]
    if second:
        scripts.append(([None], 0))
    spawn = Spawn(*scripts)
    rc = run(seats, "claude", list(args), spawn=spawn, get=fake_get({}),
             notify=lambda m: None, swap_factory=lambda _ctx: swap)
    return rc, spawn


def test_a_handover_request_lapses_when_maintain_stops_asking(seats, tokens, monkeypatch):
    """N5: maintain() returning None clears a handover it asked for earlier."""
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    swap.in_flight = 1   # busy at first: the handover has to wait

    def stops_asking_and_goes_quiet():
        swap.maintain_result = None
        swap.in_flight = 0

    rc, spawn = _handover_run(seats, swap, clock, extra=(stops_asking_and_goes_quiet, None),
                              second=False)
    assert rc == 1 and spawn.stops == 0


def test_a_handover_waits_for_the_proxy_to_be_quiet(seats, tokens, monkeypatch):
    """N5: a request in flight (or one that just finished) holds the handover back."""
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    swap.idle = 1.0
    rc, spawn = _handover_run(seats, swap, clock, extra=(None,), second=False)
    assert rc == 1 and spawn.stops == 0


def test_no_handover_restart_for_a_print_run(seats, tokens, monkeypatch):
    """N5: `claude -p` has no prompt to go quiet at; it is never handed over."""
    clock = Clock(monkeypatch)
    swap = FakeSwap()
    rc, spawn = _handover_run(seats, swap, clock, args=("-p", "do it"), extra=(None,),
                              second=False)
    assert rc == 1 and spawn.stops == 0


def test_no_handover_onto_a_seat_without_a_loadable_login(seats, tokens, monkeypatch):
    """N5: restarting onto a seat whose login can't be loaded would end in "Not logged in"."""
    clock = Clock(monkeypatch)
    monkeypatch.setattr(L, "plan_claude_login", lambda ctx, state, email: None)
    swap = FakeSwap()
    rc, spawn = _handover_run(seats, swap, clock, extra=(None,), second=False)
    assert rc == 1 and spawn.stops == 0


def test_handover_prefers_home_over_a_restart(seats, tokens, monkeypatch):
    clock = Clock(monkeypatch)
    swap = FakeSwap()

    def home_back_and_token_running_out():
        st = seats.load_state()
        st.get_seat("claude", C1).pop("limited_until")
        st.save()
        swap.maintain_result = "handover"
        clock.advance(L.HANDOVER_QUIET_S + 1)

    spawn = Spawn(([lambda: swap.post("limit", C1), None, home_back_and_token_running_out], 0))
    assert run(seats, "claude", [], spawn=spawn, get=fake_get({}), notify=lambda m: None,
               swap_factory=lambda _ctx: swap) == 0
    assert spawn.stops == 0 and swap.bound() is None   # home: the child's own login
    assert seats.load_state().active("claude") == C1


def test_going_home_ignores_the_budget_and_the_toggle(seats, tokens, monkeypatch):
    """N6: going back to the child's own login is no switch: no budget, no toggle."""
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 1)
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
    assert _run(seats, spawn, swap) == 0
    assert swap.answers[-1] == (hits[0], Seat(C1, None)) and spawn.stops == 0


@pytest.mark.crossings
def test_a_cold_start_on_a_dead_seat_asks_and_waits_for_the_crossing(seats, tokens):
    """N7a: the active seat is signed out and the only other one crosses: ask, wait, then start
    there once approved — never launch on the dead seat."""
    from acctsw import seatkind
    _drop(seats, C3)
    _plans(seats, **{C1: "team", C2: "max"})
    st = seats.load_state()
    st.get_seat("claude", C1)["auth_error"] = "refresh_token_revoked"
    st.save()
    swap = FakeSwap()
    sleeps, notes = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            st = seats.load_state()
            seatkind.approve(st, "claude", C1, C2, "once")
            st.save()

    spawn = Spawn(([None], 0))
    assert _run(seats, spawn, swap, notify=notes.append, sleep=sleep) == 0
    assert any("approve in ai guest list" in n for n in notes)
    assert swap.homes == [C2]
    assert sleeps and max(sleeps) <= L.CROSSING_POLL_S   # N7b: an open question polls fast


# --- transport v2: a client that won't trust the session CA --------------------------------------

def test_a_rejected_certificate_restarts_once_without_the_proxy(seats, tokens):
    """Nothing ever went through: Claude Code itself refused the CA. Restart it once on the same
    seat without the proxy env (resume) — and never again, however often it is asked."""
    swap = FakeSwap()
    swap.used = False

    def rejected():
        swap.tls_says_no = True

    spawn = Spawn(([rejected, None], 1), ([None, None, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert spawn.stops == 1 and len(spawn.calls) == 2
    assert spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert spawn.envs[0]["HTTPS_PROXY"] == FakeSwap.PROXY_URL
    assert spawn.envs[1].get("HTTPS_PROXY") != FakeSwap.PROXY_URL
    assert spawn.wakes == [FakeSwap.wake_fd, None] and swap.stopped
    assert seats.load_state().active("claude") == C1


def test_a_rejected_certificate_after_traffic_only_stops_the_hops(seats, tokens):
    """Claude Code's own requests went through, so some other tool refused the CA: no restart,
    the session keeps running, and it no longer hops in place."""
    swap = FakeSwap()
    hits = []

    def rejected():
        swap.tls_says_no = True

    spawn = Spawn(([rejected, None, lambda: hits.append(swap.post("limit", C1)),
                    lambda: hits.append(spawn.stops)], 1), ([None], 0))
    notes = []
    assert _run(seats, spawn, swap, notify=notes.append) == 0
    assert swap.binds == [] and swap.answers[0] == (hits[0], None)
    assert any("off for this session" in n for n in notes)


@pytest.mark.crossings
def test_a_question_the_app_raised_for_this_session_is_withdrawn_at_exit(seats, tokens):
    """A click elsewhere asked about THIS session under the supervisor's pid; when the session
    ends, that question goes with it."""
    from acctsw import seatkind
    _plans(seats, **{C1: "team", C2: "max"})
    swap = FakeSwap()

    def app_asks_for_this_session():
        st = seats.load_state()
        seatkind.request(st, "claude", C1, C2, str(os.getpid()))
        st.save()

    spawn = Spawn(([app_asks_for_this_session, None], 0))
    assert _run(seats, spawn, swap) == 0
    assert seatkind.pending(seats.load_state(), "claude") == []


def test_maintain_may_pick_a_third_seat_only_while_the_budget_lasts(seats, tokens, monkeypatch):
    """The swap's own move to a third seat counts as a hop, and once the budget is spent it is
    told so: third_seat=False."""
    monkeypatch.setattr(L, "MAX_INPLACE_SWITCHES", 2)
    swap = FakeSwap()
    seen = []

    def swap_moves_to_c3():
        swap.maintain_moves_to = C3

    spawn = Spawn(([lambda: swap.post("limit", C1), None, swap_moves_to_c3, None,
                    lambda: seen.append(swap.third_seat), None,
                    lambda: seen.append(swap.third_seat)], 0))
    assert _run(seats, spawn, swap) == 0
    assert seen == [False, False] and seats.load_state().active("claude") == C3
