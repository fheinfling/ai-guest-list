"""In-place switching glue: gates, eligibility, the hit hand-off and token maintenance."""
import json
import os
import threading
import time
from datetime import timedelta

import pytest

from acctsw import seatkind, seatswap
from acctsw.seatproxy import Hit, Seat
from acctsw.util import iso, now
from tests.conftest import make_claude_blob

pytestmark = pytest.mark.crossings

T = "claude"


@pytest.fixture(autouse=True)
def _no_machine_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(seatswap, "MANAGED_SETTINGS", tmp_path / "_managed" / "managed-settings.json")
    monkeypatch.setattr(seatswap, "MANAGED_SETTINGS_DIR", tmp_path / "_managed" / "managed-settings.d")


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


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_a_falsy_env_switch_turns_it_off(ctx, tmp_path, value):
    assert not seatswap.enabled(ctx, {"ACCTSW_IN_PLACE_SWITCH": value}, cwd=tmp_path)


@pytest.mark.parametrize("value", ["1", "true", "yes", ""])
def test_a_truthy_or_empty_env_switch_leaves_it_on(ctx, tmp_path, value):
    assert seatswap.enabled(ctx, {"ACCTSW_IN_PLACE_SWITCH": value}, cwd=tmp_path)


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

_REAL_CA = []


def real_ca():
    """One genuine SessionCA per test run (an openssl call costs ~100ms)."""
    if not _REAL_CA:
        from acctsw.sessionca import SessionCA
        _REAL_CA.append(SessionCA())
    return _REAL_CA[0]


class FakeCA:
    """The SessionCA surface: real TLS material, fake bundle files, counted close()."""

    def __init__(self, pem=None):
        self.closes = 0
        self.pem = pem

    def ca_pem(self):
        return self.pem if self.pem is not None else real_ca().ca_pem()

    def server_context(self):
        return real_ca().server_context()

    def bundle_paths(self, environ):
        self.bundle_base = dict(environ)
        return {"NODE_EXTRA_CA_CERTS": "/ca/node.pem", "SSL_CERT_FILE": "/ca/full.pem",
                "REQUESTS_CA_BUNDLE": "/ca/full.pem", "CURL_CA_BUNDLE": "/ca/full.pem"}

    def close(self):
        self.closes += 1


class FakeProxy:
    def __init__(self, decide=None, **kw):
        self.kw = kw
        self.decide = decide
        self.seat = Seat("", None)
        self.stops = 0
        self.binds = []
        self.flight = 0
        self.idle = 60.0
        self.running = False
        self.preflight_reason = None
        self.seen = 0

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

    def preflight(self):
        return self.preflight_reason

    def messages_seen(self):
        return self.seen


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(ctx, home="home@x", **kw):
    clock = Clock()
    kw.setdefault("ca", FakeCA())
    kw.setdefault("environ", {})
    swap = seatswap.SeatSwap(ctx, home, proxy_factory=FakeProxy, clock=clock, **kw)
    return swap, swap.proxy, clock


def test_start_binds_home_passthrough_and_env_points_at_the_proxy(ctx):
    swap, proxy, _ = make(ctx)
    assert proxy.running and proxy.bound().email == "home@x" and proxy.bound().token is None
    assert swap.bound() == "home@x"
    assert swap.start() == "http://127.0.0.1:40404/s3cret" and len(proxy.binds) == 1
    env = swap.env({"PATH": "/bin", "NO_PROXY": "corp.example"})
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://127.0.0.1:40404/s3cret"
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["NO_PROXY"].split(",") == ["corp.example", "127.0.0.1", "localhost"]
    assert env["no_proxy"] == "127.0.0.1,localhost"
    assert env["PATH"] == "/bin"
    swap.stop()


def hit(email="home@x"):
    return Hit("limit", email, None, None, None)


def test_hit_round_trip_wakes_the_main_thread_and_binds_the_answer(ctx):
    swap, proxy, _ = make(ctx)
    got = {}
    worker = threading.Thread(target=lambda: got.setdefault("seat", swap._decide(hit())))
    worker.start()
    import select
    ready, _, _ = select.select([swap.wake_fd], [], [], 5)
    assert ready
    [h] = swap.pending()
    target = Seat("b@x", "tok-b")
    assert swap.answer(h, target)
    worker.join(5)
    assert got["seat"] == target and proxy.bound() == target
    assert swap.pending() == []
    swap.stop()


