"""In-place seat switching end to end, minus the network: ``launcher.run`` with the REAL seatswap,
seatkind, selection, switch and session modules, the in-memory keychain of the test Context, and a
scripted child. Only the socket-owning SeatProxy is a fake, injected through seatswap's
``_default_proxy``; it plays the proxy thread by calling ``decide`` from a thread of its own, the
way a 429 on a live request would."""
import json
import os
import select
import threading
from datetime import timedelta

import pytest

from acctsw import accounts as acct
from acctsw import seatkind, seatswap
from acctsw import session as session_mod
from acctsw.launcher import run
from acctsw.seatproxy import Hit, Seat
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.test_usage import fake_get

C1, C2, C3 = "c1@x.com", "c2@x.com", "c3@x.com"
BASE_URL = "http://127.0.0.1:1/secret"


def _blob(email, ttl=timedelta(hours=2)):
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"tok-{email}", "refreshToken": f"ref-{email}",
        "expiresAt": int((now() + ttl).timestamp() * 1000), "scopes": [],
        "subscriptionType": "max"}})


class FakeProxy:
    """SeatProxy without a socket. ``limit()`` is a request on the bound seat answered 429."""

    def __init__(self, decide, *, decide_timeout, log):
        self.decide = decide
        self.seat = Seat("", None)
        self.binds = []
        self.stopped = False
        self.calls = []

    def start(self):
        return BASE_URL

    def stop(self):
        self.stopped = True

    def bind(self, seat):
        self.binds.append(seat)
        self.seat = seat

    def bound(self):
        return self.seat

    def in_flight(self):
        return 0

    def preflight(self, timeout=5.0):
        return None   # TLS to the upstream would be fine

    def messages_seen(self):
        return 1      # the child talks to the API through the proxy

    def idle_for(self):
        return 999.0

    def limit(self, reset_at=None):
        """Post a limit Hit from a proxy thread and wait until the launcher can see it."""
        hit = Hit("limit", self.seat.email, reset_at, None, "five_hour")
        call = {"hit": hit}
        call["thread"] = threading.Thread(
            target=lambda: call.setdefault("seat", self.decide(hit)), daemon=True)
        call["thread"].start()
        select.select([self.decide.__self__.wake_fd], [], [], 2.0)   # the doorbell rang
        self.calls.append(call)
        return call

    def answered(self, call):
        call["thread"].join(2.0)
        assert not call["thread"].is_alive(), "the proxy thread was never answered"
        return call["seat"]


class Spawn:
    """Per child: a list of steps (a callable or None), each run just before a heartbeat tick."""

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
            if step is not None:
                step()
            if on_tick():
                self.stops += 1
                return status
        return status


