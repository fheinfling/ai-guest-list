"""In-place switching glue: gates, eligibility, the hit hand-off and token maintenance."""
import json
import threading
import time
from datetime import timedelta

import pytest

from acctsw import seatkind, seatswap
from acctsw.util import iso, now
from tests.conftest import make_claude_blob

pytestmark = pytest.mark.crossings

T = "claude"


@pytest.fixture(autouse=True)
def _no_machine_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(seatswap, "MANAGED_SETTINGS", tmp_path / "_managed" / "managed-settings.json")


def blob(token="tok", *, minutes=60, sub="max", expiry="set"):
    value = json.loads(make_claude_blob(sub))
    value["claudeAiOauth"]["accessToken"] = token
    if expiry == "set":
        value["claudeAiOauth"]["expiresAt"] = (now() + timedelta(minutes=minutes)).timestamp() * 1000
    else:
        value["claudeAiOauth"].pop("expiresAt")
    return json.dumps(value)


def put_seat(ctx, email, plan="Max", **kw):
    ctx.claude_home(email).mkdir(parents=True, exist_ok=True)
    ctx.claude_cred(email).set_live(blob(**{"sub": plan.lower(), **kw}))
    state = ctx.load_state()
    state.upsert_seat(T, email, plan=plan)
    if state.active(T) is None:
        state.set_active(T, email)
    state.save()


def rest(ctx, email, hours=2):
    state = ctx.load_state()
    state.set_limited_until(T, email, iso(now() + timedelta(hours=hours)), source="usage")
    state.save()


# --- gates -----------------------------------------------------------------------------------------

def test_enabled_by_default(ctx, tmp_path):
    assert seatswap.gate(ctx, {}, cwd=tmp_path) is None
    assert seatswap.enabled(ctx, {}, cwd=tmp_path)


def test_env_switch_turns_it_off(ctx, tmp_path):
    assert not seatswap.enabled(ctx, {"ACCTSW_IN_PLACE_SWITCH": "0"}, cwd=tmp_path)
    assert seatswap.enabled(ctx, {"ACCTSW_IN_PLACE_SWITCH": "1"}, cwd=tmp_path)


def test_the_setting_turns_it_off(ctx, tmp_path):
    state = ctx.load_state()
    state.set_setting("in_place_switch", False)
    state.save()
    assert seatswap.gate(ctx, {}, cwd=tmp_path) == "setting off"


@pytest.mark.parametrize("name, value", [
    ("ANTHROPIC_BASE_URL", "https://gateway.example"), ("ANTHROPIC_API_KEY", "k"),
    ("ANTHROPIC_AUTH_TOKEN", "t"), ("CLAUDE_CODE_OAUTH_TOKEN", "o"),
    ("CLAUDE_CODE_USE_BEDROCK", "1"), ("CLAUDE_CODE_USE_VERTEX", "true"),
    ("CLAUDE_CODE_USE_FOUNDRY", "1"),
])
def test_env_routing_or_auth_turns_it_off(ctx, tmp_path, name, value):
    assert seatswap.gate(ctx, {name: value}, cwd=tmp_path) == f"env {name}"


def test_falsy_provider_flags_and_empty_values_do_not_gate(ctx, tmp_path):
    env = {"CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDE_CODE_USE_VERTEX": "", "ANTHROPIC_API_KEY": ""}
    assert seatswap.enabled(ctx, env, cwd=tmp_path)


@pytest.mark.parametrize("where", ["user", "project", "project-local", "managed"])
@pytest.mark.parametrize("content, reason", [
    ({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}}, "env ANTHROPIC_BASE_URL"),
    ({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}, "env CLAUDE_CODE_USE_BEDROCK"),
    ({"env": {"ANTHROPIC_API_KEY": "k"}}, "env ANTHROPIC_API_KEY"),
    ({"apiKeyHelper": "~/bin/key.sh"}, "apiKeyHelper"),
])
def test_claude_settings_routing_turns_it_off(ctx, tmp_path, where, content, reason):
    path = {"user": ctx._claude_real / "settings.json",
            "project": tmp_path / "proj" / ".claude" / "settings.json",
            "project-local": tmp_path / "proj" / ".claude" / "settings.local.json",
            "managed": seatswap.MANAGED_SETTINGS}[where]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))
    (tmp_path / "proj").mkdir(exist_ok=True)
    found = seatswap.gate(ctx, {}, cwd=tmp_path / "proj")
    assert found is not None and found.endswith(reason)


def test_unreadable_settings_do_not_gate(ctx, tmp_path):
    path = ctx._claude_real / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert seatswap.enabled(ctx, {}, cwd=tmp_path)