def test_an_unanswered_hit_times_out_and_a_late_answer_is_refused(ctx):
    swap, proxy, _ = make(ctx, decide_timeout=0.05)
    h = hit()
    assert swap._decide(h) is None
    assert not swap.answer(h, Seat("b@x", "tok-b"))
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
    proxy.bind(Seat("b@x", "old"))
    ctx.claude_cred("b@x").set_live(blob("new"))
    assert swap.maintain() is None
    assert proxy.bound() == Seat("b@x", "new")
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
    proxy.bind(Seat("b@x", "b"))
    assert swap.maintain() is None
    assert proxy.bound() == Seat("home@x", None)


def test_maintain_moves_to_another_seat_when_home_is_resting(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    put_seat(ctx, "c@x", token="c")
    rest(ctx, "home@x")
    swap, proxy, _ = make(ctx)
    proxy.bind(Seat("b@x", "b"))
    swap.maintain()
    assert proxy.bound() == Seat("c@x", "c")


def test_maintain_asks_for_a_handover_only_once_idle(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    rest(ctx, "home@x")
    swap, proxy, clock = make(ctx)
    proxy.bind(Seat("b@x", "b"))
    proxy.flight = 1
    assert swap.maintain() is None
    proxy.flight, proxy.idle = 0, 1.0
    assert swap.maintain() is None
    proxy.idle = seatswap.HANDOVER_IDLE_S
    assert swap.maintain() == "handover"
    swap.set_home("b@x")
    assert proxy.bound() == Seat("b@x", None)
    assert swap.maintain() is None


def test_maintain_never_crosses_without_approval(ctx):
    put_seat(ctx, "home@x", plan="Team")
    put_seat(ctx, "b@x", plan="Team", token="b", minutes=10)
    put_seat(ctx, "p@x", plan="Max", token="p")
    rest(ctx, "home@x")
    swap, proxy, _ = make(ctx)
    proxy.bind(Seat("b@x", "b"))
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
    assert swap.pick(ctx, "home@x") == Seat("w@x", "w")
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
    swap = seatswap.SeatSwap(ctx, proxy_factory=FakeProxy, ca=FakeCA())
    assert swap.proxy.running and swap.bound() is None and swap.proxy.binds == []
    assert swap.maintain() is None
    swap.set_home("a@x")
    assert swap.bound() == "a@x" and swap.proxy.bound().token is None
    swap.bind(Seat("b@x", "tok-b"))
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
        seatswap.SeatSwap(ctx, "a@x", proxy_factory=Broken, ca=FakeCA())
    assert len(os.listdir("/dev/fd")) == before


def test_eligible_without_a_home_skips_only_the_tier_check(ctx):
    put_seat(ctx, "b@x", plan="Pro")
    assert seatswap.eligible(ctx, "b@x", home_email=None)


# --- review round: gate sources, read errors, home, preflight -----------------------------------

ROUTED = {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}}


@pytest.mark.parametrize("form", ["file", "relative", "json", "equals"])
def test_child_settings_flag_gates(ctx, tmp_path, form):
    (tmp_path / "s.json").write_text(json.dumps(ROUTED))
    args = {"file": ["--settings", str(tmp_path / "s.json")],
            "relative": ["-p", "hi", "--settings", "s.json"],
            "json": ["--settings", json.dumps(ROUTED)],
            "equals": [f"--settings={json.dumps(ROUTED)}"]}[form]
    assert seatswap.gate(ctx, {}, cwd=tmp_path, args=args) == "--settings env ANTHROPIC_BASE_URL"
    assert seatswap.enabled(ctx, {}, cwd=tmp_path, args=["--settings", '{"model": "x"}'])


def test_an_unreadable_child_settings_flag_gates(ctx, tmp_path):
    for args in (["--settings", str(tmp_path / "missing.json")], ["--settings", "{broken"],
                 ["--settings"]):
        assert seatswap.gate(ctx, {}, cwd=tmp_path, args=args) == "--settings unreadable"


def test_managed_settings_drop_ins_gate(ctx, tmp_path):
    drop = seatswap.MANAGED_SETTINGS_DIR
    drop.mkdir(parents=True)
    (drop / "00-ok.json").write_text(json.dumps({"model": "x"}))
    assert seatswap.enabled(ctx, {}, cwd=tmp_path)
    (drop / "10-gateway.json").write_text(json.dumps({"apiKeyHelper": "/bin/key"}))
    assert seatswap.gate(ctx, {}, cwd=tmp_path) == "10-gateway.json apiKeyHelper"


def _locked_keychain(monkeypatch, *emails):
    from acctsw import claudehome
    from acctsw.keychain import KeychainError
    real = claudehome.load

    def load(ctx, email):
        if email in emails:
            raise KeychainError("keychain read failed")
        return real(ctx, email)
    monkeypatch.setattr(claudehome, "load", load)


@pytest.mark.parametrize("locked", ["seat", "home"])
def test_a_keychain_error_is_not_eligible(ctx, monkeypatch, locked):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x")
    _locked_keychain(monkeypatch, "b@x" if locked == "seat" else "home@x")
    assert seatswap.eligible(ctx, "b@x", home_email="home@x") is None


def test_maintain_keeps_the_binding_when_the_keychain_is_unreadable(ctx, monkeypatch):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=60)
    swap, proxy, clock = make(ctx)
    proxy.bind(Seat("b@x", "b"))
    _locked_keychain(monkeypatch, "b@x")
    for _ in range(3):
        assert swap.maintain() is None
        clock.t += seatswap.MAINTAIN_EVERY_S
    assert proxy.bound() == Seat("b@x", "b") and len(proxy.binds) == 2