@pytest.fixture
def proxies(monkeypatch, tmp_path):
    """The feature ON through its real gate, with the fake proxy in place of the socket."""
    made = []

    def factory(decide, *, decide_timeout, log, **transport):   # ca, upstream_proxy, on_tls_reject
        made.append(FakeProxy(decide, decide_timeout=decide_timeout, log=log))
        made[-1].transport = transport
        return made[-1]

    monkeypatch.setattr(seatswap, "_default_proxy", factory)
    monkeypatch.setattr(seatswap, "MANAGED_SETTINGS", tmp_path / "managed-settings.json")
    monkeypatch.setenv(seatswap.SWITCH_ENV, "1")
    for name in (*seatswap.GATE_ENV, *seatswap.GATE_FLAGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return made


@pytest.fixture
def seats(ctx):
    def add(**plans):
        for email, plan in plans.items():
            ctx.cred["claude"].set_live(_blob(email))
            acct.add(ctx, ctx.load_state(), "claude", email=email)
        st = ctx.load_state()
        for email, plan in plans.items():
            st.get_seat("claude", email)["plan"] = plan
        st.save()
        switch(ctx, ctx.load_state(), "claude", C1)
        return ctx
    return add


def _run(ctx, spawn, **kw):
    kw.setdefault("notify", lambda m: None)
    return run(ctx, "claude", [], spawn=spawn, get=fake_get({}), **kw)


def _session(spawn):
    first = spawn.calls[0]
    return first[first.index("--session-id") + 1]


def test_limit_moves_the_live_child_to_a_same_kind_seat(seats, proxies):
    ctx = seats(**{C1: "max", C2: "max"})
    reset = iso(now() + timedelta(hours=3))
    marker, bound_after_hit, calls = [], [], []

    def look():
        marker.append(session_mod.active_session(ctx.data_dir, "claude"))
        bound_after_hit.append(proxies[0].seat)

    spawn = Spawn(([lambda: calls.append(proxies[0].limit(reset_at=reset)), look], 0))
    notes = []

    assert _run(ctx, spawn, notify=notes.append) == 0

    proxy = proxies[0]
    assert proxy.answered(calls[0]) == Seat(C2, f"tok-{C2}")    # replayed on c2
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert spawn.envs[0]["HTTPS_PROXY"] == BASE_URL and "ANTHROPIC_BASE_URL" not in spawn.envs[0]
    assert spawn.envs[0]["NODE_EXTRA_CA_CERTS"]          # the child trusts the session CA
    assert "127.0.0.1" in spawn.envs[0]["NO_PROXY"].split(",")
    assert spawn.wakes[0] is not None
    st = ctx.load_state()
    assert st.active("claude") == C2
    assert st.get_seat("claude", C1)["limited_until"] == reset
    assert marker[0]["email"] == C2 and marker[0]["home"] == C1
    assert bound_after_hit == [Seat(C2, f"tok-{C2}")]   # answer() bound it, no second hop needed
    assert proxy.binds[-1] == Seat(C2, f"tok-{C2}")
    assert sum("takes over" in n for n in notes) == 1      # one hop, not a second to repair it
    assert proxy.stopped


def test_a_crossing_asks_first_and_moves_on_the_tick_after_approval(seats, proxies):
    ctx = seats(**{C1: "team", C2: "max"})
    asked, calls = [], []

    def app_approves_once():
        st = ctx.load_state()
        asked.extend(st.data["tools"]["claude"]["crossings"]["requests"])
        assert [(r["from"], r["to"]) for r in seatkind.pending(st, "claude")] == [(C1, C2)]
        assert st.active("claude") == C1   # nothing moved while the question was open
        seatkind.approve(st, "claude", C1, C2, "once")
        st.save()

    spawn = Spawn(([lambda: calls.append(proxies[0].limit()), None, app_approves_once], 0))
    notes = []

    assert _run(ctx, spawn, notify=notes.append) == 0

    proxy = proxies[0]
    assert proxy.answered(calls[0]) is None                     # passed through: not approved
    assert [(r["from"], r["to"], r["session_id"]) for r in asked] == [(C1, C2, str(os.getpid()))]
    assert any("approve in ai guest list" in n for n in notes)
    assert spawn.stops == 0
    assert proxy.seat == Seat(C2, f"tok-{C2}")
    st = ctx.load_state()
    assert st.active("claude") == C2
    assert not seatkind.approved(st, "claude", C1, C2)          # the "once" was spent
    assert seatkind.pending(st, "claude") == []


def test_a_target_whose_token_is_about_to_expire_is_reached_by_restart(seats, proxies):
    ctx = seats(**{C1: "max", C2: "max"})
    ctx.claude_cred(C2).set_live(_blob(C2, ttl=timedelta(minutes=5)))
    calls, at_launch = [], []
    spawn = Spawn(([lambda: calls.append(proxies[0].limit())], 1),
                  ([lambda: at_launch.append(proxies[0].seat)], 0))

    assert _run(ctx, spawn) == 0

    proxy = proxies[0]
    assert proxy.answered(calls[0]) is None
    assert spawn.stops == 1 and spawn.calls[1][-2:] == ["--resume", _session(spawn)]
    assert ctx.load_state().active("claude") == C2
    assert at_launch == [Seat(C2, None)]   # the new child holds c2's own login: passthrough
    assert proxy.seat == Seat(C2, None)


def test_a_click_moves_the_live_child_in_place(seats, proxies):
    ctx = seats(**{C1: "max", C2: "max"})

    def click():
        with ctx.locked():
            switch(ctx, ctx.load_state(), "claude", C2, sync=False, mirror=False, manual=True)

    spawn = Spawn(([click, None], 0))
    assert _run(ctx, spawn) == 0
    assert spawn.stops == 0
    assert proxies[0].seat == Seat(C2, f"tok-{C2}")
    assert ctx.load_state().active("claude") == C2