# --- eligibility -----------------------------------------------------------------------------------

def test_eligible_returns_the_token(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="tok-b", minutes=60)
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") == "tok-b"


def test_eligible_needs_more_than_the_minimum_ttl(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", minutes=14)
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None
    put_seat(ctx, "c@x", minutes=16)
    assert seatswap.eligible(ctx, "c@x", home_email="home@x")


def test_unknown_expiry_is_not_eligible(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", expiry=None)
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


def test_quarantined_seat_is_not_eligible(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x")
    (ctx.claude_home("b@x") / ".acctsw-quarantined").write_text("1")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


def test_a_different_tier_than_home_is_not_eligible(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "b@x", plan="Pro")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


def test_an_unknown_home_tier_does_not_block(ctx):
    put_seat(ctx, "b@x", plan="Pro")
    assert seatswap.eligible(ctx, "b@x", home_email="nobody@x") is not None


def test_missing_item_or_token_is_not_eligible(ctx):
    put_seat(ctx, "home@x")
    assert seatswap.eligible(ctx, "ghost@x", home_email="home@x") is None
    put_seat(ctx, "b@x", token="")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


# --- SeatSwap with a fake proxy --------------------------------------------------------------------

class FakeProxy:
    def __init__(self, decide=None, **_kw):
        self.decide = decide
        self.seat = seatswap._seat("", None)
        self.stops = 0
        self.binds = []
        self.flight = 0
        self.idle = 60.0
        self.running = False

    def start(self):
        self.running = True
        return "http://127.0.0.1:40404/s3cret"

    def stop(self):
        self.running = False
        self.stops += 1

    def bind(self, seat):
        self.seat = seat
        self.binds.append(seat)

    def bound(self):
        return self.seat

    def in_flight(self):
        return self.flight

    def idle_for(self):
        return self.idle


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(ctx, home="home@x", **kw):
    clock = Clock()
    swap = seatswap.SeatSwap(ctx, home, proxy_factory=FakeProxy, clock=clock, **kw)
    return swap, swap.proxy, clock


def test_start_binds_home_passthrough_and_env_points_at_the_proxy(ctx):
    swap, proxy, _ = make(ctx)
    assert proxy.running and proxy.bound().email == "home@x" and proxy.bound().token is None
    assert swap.bound() == "home@x"
    assert swap.start() == "http://127.0.0.1:40404/s3cret" and len(proxy.binds) == 1
    env = swap.env({"PATH": "/bin", "NO_PROXY": "corp.example"})
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:40404/s3cret"
    assert env["NO_PROXY"].split(",") == ["corp.example", "127.0.0.1", "localhost"]
    assert env["no_proxy"] == "127.0.0.1,localhost"
    assert env["PATH"] == "/bin"
    swap.stop()


def hit(email="home@x"):
    return seatswap._types()[1]("limit", email, None, None, None)


def test_hit_round_trip_wakes_the_main_thread_and_binds_the_answer(ctx):
    swap, proxy, _ = make(ctx)
    got = {}
    worker = threading.Thread(target=lambda: got.setdefault("seat", swap._decide(hit())))
    worker.start()
    import select
    ready, _, _ = select.select([swap.wake_fd], [], [], 5)
    assert ready
    [h] = swap.pending()
    target = seatswap._seat("b@x", "tok-b")
    assert swap.answer(h, target)
    worker.join(5)
    assert got["seat"] == target and proxy.bound() == target
    assert swap.pending() == []
    swap.stop()


def test_an_unanswered_hit_times_out_and_a_late_answer_is_refused(ctx):
    swap, proxy, _ = make(ctx, decide_timeout=0.05)
    h = hit()
    assert swap._decide(h) is None
    assert not swap.answer(h, seatswap._seat("b@x", "tok-b"))
    assert proxy.bound().email == "home@x"
    swap.stop()


def test_maintain_is_idle_at_home(ctx):
    swap, proxy, _ = make(ctx)
    put_seat(ctx, "home@x")
    assert swap.maintain() is None
    assert len(proxy.binds) == 1


def test_maintain_rebinds_a_newer_token_at_most_once_a_minute(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="old")
    swap, proxy, clock = make(ctx)
    proxy.bind(seatswap._seat("b@x", "old"))
    ctx.claude_cred("b@x").set_live(blob("new"))
    assert swap.maintain() is None
    assert proxy.bound() == seatswap._seat("b@x", "new")
    ctx.claude_cred("b@x").set_live(blob("newer"))
    clock.t += seatswap.MAINTAIN_EVERY_S - 1
    swap.maintain()
    assert proxy.bound().token == "new"
    clock.t += 1
    swap.maintain()
    assert proxy.bound().token == "newer"


def test_maintain_goes_home_when_the_borrowed_token_lapses(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    swap, proxy, _ = make(ctx)
    proxy.bind(seatswap._seat("b@x", "b"))
    assert swap.maintain() is None
    assert proxy.bound() == seatswap._seat("home@x", None)


def test_maintain_moves_to_another_seat_when_home_is_resting(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    put_seat(ctx, "c@x", token="c")
    rest(ctx, "home@x")
    swap, proxy, _ = make(ctx)
    proxy.bind(seatswap._seat("b@x", "b"))
    swap.maintain()
    assert proxy.bound() == seatswap._seat("c@x", "c")


def test_maintain_asks_for_a_handover_only_once_idle(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    rest(ctx, "home@x")
    swap, proxy, clock = make(ctx)
    proxy.bind(seatswap._seat("b@x", "b"))
    proxy.flight = 1
    assert swap.maintain() is None
    proxy.flight, proxy.idle = 0, 1.0
    assert swap.maintain() is None
    proxy.idle = seatswap.HANDOVER_IDLE_S
    assert swap.maintain() == "handover"
    swap.set_home("b@x")
    assert proxy.bound() == seatswap._seat("b@x", None)
    assert swap.maintain() is None


def test_maintain_never_crosses_without_approval(ctx):
    put_seat(ctx, "home@x", plan="Team")
    put_seat(ctx, "b@x", plan="Team", token="b", minutes=10)
    put_seat(ctx, "p@x", plan="Max", token="p")
    rest(ctx, "home@x")
    swap, proxy, _ = make(ctx)
    proxy.bind(seatswap._seat("b@x", "b"))
    assert swap.maintain() == "handover"
    assert proxy.bound().email == "b@x"


def test_pick_spends_a_once_approval_and_not_the_reverse(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "w@x", plan="Team", token="w")
    rest(ctx, "home@x")
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x") is None
    state = ctx.load_state()
    seatkind.approve(state, T, "w@x", "home@x", "always")
    state.save()
    assert swap.pick(ctx, "home@x") is None
    state = ctx.load_state()
    seatkind.approve(state, T, "home@x", "w@x", "once")
    state.save()
    # Tier differs (team vs max) so it is not eligible in place even when approved.
    assert swap.pick(ctx, "home@x") is None
    ctx.claude_cred("w@x").set_live(blob("w", sub="max"))
    assert swap.pick(ctx, "home@x") == seatswap._seat("w@x", "w")
    assert not seatkind.approved(ctx.load_state(), T, "home@x", "w@x")
    assert swap.pick(ctx, "home@x") is None


def test_pick_without_consume_leaves_the_approval(ctx):
    put_seat(ctx, "home@x", plan="Max")
    put_seat(ctx, "w@x", plan="Team", token="w", sub="max")
    rest(ctx, "home@x")
    state = ctx.load_state()
    seatkind.approve(state, T, "home@x", "w@x", "once")
    state.save()
    swap, _, _ = make(ctx)
    assert swap.pick(ctx, "home@x", consume=False).email == "w@x"
    assert seatkind.approved(ctx.load_state(), T, "home@x", "w@x")


def test_stop_releases_a_waiting_proxy_thread(ctx):
    swap, _, _ = make(ctx, decide_timeout=30)
    got = {}
    worker = threading.Thread(target=lambda: got.setdefault("seat", swap._decide(hit())))
    worker.start()
    deadline = time.monotonic() + 5
    while not swap.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    swap.stop()
    worker.join(5)
    assert not worker.is_alive() and got["seat"] is None


def test_constructed_without_a_home_until_set_home(ctx):
    swap = seatswap.SeatSwap(ctx, proxy_factory=FakeProxy)
    assert swap.proxy.running and swap.bound() is None and swap.proxy.binds == []
    assert swap.maintain() is None
    swap.set_home("a@x")
    assert swap.bound() == "a@x" and swap.proxy.bound().token is None
    swap.bind(seatswap._seat("b@x", "tok-b"))
    assert swap.bound() == "b@x"
    swap.stop()
    swap.stop()
    assert swap.proxy.stops == 1


def test_a_proxy_that_cannot_bind_raises_and_leaks_nothing(ctx):
    import os

    class Broken(FakeProxy):
        def start(self):
            raise OSError("bind refused")
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(OSError):
        seatswap.SeatSwap(ctx, "a@x", proxy_factory=Broken)
    assert len(os.listdir("/dev/fd")) == before


def test_eligible_without_a_home_skips_only_the_tier_check(ctx):
    put_seat(ctx, "b@x", plan="Pro")
    assert seatswap.eligible(ctx, "b@x", home_email=None)