def test_maintain_goes_home_without_approval_even_across_kinds(ctx):
    put_seat(ctx, "home@x", plan="Team")
    put_seat(ctx, "p@x", plan="Max", token="p", minutes=10)
    swap, proxy, _ = make(ctx)
    proxy.bind(Seat("p@x", "p"))
    assert swap.maintain() is None
    assert proxy.bound() == Seat("home@x", None)


def test_proxy_used_delegates(ctx):
    swap, proxy, _ = make(ctx)
    assert not swap.proxy_used()
    proxy.seen = 1
    assert swap.proxy_used()
    swap.stop()


def _age_cache(ctx, minutes):
    path = ctx.data_dir / seatswap.PREFLIGHT_CACHE
    cache = json.loads(path.read_text())
    for entry in cache.values():
        entry["at"] = iso(now() - timedelta(minutes=minutes))
    path.write_text(json.dumps(cache))


def test_preflight_verdict_is_cached_per_upstream_for_ten_minutes(ctx):
    ctx.ensure_dirs()
    swap, proxy, _ = make(ctx)
    calls = []
    proxy.preflight = lambda: calls.append(1) or proxy.preflight_reason
    assert swap.preflight() is None and len(calls) == 1
    proxy.preflight_reason = "tls: certificate verify failed"
    assert swap.preflight() is None and len(calls) == 1          # cached ok
    _age_cache(ctx, 9)
    assert swap.preflight() is None and len(calls) == 1
    _age_cache(ctx, 11)
    assert swap.preflight() == "tls: certificate verify failed" and len(calls) == 2
    proxy.preflight_reason = None
    assert swap.preflight() == "tls: certificate verify failed"   # a failure is cached too
    other = seatswap.SeatSwap(ctx, "home@x", proxy_factory=FakeProxy, upstream="https://gw.example",
                              ca=FakeCA())
    assert other.preflight() is None                             # its own upstream, its own verdict
    cache = json.loads((ctx.data_dir / seatswap.PREFLIGHT_CACHE).read_text())
    assert set(cache) == {seatswap.UPSTREAM, "https://gw.example"}
    assert cache[seatswap.UPSTREAM] == {"ok": False, "reason": "tls: certificate verify failed",
                                        "at": cache[seatswap.UPSTREAM]["at"]}


def test_an_unreadable_preflight_cache_just_preflights(ctx):
    ctx.ensure_dirs()
    (ctx.data_dir / seatswap.PREFLIGHT_CACHE).write_text("{broken")
    swap, proxy, _ = make(ctx)
    proxy.preflight_reason = "unreachable: ConnectionRefusedError"
    assert swap.preflight() == "unreachable: ConnectionRefusedError"


# --- N9: a third seat only with auto_switch, in_place_switch and the hop budget ------------------

def _lapsing_on_b(ctx):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="b", minutes=10)
    put_seat(ctx, "c@x", token="c")
    rest(ctx, "home@x")
    swap, proxy, _ = make(ctx)
    proxy.bind(Seat("b@x", "b"))
    return swap, proxy


@pytest.mark.parametrize("setting", ["auto_switch", "in_place_switch"])
def test_maintain_never_moves_to_a_third_seat_with_a_setting_off(ctx, setting):
    swap, proxy = _lapsing_on_b(ctx)
    state = ctx.load_state()
    state.set_setting(setting, False)
    state.save()
    assert swap.maintain() == "handover"
    assert proxy.bound().email == "b@x" and swap.third_seat_moves == 0


def test_maintain_never_moves_to_a_third_seat_past_the_hop_budget(ctx):
    swap, proxy = _lapsing_on_b(ctx)
    assert swap.maintain(third_seat=False) == "handover"
    assert proxy.bound().email == "b@x"


def test_a_third_seat_move_is_counted(ctx):
    swap, proxy = _lapsing_on_b(ctx)
    swap.maintain()
    assert proxy.bound() == Seat("c@x", "c") and swap.third_seat_moves == 1


@pytest.mark.parametrize("setting", ["auto_switch", "in_place_switch"])
def test_going_home_and_rebinding_ignore_the_settings_and_budget(ctx, setting):
    put_seat(ctx, "home@x")
    put_seat(ctx, "b@x", token="old", minutes=10)
    state = ctx.load_state()
    state.set_setting(setting, False)
    state.save()
    swap, proxy, clock = make(ctx)
    proxy.bind(Seat("b@x", "old"))
    ctx.claude_cred("b@x").set_live(blob("new"))
    swap.maintain(third_seat=False)
    assert proxy.bound() == Seat("b@x", "new")
    ctx.claude_cred("b@x").set_live(blob("new", minutes=10))
    clock.t += seatswap.MAINTAIN_EVERY_S
    swap.maintain(third_seat=False)
    assert proxy.bound() == Seat("home@x", None) and swap.third_seat_moves == 0


def test_a_hit_after_stop_never_writes_to_a_reused_descriptor(ctx):
    import os
    swap, _, _ = make(ctx)
    wake_w = swap._wake_w
    swap.stop()
    fds = []
    try:
        while True:   # take descriptors until the old write end's number is in use again
            r, w = os.pipe()
            fds += [r, w]
            if wake_w in (r, w):
                break
        reader = r if w == wake_w else None
        os.set_blocking(r, False)
        assert swap._decide(hit()) is None
        if reader is None:   # the old number came back as a read end: write to it must not occur
            return
        with pytest.raises(BlockingIOError):
            os.read(reader, 1)
    finally:
        for fd in fds:
            os.close(fd)


# --- transport v2: HTTPS_PROXY interception with a session CA -----------------------------------

def test_env_routes_https_through_the_proxy_and_trusts_the_session_ca(ctx):
    ca = FakeCA()
    swap, _, _ = make(ctx, ca=ca)
    base = {"HTTPS_PROXY": "http://corp:3128", "https_proxy": "http://corp:3128",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com", "HTTP_PROXY": "http://corp:3128",
            "NODE_EXTRA_CA_CERTS": "/user/extra.pem", "TERM": "xterm"}
    env = swap.env(base)
    assert env["HTTPS_PROXY"] == env["https_proxy"] == swap.proxy_url
    assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"   # the user's, untouched
    assert env["HTTP_PROXY"] == "http://corp:3128" and env["TERM"] == "xterm"
    assert env["NODE_EXTRA_CA_CERTS"] == "/ca/node.pem" and env["SSL_CERT_FILE"] == "/ca/full.pem"
    assert env["REQUESTS_CA_BUNDLE"] == env["CURL_CA_BUNDLE"] == "/ca/full.pem"
    assert ca.bundle_base["NODE_EXTRA_CA_CERTS"] == "/user/extra.pem"   # merged from the user's
    assert "ANTHROPIC_BASE_URL" not in swap.env({"PATH": "/bin"})


def test_env_takes_anthropic_out_of_no_proxy_and_keeps_the_rest(ctx):
    swap, _, _ = make(ctx)
    entries = ["corp.example", "api.anthropic.com", ".anthropic.com", "*.anthropic.com",
               "anthropic.com", "API.Anthropic.COM:443", " 10.0.0.0/8", "notanthropic.com",
               "anthropic.com.evil.example", "pi.anthropic.com", "[::1]", "localhost"]
    env = swap.env({"NO_PROXY": ",".join(entries), "no_proxy": "anthropic.com"})
    assert env["NO_PROXY"].split(",") == ["corp.example", "10.0.0.0/8", "notanthropic.com",
                                          "anthropic.com.evil.example", "pi.anthropic.com",
                                          "[::1]", "localhost",
                                          "127.0.0.1"]
    assert env["no_proxy"] == "127.0.0.1,localhost"


@pytest.mark.parametrize("environ, expected", [
    ({"HTTPS_PROXY": "http://corp:3128", "ALL_PROXY": "socks5://x:1"}, "http://corp:3128"),
    ({"https_proxy": "http://lower:8080"}, "http://lower:8080"),
    ({"ALL_PROXY": "http://all:1"}, "http://all:1"),
    ({"all_proxy": "http://all-lower:1"}, "http://all-lower:1"),
    ({"HTTPS_PROXY": "  ", "HTTP_PROXY": "http://plain:1"}, None),
    ({}, None),
])
def test_the_users_own_proxy_becomes_the_upstream_proxy(ctx, environ, expected):
    ca = FakeCA()
    swap, proxy, _ = make(ctx, ca=ca, environ=environ)
    assert swap.upstream_proxy == expected
    assert proxy.kw["upstream_proxy"] == expected and proxy.kw["ca"] is ca
    assert proxy.kw["on_tls_reject"] == swap._on_tls_reject


def test_a_client_rejecting_the_ca_flags_and_wakes_once(ctx):
    import select
    swap, proxy, _ = make(ctx)
    assert not swap.tls_rejected()
    proxy.kw["on_tls_reject"]("alert unknown ca")
    proxy.kw["on_tls_reject"]("alert unknown ca")
    assert swap.tls_rejected()
    assert select.select([swap.wake_fd], [], [], 1)[0]
    assert os.read(swap.wake_fd, 64) == b"!"
    swap.stop()


def test_stop_closes_the_session_ca_once(ctx):
    ca = FakeCA()
    swap, _, _ = make(ctx, ca=ca)
    swap.stop()
    swap.stop()
    assert ca.closes == 1


def test_a_failing_session_ca_raises_and_leaks_nothing(ctx):
    from acctsw.sessionca import SessionCAError

    def broken():
        raise SessionCAError("openssl unavailable: FileNotFoundError")
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(SessionCAError):
        seatswap.SeatSwap(ctx, "a@x", proxy_factory=FakeProxy, ca_factory=broken, environ={})
    assert len(os.listdir("/dev/fd")) == before


def test_a_proxy_that_cannot_start_closes_the_ca_it_was_given_by_the_factory(ctx):
    built = []

    class Broken(FakeProxy):
        def start(self):
            raise OSError("bind refused")

    def factory():
        built.append(FakeCA())
        return built[-1]
    with pytest.raises(OSError):
        seatswap.SeatSwap(ctx, "a@x", proxy_factory=Broken, ca_factory=factory, environ={})
    assert built[0].closes == 1


def test_missing_openssl_turns_it_off(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(seatswap, "OPENSSL", str(tmp_path / "no-openssl"))
    assert seatswap.gate(ctx, {}, cwd=tmp_path) == "openssl missing"


@pytest.mark.parametrize("url, gated", [
    ("https://api.anthropic.com", False), ("https://api.anthropic.com/", False),
    ("https://API.anthropic.com:443", False), ("http://api.anthropic.com", True),
    ("https://api.anthropic.com.evil.example", True), ("https://gateway.example", True),
    ("https://api.anthropic.com:8443", True), ("not a url", True),
])
def test_a_first_party_base_url_does_not_gate(ctx, tmp_path, url, gated):
    assert (seatswap.gate(ctx, {"ANTHROPIC_BASE_URL": url}, cwd=tmp_path) is not None) is gated
    path = ctx._claude_real / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}))
    assert (seatswap.gate(ctx, {}, cwd=tmp_path) is not None) is gated


def test_preflight_self_test_passes_with_the_session_ca(ctx):
    swap, _, _ = make(ctx)
    assert swap._ca_selftest() is None
    assert swap.preflight() is None


def test_preflight_self_test_fails_when_the_ca_is_not_the_servers(ctx, tmp_path):
    from acctsw.sessionca import SessionCA
    other = SessionCA()
    try:
        swap, proxy, _ = make(ctx, ca=FakeCA(pem=other.ca_pem()))
        calls = []
        proxy.preflight = lambda: calls.append(1)
        reason = swap.preflight()
        assert reason and reason.startswith("local ca:") and calls == []
    finally:
        other.close()
