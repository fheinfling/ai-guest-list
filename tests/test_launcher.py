"""Feature tests for the supervised launcher — auto-switch + resume, via a scripted fake spawn.

No real PTY, no network: `spawn` is injected and `get` returns canned usage.
"""
import json
import os
import threading
import time

import pytest

from acctsw import accounts as acct
from acctsw import launcher as L
from acctsw.launcher import (NoSeats, build_cmd, resume_cmd, detect_limit, detect_auth_dead,
                             detect_hard_limit, handle_limit, handle_auth_dead, run)
from acctsw.util import now, iso
from datetime import timedelta
from tests.conftest import make_claude_blob, make_codex_blob
from tests.test_rollout import (OUT_OF_CREDITS, task_complete_error_line, token_count_line,
                                window, write_rollout)
from tests.test_usage import (fake_get, claude_ok_body, codex_credits_depleted_body,
                              codex_ok_body)
from acctsw import paths as P


# --- pure helpers -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Error: you've hit your usage limit", "rate limit exceeded", "5-hour limit reached",
    "HTTP 429 Too Many Requests", "you are out of credits",
    # Real banners whose committal wording varies — must still be caught (recall over the exact token)
    "usage limit exceeded", "you've reached your usage limit",
])
def test_detect_limit_positive(text):
    assert detect_limit("codex", text) or detect_limit("claude", text)


@pytest.mark.parametrize("text", [
    "5-hour limit reached", "5-hour limit · resets 8pm", "weekly limit · resets Monday",
])
def test_detect_limit_claude_window_banners(text):
    """Claude's own window-limit banners are caught even when they omit "reached" (e.g. a status
    line that only says "resets") — the corroboration guard vetoes any false positive."""
    assert detect_limit("claude", text)


def test_detect_limit_negative():
    assert not detect_limit("codex", "compiling project, running tests, all green")


@pytest.mark.parametrize("benign", [
    "the cache resets at midnight", "please try again in a moment",
    "approaching the recursion limit", "rate limiting middleware installed",
    # Real false positives that killed healthy sessions (the model narrating ABOUT limits while
    # developing this repo). None are the tool's own committal banner, so none may match.
    "the usage limit detector fired on the agent's own text",
    "all claude seats are resting; soonest unlocks at 17:57",
    "2 MCP servers need authentication · run /mcp",
])
def test_detect_limit_no_false_positive_on_benign(benign):
    assert not detect_limit("codex", benign)
    assert not detect_limit("claude", benign)


@pytest.mark.parametrize("text", [
    # Claude Code's own server-overload error: a transient 429/529, NOT the account's usage limit.
    "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
    "Rate limited — this is not your usage limit, please retry",
])
def test_detect_limit_ignores_server_throttle(text):
    """A server-side throttle that explicitly disclaims the usage limit must never read as one —
    otherwise a transient overload wrongly rests/switches a seat (the false-positive class this guards)."""
    assert not detect_limit("codex", text)
    assert not detect_limit("claude", text)
    from acctsw.launcher import detect_event
    assert detect_event("claude", text) is None


def test_detect_limit_claude_ignores_codex_credit_narration():
    """A Claude session narrating about Codex credits ("out of credits") must NOT read as a Claude
    limit — Claude Code never emits that wording as a banner. This is the exact string that kept
    killing live Claude sessions."""
    narration = "the routed Codex workspace is out of credits"
    assert not detect_limit("claude", narration)
    assert detect_limit("codex", narration)  # still a real signal for an actual codex session


def test_detect_limit_ignores_ansi_codes():
    colored = "\x1b[31musage\x1b[0m \x1b[1mlimit\x1b[0m reached"
    assert detect_limit("codex", colored)


def test_detect_hard_limit_matches_codex_workspace_credits_banner():
    banner = "■ Your workspace is out of credits. Add credits to continue."
    assert detect_hard_limit("codex", banner)
    assert not detect_hard_limit("claude", banner)
    assert not detect_hard_limit("codex", "the routed Codex workspace is out of credits")
    assert not detect_hard_limit(
        "codex",
        "the routed Codex workspace is out of credits. Add credits to continue",
    )
    assert not detect_hard_limit(
        "codex",
        "Codex printed: Your workspace is out of credits. Add credits to continue.",
    )


def test_build_and_resume_cmd(ctx):
    assert build_cmd(ctx, "codex", ["exec", "hi"])[-2:] == ["exec", "hi"]
    assert resume_cmd(ctx, "codex")[-2:] == ["resume", "--last"]
    assert resume_cmd(ctx, "claude")[-1] == "--continue"


# --- fixtures ---------------------------------------------------------------------------------

def _two_codex(ctx):
    for em in ("a@x.com", "b@x.com"):
        ctx.cred["codex"].set_live(make_codex_blob(em))
        state = ctx.load_state()
        acct.add(ctx, state, "codex", email=em)
    # active is b (last added); make a the active starting seat for clarity
    from acctsw.switch import switch
    state = ctx.load_state()
    switch(ctx, state, "codex", "a@x.com")
    return ctx.load_state()


def _two_realistic_codex_aliases(ctx):
    emails = ("primary@example.test", "primary+codex@example.test")
    for em in emails:
        ctx.cred["codex"].set_live(make_codex_blob(em, account_id=f"acct:{em}"))
        state = ctx.load_state()
        acct.add(ctx, state, "codex", email=em)
    from acctsw.switch import switch
    state = ctx.load_state()
    switch(ctx, state, "codex", emails[0])
    return ctx.load_state()


def _two_claude(ctx):
    for em in ("c1@x.com", "c2@x.com"):
        ctx.cred["claude"].set_live(make_claude_blob())
        state = ctx.load_state()
        acct.add(ctx, state, "claude", email=em)
    from acctsw.switch import switch
    state = ctx.load_state()
    switch(ctx, state, "claude", "c1@x.com")
    return ctx.load_state()


class FakeSpawn:
    """Returns scripted ``(output, status)`` — or ``(output, status, ticks)`` — per call; records
    argv of each launch. ``output`` may be a list of chunks to exercise repeated on_output calls
    within ONE child session (None for a silent child); like the real pty_spawn, the child "dies"
    (remaining chunks dropped) once the supervisor asks for a stop.

    ``ticks`` scripts the supervisor's heartbeat: one entry per tick, each either None or a callable
    run just before that tick — the hook stands in for whatever happened outside this process (the
    child appending to its rollout log, the menubar resting a seat in state.json). One tick fires
    after each output chunk, then any remaining scripted ticks run with no further output, so a
    silent child is scripted as ``(None, status, [hook, None, None])``.
    """

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []
        self.stops = 0
        self.ticks = 0

    def __call__(self, argv, on_output, on_tick=None):
        script = self.scripts[len(self.calls)]
        out, status = script[0], script[1]
        hooks = list(script[2]) if len(script) > 2 else []
        self.calls.append(list(argv))
        chunks = out if isinstance(out, list) else ([] if out is None else [out])
        for chunk in chunks:
            if on_output(chunk):
                self.stops += 1
                return status
            if self._tick(on_tick, hooks):
                self.stops += 1
                return status
        while hooks:
            if self._tick(on_tick, hooks):
                self.stops += 1
                return status
        return status

    def _tick(self, on_tick, hooks) -> bool:
        if on_tick is None or not hooks:
            return False
        hook = hooks.pop(0)
        if callable(hook):
            hook()
        self.ticks += 1
        return bool(on_tick())


# --- handle_limit -----------------------------------------------------------------------------

def test_handle_limit_switches_when_alternative_available(ctx):
    state = _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email == "b@x.com"
    # active seat a is now flagged limited
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None


def test_handle_limit_reactive_fallback_when_seat_near_max(ctx):
    """Positive evidence with no authoritative reset: the endpoint answers "ok" and the seat sits in
    the near-max band (≥ FALSE_ALARM_MAX_PCT but < 100, so usage doesn't stamp a reset) — the reactive
    fallback flags it so we don't immediately re-pick the maxed seat, then hops away."""
    state = _two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=95.0, secondary=20.0))})
    dec = handle_limit(ctx, state, "codex", get=get)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None
    assert seat["limit_source"] == "reactive"
    assert dec.action == "switch" and dec.email == "b@x.com"


def test_handle_limit_resumes_when_endpoint_unreachable(ctx):
    """The false 'all seats resting' kill: a stdout limit signal the usage endpoint can't verify
    (network down, or the Headroom proxy in front of it flapping) is NOT positive evidence — it must
    not burn a 5h rest. With no way to confirm, keep working on the same seat, never lock it out."""
    state = _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (0, "")})   # connection failure → status "network"
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "resume" and dec.email == "a@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None
    assert state.active("codex") == "a@x.com"


def test_handle_limit_resumes_when_usage_endpoint_throttled(ctx):
    """A 429 from the USAGE endpoint is that endpoint throttling us (it rate-limits hard), NOT the
    account's quota — and a transient server 429 ("temporarily limiting requests, not your usage
    limit") is not an out-of-quota banner. Inconclusive → resume the same seat, never a 5h rest."""
    state = _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "resume" and dec.email == "a@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None


def test_handle_limit_resumes_on_unauthorized_401(ctx):
    """A 401 is routine and inconclusive — a cached access token expired, and a refresh fixes it
    when the seat becomes active. It must never rest the seat or cost the running session: resume."""
    state = _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (401, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "resume" and dec.email == "a@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None
    assert state.active("codex") == "a@x.com"


def test_handle_limit_hops_off_forbidden_403_without_resting_it(ctx):
    """403 is the one auth error that IS evidence: the endpoint answered "not entitled" — the
    subscription was cancelled or terminated. Resting it would advertise a reset that never comes,
    so the seat is LEFT (not rested) for an entitled one, exactly like a dead token."""
    state = _two_codex(ctx)  # active a, healthy b
    get = fake_get({P.CODEX_USAGE_URL: (403, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email == "b@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None  # never a phantom reset


def test_handle_limit_forbidden_with_no_other_seat_gives_up_without_resting(ctx):
    """A revoked-entitlement seat with nowhere to hop must still not be rested — and per the
    supervisor's rule the caller keeps the session running rather than killing it."""
    state = _two_codex(ctx)
    state.remove_seat("codex", "b@x.com")
    state.save()
    get = fake_get({P.CODEX_USAGE_URL: (403, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "give_up"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None


def test_handle_limit_switches_off_stale_active_pointer(ctx):
    """If the active pointer is stale (its account was removed mid-run), an inconclusive probe must
    NOT resume the phantom seat — it falls through to choose() a real, available seat."""
    state = _two_codex(ctx)  # a, b real; active a
    state.set_active("codex", "ghost@x.com")   # stale pointer, not in accounts
    state.save()
    get = fake_get({P.CODEX_USAGE_URL: (0, "")})   # unreachable → inconclusive status
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email in ("a@x.com", "b@x.com")


def test_handle_limit_resumes_when_usage_confirms_healthy(ctx):
    """Corroboration guard: a stdout match on a seat the endpoint says is healthy (both windows
    well under the cap) is a false positive — resume the same seat, never rest it."""
    state = _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "resume" and dec.email == "a@x.com"
    # the healthy seat must NOT be flagged limited by the false positive
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None
    assert state.active("codex") == "a@x.com"


def test_run_false_positive_never_kills_the_child(ctx):
    """Verify-before-kill: a benign-but-matching line on a healthy seat is dismissed while the
    child KEEPS RUNNING — no kill, no relaunch, no switch, no rest."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    spawn = FakeSpawn([
        ([b"... you've hit your usage limit ...\n",   # false positive: usage says a is fine
          b"carried on, all good\n"], 0),             # ...and the SAME child runs to completion
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 1                          # never killed, never relaunched
    assert ctx.load_state().active("codex") == "a@x.com"  # never switched away
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None


def test_run_disables_scanning_after_repeated_false_positives(ctx, monkeypatch):
    """Past the false-alarm bound the on-screen text is provably untrustworthy (persistent prose
    about limits) — the supervisor stops SCANNING, not the session: no more usage probes, the child
    runs on, and its own exit code comes back (never EXIT_GAVE_UP)."""
    _two_codex(ctx)  # active a
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)  # let every chunk re-probe
    probes = {"n": 0}

    def get(url, headers, timeout):
        probes["n"] += 1
        return 200, codex_ok_body(primary=20.0, secondary=70.0)

    msgs = []
    chunks = [b"... you've hit your usage limit ...\n"] * (L.MAX_FALSE_ALARMS + 4)
    spawn = FakeSpawn([(chunks, 0)])
    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)
    assert rc == 0
    assert len(spawn.calls) == 1                       # never killed, never relaunched
    assert probes["n"] == L.MAX_FALSE_ALARMS + 1       # scanning off past the bound → no more probes
    assert any("ignoring limit" in m for m in msgs)
    assert ctx.load_state().active("codex") == "a@x.com"


def test_run_false_positive_dismissed_even_when_switch_budget_exhausted(ctx):
    """A dismissed false positive is not a hop: it must not interact with the switch budget."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    spawn = FakeSpawn([(b"... you've hit your usage limit ...\n", 0)])
    # max_switches=0: no seat hop is permitted, yet the healthy session must survive the prose.
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=0, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"  # never switched away


def test_run_false_positive_after_real_switch_keeps_session_alive(ctx):
    """A genuine limit spends the switch budget; a later false alarm on the new seat is dismissed
    in place — the session keeps running rather than bailing at the cap."""
    _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    calls = {"n": 0}

    def get(url, headers, timeout):
        calls["n"] += 1
        # first probe (seat a) is genuinely limited (with a real future reset, so a stays resting
        # and choose hops to b); every later probe says healthy
        body = (codex_ok_body(primary=100.0, p_reset=reset) if calls["n"] == 1
                else codex_ok_body(primary=20.0))
        return 200, body

    spawn = FakeSpawn([
        (b"... you've hit your usage limit ...\n", 1),   # iter1: genuine limit on a → switch to b
        ([b"... you've hit your usage limit ...\n",      # iter2: false alarm on b, budget spent —
          b"resumed, all good\n"], 0),                   # dismissed; the same child finishes cleanly
    ])
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=1, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 2                          # one real hop, then no more relaunches
    assert ctx.load_state().active("codex") == "b@x.com"  # switched once, then stayed on healthy b
    assert spawn.calls[1][-2:] == ["resume", "--last"]    # the hop carried the work along


def test_seat_confirmed_healthy_tolerates_malformed_window(ctx):
    """A corrupt/partial usage blob (a null window) must not crash the healthy-seat guard."""
    state = _two_codex(ctx)  # active a
    state.set_usage("codex", "a@x.com", {"ok": True, "error": None, "limit_reached": False,
                                         "windows": {"primary": None, "secondary": {"used_pct": 20.0}}})
    state.save()
    summary = {"codex": {"a@x.com": "ok"}}  # endpoint reported success → guard inspects windows
    # null window must be skipped, not crash; the valid 20% window still confirms headroom
    assert L._seat_confirmed_healthy(state, "codex", "a@x.com", summary) is True


def test_handle_limit_corroborated_skips_refetch_and_second_guess(ctx):
    """When the verify-before-kill probe already confirmed the limit, handle_limit must neither
    refetch usage (the probe did, moments ago) nor dismiss the kill as a false alarm."""
    state = _two_codex(ctx)  # active a

    def get(url, headers, timeout):
        raise AssertionError("corroborated handle_limit must not hit the usage endpoint")

    dec = handle_limit(ctx, state, "codex", get=get, corroborated=True)
    assert dec.action == "switch" and dec.email == "b@x.com"
    assert state.get_seat("codex", "a@x.com")["limited_until"] is not None  # reactive fallback


def test_handle_limit_gives_up_when_all_limited(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="solo@x.com")
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "give_up"


# --- run orchestration ------------------------------------------------------------------------

def test_run_no_seats_raises(ctx):
    with pytest.raises(NoSeats):
        run(ctx, "codex", [], spawn=FakeSpawn([]))


def test_run_passes_credential_flows_through_to_stock_unsupervised(ctx, monkeypatch):
    """Login / auth / setup-token / logout must run as the STOCK tool, never under the supervisor —
    so the OAuth localhost-callback completes — and must work even with no seats yet (first sign-in)."""
    calls = []
    monkeypatch.setattr(L, "exec_stock", lambda c, tool, args: calls.append((tool, args)) or 0)

    def _no_spawn(*a, **k):
        raise AssertionError("credential flow was supervised via spawn — it must run stock")

    for tool, args in [("claude", ["auth", "login"]), ("claude", ["setup-token"]),
                       ("codex", ["login"]), ("claude", ["logout"])]:
        calls.clear()
        assert run(ctx, tool, args, spawn=_no_spawn) == 0   # no seats needed; spawn untouched
        assert calls == [(tool, args)]

    # a normal (non-credential) invocation is still supervised — passthrough must not swallow it
    with pytest.raises(NoSeats):
        run(ctx, "codex", ["--foo"], spawn=_no_spawn)


def test_run_clean_exit_no_switch(ctx):
    state = _two_codex(ctx)
    spawn = FakeSpawn([(b"all good, done\n", 0)])
    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}))
    assert rc == 0
    assert len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"


def test_run_claude_no_switch_skips_identity_cli(ctx, monkeypatch):
    """An already-chosen Claude seat must launch without paying for `claude auth status`."""
    ctx.cred["claude"].set_live(make_claude_blob())
    acct.add(ctx, ctx.load_state(), "claude", email="solo@x.com")

    def unexpected_identity(_ctx):
        raise AssertionError("no-switch Claude launch resolved live identity")

    monkeypatch.setattr(L.identity_mod, "claude_live_identity", unexpected_identity)
    spawn = FakeSpawn([(b"all good, done\n", 0)])

    assert run(ctx, "claude", [], spawn=spawn, get=fake_get({})) == 0
    assert len(spawn.calls) == 1


def test_run_claude_hop_resolves_identity_before_spawn(ctx, monkeypatch):
    """A real Claude seat hop still resolves the outgoing blob's identity before switching."""
    state = _two_claude(ctx)  # active c1
    state.set_limited_until(
        "claude", "c1@x.com", iso(now() + timedelta(hours=1)), source="usage"
    )
    state.save()
    events = []

    def resolve_identity(_ctx):
        events.append("identity")
        return L.identity_mod.ClaudeLiveIdentity(
            blob=ctx.cred["claude"].get_live(), email="c1@x.com"
        )

    def spawn(argv, on_output, on_tick=None):
        events.append("spawn")
        on_output(b"all good, done\n")
        return 0

    monkeypatch.setattr(L.identity_mod, "claude_live_identity", resolve_identity)

    assert run(ctx, "claude", [], spawn=spawn, get=fake_get({})) == 0
    assert events == ["identity", "spawn"]
    assert ctx.load_state().active("claude") == "c2@x.com"


def test_run_switches_and_resumes_on_limit(ctx):
    _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    msgs = []
    spawn = FakeSpawn([
        (b"... you've hit your usage limit ...\n", 1),  # first launch hits limit
        (b"resumed, working\n", 0),                     # resumed launch on seat b
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=msgs.append)
    assert rc == 0
    # first launch was the normal build_cmd, second was the resume command
    assert spawn.calls[0][-1] == "--foo"
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    assert spawn.stops == 1                                  # only the approved hop stopped a child
    assert ctx.load_state().active("codex") == "b@x.com"
    assert any("hopping to b@x.com" in m for m in msgs)


def test_run_with_auto_switch_off_keeps_a_limited_live_child_on_its_seat(ctx):
    """Turning the app toggle off must never turn a confirmed banner into a forced PTY stop.

    The limit is still recorded for a safe next launch, but the child retains its own exit code and
    the launcher neither swaps credentials nor resumes on the spare.
    """
    _two_codex(ctx)  # active a, spare b
    state = ctx.load_state()
    state.set_setting("auto_switch", False)
    state.save()
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    msgs = []
    spawn = FakeSpawn([(b"... you've hit your usage limit ...\n", 17)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 17
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"
    assert any("auto-switch is off" in m for m in msgs)


def test_disabling_auto_switch_during_hard_landing_probe_does_not_stop_child(ctx):
    _two_codex(ctx)
    probes = []

    def get(_url, headers, _timeout):
        probes.append(headers.get("ChatGPT-Account-Id"))
        state = ctx.load_state()
        state.set_setting("auto_switch", False)
        state.save()
        return 200, codex_ok_body(primary=10)

    spawn = FakeSpawn([(b"Your workspace is out of credits. Add credits to continue.\n", 17)])
    rc = run(ctx, "codex", [], spawn=spawn, get=get)
    assert probes == ["acct:b@x.com"]
    assert rc == 17 and spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"


@pytest.mark.parametrize("change", ["toggle", "manual_switch"])
def test_terminal_handoff_rechecks_settings_and_active_seat_after_child_stops(ctx, change):
    _two_codex(ctx)
    child = FakeSpawn([(b"you've hit your usage limit\n", 17)])

    def spawn(*args, **kwargs):
        status = child(*args, **kwargs)
        assert child.stops == 1
        state = ctx.load_state()
        if change == "toggle":
            state.set_setting("auto_switch", False)
            state.save()
        else:
            from acctsw.switch import switch
            switch(ctx, state, "codex", "b@x.com", sync=False)
        return status

    notices = []
    rc = run(ctx, "codex", [], spawn=spawn,
             get=fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(
                 primary=100, p_reset=iso(now() + timedelta(hours=1))))}),
             notify=notices.append)
    assert rc == 17 and len(child.calls) == 1
    expected = "turned off" if change == "toggle" else "another switch changed"
    assert any(expected in notice for notice in notices)
    assert ctx.load_state().active("codex") == ("a@x.com" if change == "toggle" else "b@x.com")


def test_run_hops_off_forbidden_seat_without_resting_it(ctx):
    """A live limit banner plus a 403 is positive proof that the subscription is gone. The
    supervisor must treat that verdict like dead credentials, stop only after preflighting the
    healthy sibling, and carry the work there without inventing a reset for the revoked seat."""
    _two_codex(ctx)  # active a, healthy b
    get = fake_get({P.CODEX_USAGE_URL: (403, "")})
    spawn = FakeSpawn([
        (b"usage limit reached\n", 1),
        (b"resumed on entitled seat\n", 0),
    ])

    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0
    assert len(spawn.calls) == 2 and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None


def test_run_forbidden_only_seat_keeps_child_running_without_rest(ctx):
    """A revoked subscription with no entitled landing seat is still positive evidence to leave,
    but never permission to stop with nowhere to go. The one child owns its exit and no phantom
    cooldown is persisted."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    get = fake_get({P.CODEX_USAGE_URL: (403, "")})
    spawn = FakeSpawn([(b"usage limit reached\n", 17)])

    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 17 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0
    seat = ctx.load_state().get_seat("codex", "solo@x.com")
    assert seat.get("limited_until") is None


def test_run_single_seat_confirmed_limit_does_not_stop_child(ctx):
    """Positive confirmation is necessary but not sufficient: without another available seat the
    callback leaves the only child alive and surfaces that child's own exit status."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0,
                                                            p_reset=reset))})
    msgs = []
    spawn = FakeSpawn([(b"usage limit reached\n", 12)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 12 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert ctx.load_state().active("codex") == "solo@x.com"
    assert any("staying on this seat" in m for m in msgs)


def test_confirmed_limit_starts_one_advisory_seat_watcher(ctx, monkeypatch):
    """When no hop is possible, one daemon watcher polls live capacity and notifies once. It does
    not stop the child or switch seats; the child remains in charge of its own exit."""
    state = _two_codex(ctx)  # active a
    state.set_limited_until("codex", "b@x.com", iso(now() + timedelta(hours=2)),
                            source="usage")
    state.save()
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0,
                                                            p_reset=reset))})
    watcher_calls = []
    ready = threading.Event()
    msgs = []

    def verify(*args, **kwargs):
        watcher_calls.append((args, kwargs))
        return L.Selection("b@x.com", True, None, False)

    def notify(msg):
        msgs.append(msg)
        if "available for codex now" in msg:
            ready.set()

    monkeypatch.setattr(L, "_verify_capacity", verify)
    sleeps = []
    calls = []

    def spawn(argv, on_output, on_tick=None):
        calls.append(list(argv))
        assert on_output(b"usage limit reached\n") is False
        assert ready.wait(2), "seat watcher did not notify"
        return 13

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=notify, sleep=sleeps.append)

    assert rc == 13 and len(calls) == 1
    assert sleeps == [L.POLL_INTERVAL_S]
    assert len(watcher_calls) == 1
    assert watcher_calls[0][1]["force"] is False
    assert sum("available for codex now" in m for m in msgs) == 1
    assert ctx.load_state().active("codex") == "a@x.com"


def test_run_marks_each_launch_and_hop_then_clears_session(ctx, monkeypatch):
    _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0,
                                                            p_reset=reset))})
    marks = []
    clears = []
    monkeypatch.setattr(L, "mark_session",
                        lambda data_dir, tool, email: marks.append((data_dir, tool, email)))
    monkeypatch.setattr(L, "clear_session",
                        lambda data_dir, tool: clears.append((data_dir, tool)))
    spawn = FakeSpawn([
        (b"usage limit reached\n", 1),
        (b"resumed\n", 0),
    ])

    assert run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None) == 0

    assert [email for _, _, email in marks] == ["a@x.com", "b@x.com", "b@x.com"]
    assert clears == [(ctx.data_dir, "codex")]


def test_run_switches_on_codex_workspace_out_of_credits_without_usage_confirmation(ctx):
    """The hard Codex billing banner is already positive evidence. Do not require the usage API to
    corroborate it, because workspace credits can fail outside the normal usage-window response."""
    _two_realistic_codex_aliases(ctx)

    def no_usage_fetch(*_a, **_k):
        raise AssertionError("hard workspace-credit banner must not require usage API corroboration")

    msgs = []
    spawn = FakeSpawn([
        (b"\xe2\x96\xa0 Your workspace is out of credits. Add credits to continue.\n", 1),
        (b"resumed on the fresh alias\n", 0),
    ])
    rc = run(ctx, "codex", [], spawn=spawn, get=no_usage_fetch, notify=msgs.append)
    assert rc == 0
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "primary+codex@example.test"
    seat = state.get_seat("codex", "primary@example.test")
    # ``hard`` source: healthy-looking usage windows must never clear a billing-banner rest early
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"
    assert any("primary+codex@example.test" in m for m in msgs)


def test_run_hard_billing_banner_without_free_seat_does_not_stop_child(ctx):
    """A hard billing banner supplies confirmation, not a landing seat. It still stamps a durable
    hard rest, but the one live child is untouched when no alternative exists."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="solo@x.com")
    spawn = FakeSpawn([
        (b"\xe2\x96\xa0 Your workspace is out of credits. Add credits to continue.\n", 11),
    ])

    def no_usage_fetch(*_a, **_k):
        raise AssertionError("hard banner must not probe usage")

    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=no_usage_fetch, notify=lambda m: None)

    assert rc == 11 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0
    seat = ctx.load_state().get_seat("codex", "solo@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"


def test_hard_banner_overrides_existing_soft_flag(ctx):
    """A billing banner must stamp source=\"hard\" even when the seat ALREADY carries a softer flag
    (e.g. a menubar poll's short \"usage\" stamp) — otherwise the clearable flag survives, a later
    healthy-looking fetch frees the creditless seat, and the launcher re-picks it."""
    state = _two_codex(ctx)  # active a
    soft_until = iso(now() + timedelta(minutes=10))
    state.set_limited_until("codex", "a@x.com", soft_until, source="usage")
    dec = handle_limit(ctx, state, "codex", corroborated=True, hard=True)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limit_source"] == "hard"
    from acctsw.util import parse_iso
    assert parse_iso(seat["limited_until"]) > parse_iso(soft_until)   # later unlock wins
    assert dec.action == "switch" and dec.email == "b@x.com"


def test_hard_codex_workspace_credit_banner_bypasses_disabled_generic_scan(ctx, monkeypatch):
    """Repeated prose can disable generic limit scanning, but the exact Codex billing banner must
    still be caught so a fresh seat can take over."""
    _two_codex(ctx)  # active a
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    chunks = [b"... you've hit your usage limit ...\n"] * (L.MAX_FALSE_ALARMS + 1)
    chunks.append(b"Your workspace is out of credits. Add credits to continue.\n")
    spawn = FakeSpawn([(chunks, 1), (b"resumed\n", 0)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 2
    assert ctx.load_state().active("codex") == "b@x.com"


@pytest.mark.parametrize("transport_status", [0, 429])
def test_run_unverifiable_limit_never_gives_up(ctx, monkeypatch, transport_status):
    """End-to-end guard against the false 'all seats resting' kill: every usage probe fails to
    connect or is throttled, so a limit line can never be corroborated. The supervisor must leave
    that SAME child running with the user's original argv and return its own exit code — never
    EXIT_GAVE_UP, never a same-seat resume, never a 5h rest."""
    _two_codex(ctx)  # active a
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    get = fake_get({P.CODEX_USAGE_URL: (transport_status, "")})
    msgs = []
    spawn = FakeSpawn([(b"... you've hit your usage limit ...\n", 7)])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=msgs.append)
    assert rc == 7 and rc != L.EXIT_GAVE_UP                   # child's own exit, not supervisor give-up
    assert spawn.calls == [build_cmd(ctx, "codex", ["--foo"])]  # false alarm never flips `resuming`
    assert spawn.stops == 0                                  # callback left the live child alone
    assert ctx.load_state().active("codex") == "a@x.com"     # never switched away
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None  # never rested
    assert sum("couldn't verify" in m for m in msgs) == 1


def test_run_unknown_probes_never_disable_later_limit_detection(ctx, monkeypatch):
    """Endpoint noise is not proof that stdout is untrustworthy. More than the false-alarm bound
    worth of inconclusive probes must leave scanning alive so a later confirmed limit still hops."""
    _two_codex(ctx)  # active a, healthy b
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    unknowns = L.MAX_FALSE_ALARMS + 2
    probes = {"n": 0}
    reset = iso(now() + timedelta(hours=3))

    def get(url, headers, timeout):
        probes["n"] += 1
        if probes["n"] <= unknowns:
            return 0, ""
        return 200, codex_ok_body(primary=100.0, p_reset=reset)

    chunks = [b"... you've hit your usage limit ...\n"] * (unknowns + 1)
    spawn = FakeSpawn([
        (chunks, 1),
        (b"resumed after the endpoint recovered\n", 0),
    ])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0
    assert probes["n"] == unknowns + 1
    assert len(spawn.calls) == 2 and spawn.stops == 1
    assert ctx.load_state().active("codex") == "b@x.com"


@pytest.mark.parametrize("text", [
    "your refresh token was revoked. Please log out and sign in again.",
    "error: refresh token revoked",
])
def test_detect_auth_dead_positive(text):
    assert detect_auth_dead("codex", text)


def test_detect_auth_dead_negative_and_not_a_limit():
    assert not detect_auth_dead("codex", "you've hit your usage limit")
    assert not detect_limit("codex", "refresh token was revoked")  # auth death isn't a limit
    # tightened patterns must NOT fire on the model merely writing about auth
    assert not detect_auth_dead("codex", "to fix this, please sign in again via the portal")


def test_handle_limit_honors_auth_failed_exclude(ctx):
    # after an auth hop, a later (real) limit must NOT re-select the seat that already died this run
    reset = iso(now() + timedelta(hours=3))
    maxed = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})  # ok+maxed
    state = _two_codex(ctx)  # active a, b available
    dec = handle_limit(ctx, state, "codex", get=maxed, exclude={"b@x.com"})
    assert dec.action == "give_up"          # b excluded, a genuinely limited → nothing to switch to
    # sanity: without the exclude it WOULD switch to b
    state2 = _two_codex(ctx)
    assert handle_limit(ctx, state2, "codex", get=maxed).action == "switch"


def test_detect_event_classifies_in_one_pass():
    from acctsw.launcher import detect_event
    assert detect_event("codex", "your refresh token was revoked") == "auth"
    assert detect_event("codex", "you've hit your usage limit") == "limit"
    assert detect_event("codex", "all good here") is None
    # auth wins over a co-occurring limit phrase (different remedy: hop, don't resume same seat)
    assert detect_event("claude", "usage limit; oauth token expired") == "auth"


def test_handle_auth_dead_switches_excluding_active(ctx):
    state = _two_codex(ctx)  # active a
    dec = handle_auth_dead(ctx, state, "codex")
    assert dec.action == "switch" and dec.email == "b@x.com"
    # no persisted "dead" flag — a usage-poll unauthorized isn't a reliable health signal
    assert (state.get_seat("codex", "a@x.com").get("usage") or {}).get("error") is None


def test_run_switches_to_healthy_seat_on_revoked_token(ctx):
    _two_codex(ctx)  # active a
    msgs = []
    spawn = FakeSpawn([
        (b"... your refresh token was revoked. Please log out and sign in again.\n", 1),
        (b"resumed on the healthy seat\n", 0),
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert rc == 0
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    assert ctx.load_state().active("codex") == "b@x.com"
    assert any("sign in again" in m for m in msgs)


def test_run_auth_prose_dismissed_when_token_provably_works(ctx):
    """Auth-death prose while a usage fetch succeeds with the SAME creds is a false positive —
    the token just authenticated a request, so don't hop; the child keeps running."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    spawn = FakeSpawn([(b"... your refresh token was revoked ...\n", 0)])
    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 1                          # never killed, never relaunched
    assert ctx.load_state().active("codex") == "a@x.com"  # no hop


REFRESH_REVOKED_BANNER = (
    "■ Your access token could not be refreshed because your refresh token was\n"
    "revoked. Please log out and sign in again.\n"
)


@pytest.mark.parametrize("banner", [
    REFRESH_REVOKED_BANNER,
    REFRESH_REVOKED_BANNER.replace("could not", "could\nnot"),
    "\x1b[31m" + REFRESH_REVOKED_BANNER + "\x1b[0m",
])
def test_detect_exact_refresh_revocation_banner(banner):
    assert L.detect_refresh_revoked("codex", banner)
    assert not L.detect_refresh_revoked("claude", banner)
    assert not L.detect_refresh_revoked("codex", "Codex printed: " + banner)


def test_refresh_revocation_survives_successful_usage_and_marks_both_seats(ctx):
    """The field failure: a usage 200 says nothing about the refresh token's validity."""
    from acctsw import bridge
    _two_codex(ctx)
    messages = []
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    spawn = FakeSpawn([
        (REFRESH_REVOKED_BANNER.encode(), -15),
        (REFRESH_REVOKED_BANNER.encode(), 0),
    ])
    assert run(ctx, "codex", [], spawn=spawn, get=get, notify=messages.append) == 0
    assert len(spawn.calls) == 2 and spawn.stops == 1  # never bounce back onto known-dead a
    state = ctx.load_state()
    for email in ("a@x.com", "b@x.com"):
        assert state.get_seat("codex", email)["auth_error"] == "refresh_token_revoked"
        assert state.get_seat("codex", email)["limited_until"] is None
    # A successful background usage reading must not hide the required re-login in the app.
    L.usage_mod.refresh(ctx, state, "codex", force=True, get=get)
    view = bridge.snapshot_state(ctx)["tools"]["codex"]["seats"]
    assert all(seat["needs_login"] for seat in view)
    assert any("no other codex seat is ready" in message for message in messages)
    # A new supervisor must also refuse these credentials until the user signs in again.
    unused_spawn = FakeSpawn([])
    assert run(ctx, "codex", [], spawn=unused_spawn, notify=messages.append) == L.EXIT_GAVE_UP
    assert unused_spawn.calls == []


def test_refresh_revocation_bypasses_prose_scan_cooldown(ctx):
    _two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    spawn = FakeSpawn([
        ([b"The documentation mentions refresh token revoked.\n",
          REFRESH_REVOKED_BANNER.encode()], -15),
        (b"resumed\n", 0),
    ])
    assert run(ctx, "codex", [], spawn=spawn, get=get) == 0
    assert spawn.stops == 1
    assert ctx.load_state().active("codex") == "b@x.com"


def test_refresh_revocation_marks_launch_seat_after_pointer_moves(ctx):
    from acctsw.switch import switch
    _two_codex(ctx)

    def move_pointer():
        switch(ctx, ctx.load_state(), "codex", "b@x.com")

    spawn = FakeSpawn([
        ([b"booting\n", REFRESH_REVOKED_BANNER.encode()], -15, [move_pointer]),
        (b"resumed\n", 0),
    ])
    assert run(ctx, "codex", [], spawn=spawn) == 0
    state = ctx.load_state()
    assert state.get_seat("codex", "a@x.com")["auth_error"] == "refresh_token_revoked"
    assert not state.get_seat("codex", "b@x.com").get("auth_error")
    assert state.active("codex") == "b@x.com"


def test_run_leaves_child_alive_with_relogin_hint_when_only_seat_revoked(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    msgs = []
    spawn = FakeSpawn([(b"refresh token was revoked\n", 9)])
    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert rc == 9 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert any("sign in again" in m for m in msgs)


def test_run_auth_and_revoked_stay_notifications_are_independent(ctx, monkeypatch):
    """Auth death and lost entitlement are different remedies, so one must not suppress the other."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    replies = iter([(0, ""), (403, "")])

    def get(url, headers, timeout):
        return next(replies)

    msgs = []
    spawn = FakeSpawn([([
        b"your refresh token was revoked\n",
        b"usage limit reached\n",
    ], 9)])

    assert run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append) == 9
    assert any("sign in again" in msg for msg in msgs)
    assert any("no longer entitled" in msg for msg in msgs)


def test_run_budget_spent_does_not_stop_confirmed_limit(ctx, monkeypatch):
    _two_codex(ctx)
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})  # genuinely maxed
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    # A different seat is ready, but switches==max_switches at launch. The budget is part of the
    # pre-flight, so the child keeps its terminal and its own exit code is surfaced.
    spawn = FakeSpawn([(b"usage limit reached\n", 8)])
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=0, notify=lambda m: None)
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert rc == 8 and rc != L.EXIT_GAVE_UP
    assert ctx.load_state().active("codex") == "a@x.com"


def test_run_propagates_nonzero_clean_exit(ctx):
    _two_codex(ctx)
    # no limit text AND usage says the active seat is healthy → a plain failure, surfaced untouched
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    spawn = FakeSpawn([(b"build failed\n", 3)])
    assert run(ctx, "codex", [], spawn=spawn, get=get) == 3


def test_handle_exhausted_switches_when_active_confirmed_out(ctx):
    """A fresh usage fetch confirms the active seat is genuinely maxed → hop to a healthy seat."""
    state = _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    dec = L.handle_exhausted(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email == "b@x.com"


def test_handle_exhausted_gives_up_when_active_healthy(ctx):
    """No positive evidence the active seat is out → give_up, so the caller surfaces the exit code."""
    state = _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    dec = L.handle_exhausted(ctx, state, "codex", get=get)
    assert dec.action == "give_up"


def test_handle_exhausted_gives_up_when_endpoint_unreachable(ctx):
    """A transient error is not positive evidence — never manufacture a hop on an unconfirmed limit."""
    state = _two_codex(ctx)  # active a
    dec = L.handle_exhausted(ctx, state, "codex", get=fake_get({P.CODEX_USAGE_URL: (0, "")}))
    assert dec.action == "give_up"


def test_handle_exhausted_hops_off_forbidden_without_resting(ctx):
    state = _two_codex(ctx)  # active a, healthy b
    dec = L.handle_exhausted(
        ctx, state, "codex", get=fake_get({P.CODEX_USAGE_URL: (403, "")})
    )
    assert dec.action == "switch" and dec.email == "b@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None


def test_handle_exhausted_forbidden_only_seat_gives_up_without_resting(ctx):
    state = _two_codex(ctx)
    state.remove_seat("codex", "b@x.com")
    state.save()
    dec = L.handle_exhausted(
        ctx, state, "codex", get=fake_get({P.CODEX_USAGE_URL: (403, "")})
    )
    assert dec.action == "give_up"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None


def test_run_switches_on_silent_limit_exit(ctx):
    """Exit-time safety net: codex hits a real limit but exits with only a plain error (no banner we
    can match). A fresh usage check confirms the active seat is out → hop to a healthy seat + resume."""
    _two_codex(ctx)  # active a
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})  # a maxed
    spawn = FakeSpawn([
        (b"stream error: disconnected\n", 1),   # non-zero exit, NO matchable limit banner
        (b"resumed on b, all good\n", 0),        # resumed on the healthy seat
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert len(spawn.calls) == 2
    assert spawn.calls[1][-2:] == ["resume", "--last"]          # carried the work over
    assert ctx.load_state().active("codex") == "b@x.com"        # hopped to the healthy seat


def test_run_no_switch_on_plain_nonzero_exit(ctx):
    """A non-zero exit that is NOT a limit (usage says the seat is healthy) is surfaced untouched —
    the safety net must not turn an ordinary failure into a spurious seat hop."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    spawn = FakeSpawn([(b"build failed: syntax error\n", 2)])
    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 2
    assert len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"


@pytest.mark.parametrize("code", [130, 143, -2])   # SIGINT (Ctrl-C), SIGTERM, signal death (negative)
def test_run_abort_exit_skips_safety_net(ctx, code):
    """A user abort (Ctrl-C/kill) must NOT trigger a usage fetch or a hop — the exit-time safety net
    is only for genuine (positive, non-abort) failure codes, so aborting never delays teardown on a
    network round-trip."""
    _two_codex(ctx)  # active a
    def boom_get(*a, **k):
        raise AssertionError("safety net fetched usage on an abort exit")
    spawn = FakeSpawn([(b"^C\n", code)])
    rc = run(ctx, "codex", [], spawn=spawn, get=boom_get, notify=lambda m: None)
    assert rc == code
    assert len(spawn.calls) == 1                          # no resume
    assert ctx.load_state().active("codex") == "a@x.com"  # no hop


def test_handle_exhausted_confirms_out_on_limit_reached_without_reset(ctx):
    """A seat out by the authoritative limit_reached flag but carrying NO reset timestamp still counts
    as out: usage itself now stamps a fallback rest (so choose() won't re-pick it) and we hop."""
    state = _two_codex(ctx)  # active a
    body = json.dumps({"rate_limit": {"limit_reached": True,
                       "primary_window": {"used_percent": 50},
                       "secondary_window": {"used_percent": 50}}})   # maxed flag, no window≥100, no reset
    get = fake_get({P.CODEX_USAGE_URL: (200, body)})
    dec = L.handle_exhausted(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email == "b@x.com"
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "usage"


def test_run_codex_home_preserved_on_exception(ctx):
    """Codex isolation: a crash must not corrupt/lose the active account's per-account home
    (the source of truth codex maintains); the finally mirrors home → ~/.codex."""
    state = _two_codex(ctx)  # active a, home(a) populated
    before = ctx.snapshot_get("codex", "a@x.com")

    def boom(argv, on_output, on_tick=None):
        raise RuntimeError("pty exploded")

    with pytest.raises(RuntimeError):
        run(ctx, "codex", [], spawn=boom, get=fake_get({}))
    assert ctx.snapshot_get("codex", "a@x.com") == before        # home intact
    assert ctx.cred["codex"].get_live() == before                # mirrored home → live


def test_activate_codex_home_promotes_only_without_a_live_session(ctx, monkeypatch):
    """Promotion MOVES shared files, so it is asked for only when no supervised codex session could
    have them open — otherwise the home is merely relinked and the heal waits for the next launch."""
    from acctsw import codexhome

    seen = []
    real_ensure = codexhome.ensure_home

    def spy(email, **kw):
        if "promote" in kw:   # ignore the credential-snapshot calls, which never promote
            seen.append(kw["promote"])
        return real_ensure(email, **kw)

    _two_codex(ctx)
    monkeypatch.setattr(codexhome, "ensure_home", spy)
    monkeypatch.setattr(L, "active_session", lambda data_dir, tool: {"email": "b@x.com", "pid": 1,
                                                                     "started_at": "x"})
    run(ctx, "codex", [], spawn=FakeSpawn([(b"", 0)]), get=fake_get({}))
    assert seen and seen[0] is False

    seen.clear()
    monkeypatch.setattr(L, "active_session", lambda data_dir, tool: None)
    run(ctx, "codex", [], spawn=FakeSpawn([(b"", 0)]), get=fake_get({}))
    assert seen and seen[0] is True


def test_run_claude_resume_uses_continue(ctx):
    for em in ("c1@x.com", "c2@x.com"):
        ctx.cred["claude"].set_live(__import__("tests.conftest", fromlist=["make_claude_blob"]).make_claude_blob())
        st = ctx.load_state()
        acct.add(ctx, st, "claude", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "claude", "c1@x.com")
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=100.0,
                                                             five_reset=reset))})
    spawn = FakeSpawn([(b"usage limit reached\n", 1), (b"resumed\n", 0)])
    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert spawn.calls[1][-1] == "--continue"


def test_run_claude_waits_then_starts_original_invocation_when_all_seats_resting(ctx):
    state = _two_claude(ctx)
    first = iso(now() + timedelta(seconds=30))
    second = iso(now() + timedelta(seconds=60))
    state.set_limited_until("claude", "c1@x.com", first, source="usage")
    state.set_limited_until("claude", "c2@x.com", second, source="usage")
    state.save()
    sleeps = []
    msgs = []
    spawn = FakeSpawn([(b"resumed\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=fake_get({}),
             notify=msgs.append, sleep=sleeps.append)

    assert rc == 0
    assert len(sleeps) == 1
    assert sleeps[0] > 0
    assert spawn.calls[0] == build_cmd(ctx, "claude", [])
    assert any("waiting until" in m for m in msgs)
    assert ctx.load_state().get_seat("claude", "c1@x.com").get("limited_until") is None


def test_run_confirmed_limit_with_all_other_seats_resting_keeps_child(ctx):
    state = _two_claude(ctx)  # active c1
    c1_reset = iso(now() + timedelta(seconds=60))
    c2_reset = iso(now() + timedelta(seconds=30))
    state.set_limited_until("claude", "c2@x.com", c2_reset, source="usage")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL:
                    (200, claude_ok_body(five=100.0, five_reset=c1_reset))})
    spawn = FakeSpawn([(b"usage limit reached\n", 6)])

    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 6 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert ctx.load_state().active("claude") == "c1@x.com"


def test_run_claude_all_resting_opt_out_exits_without_spawn(ctx, monkeypatch):
    state = _two_claude(ctx)
    state.set_limited_until("claude", "c1@x.com", iso(now() + timedelta(seconds=30)),
                            source="usage")
    state.set_limited_until("claude", "c2@x.com", iso(now() + timedelta(seconds=60)),
                            source="usage")
    state.save()
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    msgs = []
    spawn = FakeSpawn([])

    rc = run(ctx, "claude", [], spawn=spawn, get=fake_get({}), notify=msgs.append)

    assert rc == L.EXIT_GAVE_UP
    assert spawn.calls == []
    assert any("soonest unlocks" in m for m in msgs)


def test_run_verifies_before_blocking_and_skips_wait_when_capacity_exists(ctx):
    """The headline bug: stale reactive flags said 'all seats resting' while the seats actually had
    capacity. The wait must FORCE-verify against the endpoint first and start immediately —
    no sleep, no 'waiting until' — clearing the disproven flags."""
    state = _two_claude(ctx)
    for em in ("c1@x.com", "c2@x.com"):
        state.set_limited_until("claude", em, iso(now() + timedelta(hours=4)), source="reactive")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=5.0, week=10.0))})
    sleeps = []
    spawn = FakeSpawn([(b"resumed straight away\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None, sleep=sleeps.append)

    assert rc == 0
    assert sleeps == []                                   # verified healthy → never slept
    assert len(spawn.calls) == 1
    state = ctx.load_state()
    assert state.get_seat("claude", "c1@x.com")["limited_until"] is None
    assert state.get_seat("claude", "c2@x.com")["limited_until"] is None


def test_run_wait_polls_and_wakes_early_respecting_backoff(ctx):
    """A long wait is not one blind sleep: the loop wakes every POLL_INTERVAL_S, re-verifies
    (gated by the endpoint's error backoff), and resumes EARLY the moment a seat frees — well
    before the advertised unlock time."""
    state = _two_claude(ctx)
    until = iso(now() + timedelta(minutes=30))
    for em in ("c1@x.com", "c2@x.com"):
        state.set_limited_until("claude", em, until, source="usage")
    state.save()
    calls = {"n": 0}
    def get(url, headers, timeout):
        calls["n"] += 1
        # Entry sweep: endpoint down (429 → error_streak=1 → backoff 2*POLL_MIN_FETCH_S=480s).
        # Once it recovers, the seats are healthy — the wait must notice and resume early.
        return (429, "") if calls["n"] <= 2 else (200, claude_ok_body(five=5.0, week=10.0))
    sleeps = []
    spawn = FakeSpawn([(b"resumed early\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None, sleep=sleeps.append)

    assert rc == 0
    # One chunked sleep, never the full 30 min. The ACTIVE seat's error backoff is capped at
    # usage.ACTIVE_MAX_BACKOFF_SECONDS (300s) rather than the raw 480s the streak would give — the
    # seat we're waiting to run ON must re-validate promptly — so the +300s poll fetches it, sees
    # health and wakes 25 min early. Its resting sibling stays behind the full 480s (not fetched
    # here), which is what keeps a flapping endpoint from being hammered.
    assert sleeps == [L.POLL_INTERVAL_S]
    assert calls["n"] == 3                                # 2 entry + 1 active-seat re-check
    assert ctx.load_state().active("claude") == "c1@x.com"
    assert ctx.load_state().get_seat("claude", "c1@x.com")["limited_until"] is None


def test_run_wait_clears_expired_flags_for_all_seats(ctx):
    """After the unlock time passes, EVERY expired rest marker is dropped — the old wait cleared
    only the one chosen seat, leaving stale siblings to mis-drive the next selection."""
    state = _two_claude(ctx)
    until = iso(now() + timedelta(seconds=30))
    for em in ("c1@x.com", "c2@x.com"):
        state.set_limited_until("claude", em, until, source="usage")
    state.save()
    sleeps = []
    spawn = FakeSpawn([(b"resumed\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=fake_get({}),  # endpoint 404s: clock decides
             notify=lambda m: None, sleep=sleeps.append)

    assert rc == 0
    assert len(sleeps) == 1
    state = ctx.load_state()
    assert state.get_seat("claude", "c1@x.com")["limited_until"] is None
    assert state.get_seat("claude", "c2@x.com")["limited_until"] is None


def test_wait_for_unlock_forced_verify_clears_stale_reactive_flag_when_waiting_disabled(ctx, monkeypatch):
    """Fix A / Issue 3: a single seat carrying a STALE FUTURE reactive flag (a leftover false
    positive) must not instant-give-up when waiting is DISABLED. The forced cold-start verify sweep
    still runs; a healthy (5% used) fetch clears the flag, so the wait returns the seat — not None."""
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="solo@x.com")
    state.set_limited_until("claude", "solo@x.com", iso(now() + timedelta(hours=4)), source="reactive")
    state.save()
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=5.0, week=10.0))})
    sleeps = []
    email = L._wait_for_unlock(ctx, "claude", lambda m: None, sleeps.append, get)
    assert email == "solo@x.com"
    assert sleeps == []  # waiting disabled → verified, never polled
    assert ctx.load_state().get_seat("claude", "solo@x.com").get("limited_until") is None


def test_wait_for_unlock_forced_verify_clears_stale_reactive_flag_when_waiting_enabled(ctx):
    """Same self-heal with waiting ENABLED: the forced sweep clears the stale reactive flag and the
    seat is returned immediately, without any sleep."""
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="solo@x.com")
    state.set_limited_until("claude", "solo@x.com", iso(now() + timedelta(hours=4)), source="reactive")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=5.0, week=10.0))})
    sleeps = []
    email = L._wait_for_unlock(ctx, "claude", lambda m: None, sleeps.append, get)
    assert email == "solo@x.com"
    assert sleeps == []
    assert ctx.load_state().get_seat("claude", "solo@x.com").get("limited_until") is None


def test_wait_for_unlock_returns_seat_even_without_advertised_unlock(ctx):
    """Direct contract check for the give-up-without-timestamp path: the loop needs no pre-known
    unlock time — it verifies live capacity itself and returns a usable seat instead of None
    (which would have closed the whole CLI session)."""
    _two_claude(ctx)
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=5.0, week=10.0))})
    email = L._wait_for_unlock(ctx, "claude", lambda m: None, lambda s: None, get)
    assert email in ("c1@x.com", "c2@x.com")


def test_run_pre_launch_sweep_clears_near_max_reactive_flag(ctx):
    """Fix B end-to-end: a single seat carrying a near-max (95%) reactive flag reads all_limited at
    startup, but the pre-launch forced sweep (trust_reactive_lag=False) sees the seat is below the
    real limit, clears the stale guess, and the session starts immediately — no instant give-up."""
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="solo@x.com")
    state.set_limited_until("claude", "solo@x.com", iso(now() + timedelta(hours=4)), source="reactive")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=95.0, week=20.0))})
    sleeps = []
    spawn = FakeSpawn([(b"resumed, all good\n", 0)])
    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None, sleep=sleeps.append)
    assert rc == 0
    assert len(spawn.calls) == 1
    assert sleeps == []
    assert ctx.load_state().get_seat("claude", "solo@x.com").get("limited_until") is None


def test_wait_mid_session_keeps_near_max_reactive_flag(ctx, monkeypatch):
    """Regression (review F1): the near-max reactive relaxation is COLD-START ONLY. A mid-session wait
    (cold_start=False, the default) must NOT clear a just-stamped near-max reactive rest on an endpoint
    lagging below 100% — otherwise the maxed seat is resumed at once in a same-seat busy-loop. Cold
    start (cold_start=True) DOES clear the stale guess. Waiting is disabled so the forced sweep is the
    only step (no poll loop)."""
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    _two_claude(ctx)  # c1 active, c2
    state = ctx.load_state()
    for e in ("c1@x.com", "c2@x.com"):
        state.set_limited_until("claude", e, iso(now() + timedelta(hours=2)), source="reactive")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=96.0, week=20.0))})

    # mid-session (default cold_start=False): near-max reactive flags are KEPT → no seat freed → None
    assert L._wait_for_unlock(ctx, "claude", lambda m: None, lambda s: None, get) is None
    assert ctx.load_state().get_seat("claude", "c1@x.com")["limited_until"] is not None

    # cold start: the same lagging reading clears the stale guess and a seat is returned
    email = L._wait_for_unlock(ctx, "claude", lambda m: None, lambda s: None, get, cold_start=True)
    assert email in ("c1@x.com", "c2@x.com")


def test_run_wait_hard_rested_seat_not_repicked(ctx):
    """A ``hard``-rested seat (billing banner) must survive the wait's healthy verify sweep — the
    OTHER seat (whose usage-source flag the sweep disproves) takes the floor instead."""
    state = _two_claude(ctx)  # active c1
    state.set_limited_until("claude", "c1@x.com", iso(now() + timedelta(hours=2)), source="hard")
    state.set_limited_until("claude", "c2@x.com", iso(now() + timedelta(minutes=30)), source="usage")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=5.0, week=10.0))})
    sleeps = []
    spawn = FakeSpawn([(b"resumed on c2\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None, sleep=sleeps.append)

    assert rc == 0
    assert sleeps == []
    state = ctx.load_state()
    assert state.active("claude") == "c2@x.com"
    seat = state.get_seat("claude", "c1@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"


# --- real PTY smoke tests (would have caught the B1 reaping bug) -------------------------------

def test_pty_spawn_clean_exit_captures_output():
    seen = bytearray()
    def cb(chunk):
        seen.extend(chunk)
        return False
    rc = L.pty_spawn(["/bin/echo", "hello-pty"], cb)
    assert rc == 0
    assert b"hello-pty" in bytes(seen)


def test_pty_spawn_stop_path_terminates_without_error():
    """on_output returns True → child killed AND reaped exactly once (no ChildProcessError)."""
    def cb(chunk):
        return b"usage limit" in chunk
    rc = L.pty_spawn(["/bin/sh", "-c", "echo usage limit; sleep 5"], cb)
    # returns promptly with a signal-derived status; the key assertion is "does not raise"
    assert rc != 0


def test_terminate_gives_sigterm_five_seconds_before_sigkill(monkeypatch):
    """The legitimate hop path gives a TUI 100×50ms to flush its resume file before SIGKILL."""
    signals = []
    sleeps = []
    clock = [0.0]
    monkeypatch.setattr(L.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(L.os, "killpg", lambda pgid, sig: signals.append(sig))

    def waitpid(pid, options):
        assert options == L.os.WNOHANG
        return (pid, 0) if L.signal.SIGKILL in signals else (0, 0)

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(L.os, "waitpid", waitpid)
    monkeypatch.setattr(L.time, "sleep", sleep)
    monkeypatch.setattr(L.time, "monotonic", lambda: clock[0])

    assert L._terminate(4242) == 0
    assert signals == [L.signal.SIGTERM, L.signal.SIGKILL]
    assert sum(sleeps) == pytest.approx(5.0)


def test_pty_spawn_nonzero_exit_propagates():
    rc = L.pty_spawn(["/bin/sh", "-c", "exit 7"], lambda c: False)
    assert rc == 7


def test_manual_switch_restarts_real_pty_child_with_new_credentials(ctx, tmp_path, monkeypatch):
    """Exercise actual process shutdown, saved conversation, and per-account homes together."""
    import sys
    from acctsw import bridge
    monkeypatch.chdir(tmp_path)
    state = _two_codex(ctx)
    state.set_setting("auto_switch", False)
    state.save()
    _codex_sessions(ctx)
    program = tmp_path / "test-codex"
    program.write_text(f"#!{sys.executable}\n" + r'''
import json, os, signal, sys
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
account = json.loads((home / "auth.json").read_text())["tokens"]["account_id"]
events = Path(__file__).with_suffix(".events")
saved = Path(__file__).with_suffix(".saved")
def record(event):
    with events.open("a") as f:
        f.write(json.dumps({"event": event, "account": account, "pid": os.getpid()}) + "\n")
if sys.argv[1:]:
    assert sys.argv[1:] == ["resume", "pty-conversation"], sys.argv
    assert saved.read_text() == "conversation saved before restart"
    record("resumed")
    print("RESUMED", flush=True)
    sys.exit(0)
def stop(*_):
    saved.write_text("conversation saved before restart")
    record("stopped")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.alarm(10)
path = home / "sessions" / "rollout-test-pty-conversation.jsonl"
path.write_text(json.dumps({"type": "session_meta", "payload": {
    "id": "pty-conversation", "cwd": os.getcwd()}}) + "\n")
record("started")
print("READY_TO_SWITCH", flush=True)
while True:
    signal.pause()
''')
    program.chmod(0o755)
    ctx.codex_bin = str(program)
    output = bytearray()
    clicked = False

    def spawn(argv, on_output, on_tick=None):
        def observe(chunk):
            nonlocal clicked
            output.extend(chunk)
            if not clicked and b"READY_TO_SWITCH" in output:
                clicked = True
                result = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "b@x.com"})
                assert result["ok"]
            return on_output(chunk)
        return L.pty_spawn(argv, observe, on_tick=on_tick, tick_interval=0.05)

    assert run(ctx, "codex", [], spawn=spawn, max_switches=0) == 0
    events = [json.loads(line) for line in program.with_suffix(".events").read_text().splitlines()]
    assert [event["event"] for event in events] == ["started", "stopped", "resumed"]
    assert [event["account"] for event in events] == ["acct:a@x.com", "acct:a@x.com", "acct:b@x.com"]
    assert events[0]["pid"] == events[1]["pid"] != events[2]["pid"]
    assert b"RESUMED" in output


def test_reset_terminal_disables_mouse_tracking_on_tty():
    """A killed TUI can't disable its own mouse reporting; teardown must, or the shell that
    inherits the terminal spews "\\e[<..M" mouse coordinates at the prompt."""
    import pty as _pty
    master, slave = _pty.openpty()
    try:
        L._reset_terminal(slave)  # slave is a real tty → reset written
        data = os.read(master, 4096)
    finally:
        os.close(master)
        os.close(slave)
    assert b"\x1b[?1000l" in data  # X10/normal mouse tracking off
    assert b"\x1b[?1006l" in data  # SGR mouse mode off
    assert b"\x1b[?25h" in data    # cursor restored


def test_reset_terminal_noop_on_non_tty():
    r, w = os.pipe()
    try:
        L._reset_terminal(w)  # pipe is not a tty → nothing written, no raise
        os.set_blocking(r, False)
        try:
            leaked = os.read(r, 4096)
        except BlockingIOError:
            leaked = b""
        assert leaked == b""
    finally:
        os.close(r)
        os.close(w)


def test_run_claude_exit_syncs_back_rotated_creds(ctx, monkeypatch):
    """Claude rotates refresh tokens mid-session and the LIVE copy is the freshest one. If exit
    doesn't sync it into the seat's snapshot, an out-of-band login that replaces the live item next
    loses those bytes outright — the snapshot keeps a superseded token and the seat quietly stops
    working (switch.sync_back's module docstring is explicit about this)."""
    ctx.cred["claude"].set_live(make_claude_blob())
    acct.add(ctx, ctx.load_state(), "claude", email="solo@x.com")
    rotated = make_claude_blob("max").replace('"accessToken": "x"', '"accessToken": "rotated"')

    def rotate_mid_session(_argv, on_output, on_tick=None):
        ctx.cred["claude"].set_live(rotated)   # the child refreshed its token while running
        on_output(b"done\n")
        return 0

    monkeypatch.setattr(
        L.identity_mod, "claude_live_identity",
        lambda _c: L.identity_mod.ClaudeLiveIdentity(blob=ctx.cred["claude"].get_live(),
                                                     email="solo@x.com"))

    assert run(ctx, "claude", [], spawn=rotate_mid_session, get=fake_get({})) == 0
    assert ctx.snapshot_get("claude", "solo@x.com") == rotated


def test_run_claude_exit_without_rotation_skips_identity_cli(ctx, monkeypatch):
    """The sync-back is preserved, but its COST is not paid when there is nothing to preserve: an
    unchanged live blob is byte-identical to the snapshot, so no `claude auth status` is spawned."""
    ctx.cred["claude"].set_live(make_claude_blob())
    acct.add(ctx, ctx.load_state(), "claude", email="solo@x.com")

    def unexpected_identity(_ctx):
        raise AssertionError("unrotated Claude exit resolved live identity")

    monkeypatch.setattr(L.identity_mod, "claude_live_identity", unexpected_identity)

    assert run(ctx, "claude", [], spawn=FakeSpawn([(b"done\n", 0)]), get=fake_get({})) == 0


def test_run_exit_hop_off_revoked_seat_says_entitlement_not_limit(ctx):
    """The post-exit safety net can now hop for TWO reasons. Telling a user whose subscription was
    cancelled that they "hit their usage limit" promises a reset that will never arrive — the exit
    path must name the real cause, exactly as the mid-session path already does."""
    _two_codex(ctx)  # active a, healthy b
    get = fake_get({P.CODEX_USAGE_URL: (403, "")})
    msgs = []
    # No banner on stdout: the child just dies, so ONLY the exit-time safety net can react.
    spawn = FakeSpawn([(b"boom\n", 3), (b"carried on\n", 0)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0
    assert ctx.load_state().active("codex") == "b@x.com"     # it did hop
    assert any("no longer entitled" in m for m in msgs)
    assert not any("hit its usage limit" in m for m in msgs)


# --- structured signals: the supervisor's tick -------------------------------------------------
# Field failure (2026-09-11, codex 0.153.4): the banner wording changed AND the menubar poll rested
# the running seat in state.json, yet the live child never hopped. These drive the two structured
# sources that now decide — the child's own rollout JSONL and the engine state file — through the
# heartbeat, with stdout demoted to a hint.

def _codex_sessions(ctx):
    d = ctx._codex_real / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rollout_session(ctx, cwd, *, thread="01a0abcd"):
    """A rollout file holding only its session_meta header, as it looks the moment codex starts.

    Pre-created so the cwd match can succeed, but discovery is snapshot-then-diff on mtime: the
    watcher attaches only once a tick hook APPENDS to it, exactly like a real child writing its
    first event.
    """
    return write_rollout(_codex_sessions(ctx), thread=thread, cwd=cwd)


def _appender(path, *lines, bump=5.0):
    """A tick hook: the child appends ``lines`` to its rollout and the file's mtime moves."""
    def _hook():
        with open(path, "a") as f:
            for line in lines:
                f.write(line + "\n")
        stamp = time.time() + bump
        os.utime(path, (stamp, stamp))
    return _hook


def _soon(seconds=5):
    """A rollout line timestamp inside THIS session (the watcher drops replayed older lines)."""
    return iso(now() + timedelta(seconds=seconds))


def _reset_epoch(hours=3):
    return int(time.time() + hours * 3600)


REFILL_BANNER = ("■ Your workspace is out of credits. "
                 "Ask your workspace owner to refill in order to continue.")


def test_tick_hops_on_rollout_credits_depleted(ctx, monkeypatch, tmp_path):
    """The child's own log says the turn failed with usage_limit_exceeded — a fact, not prose. The
    tick reads it while the child is alive, rests the seat as "hard" with the server's own message,
    and hops. The only usage fetch in the whole run is the hard landing pre-flight."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    path = _rollout_session(ctx, cwd)
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        return 200, codex_ok_body(primary=10.0, secondary=10.0)   # the landing seat is fine

    spawn = FakeSpawn([
        (None, 1, [_appender(path, task_complete_error_line(timestamp=_soon()))]),
        (b"resumed\n", 0),
    ])

    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0
    assert spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"
    assert seat["limit_detail"] == OUT_OF_CREDITS
    assert gets == [P.CODEX_USAGE_URL]   # pre-flight only: no corroboration probe, no exit fetch


def test_tick_rollout_limit_without_landing_seat_keeps_child(ctx, monkeypatch, tmp_path):
    """Confirmed by the rollout, but the only seat IS the running one: the child lives on, the hard
    rest is still persisted, the user is told once, and the advisory watcher takes over."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    path = _rollout_session(ctx, cwd)
    monkeypatch.setattr(L, "_verify_capacity",
                        lambda *a, **k: L.Selection("solo@x.com", True, None, False))
    msgs = []
    sleeps = []
    spawn = FakeSpawn([(None, 11, [_appender(path, task_complete_error_line(timestamp=_soon()))])])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append,
             sleep=sleeps.append)

    assert rc == 11 and rc != L.EXIT_GAVE_UP
    assert spawn.stops == 0 and len(spawn.calls) == 1
    seat = ctx.load_state().get_seat("codex", "solo@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "hard"
    assert seat["limit_detail"] == OUT_OF_CREDITS
    assert sum("staying on this seat" in m for m in msgs) == 1
    assert sleeps == [L.POLL_INTERVAL_S]                      # the capacity watcher did start
    assert sum("available for codex now" in m for m in msgs) == 1


def test_tick_hops_when_another_process_rests_the_running_seat(ctx):
    """THE FIELD REGRESSION. The menubar's usage poll rested the exhausted seat and pointed
    ``active`` at the sibling — and the running supervised child never noticed, because nothing fed
    a seat rested by ANOTHER process back into a live launcher. No rollout, no banner, no network:
    the state file alone must be enough to hop and resume."""
    _two_codex(ctx)  # active a

    def menubar_poll():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=3)),
                                 source="usage")
            st.set_active("codex", "b@x.com")   # ...and it moves the pointer, as in the field
            st.save()

    def get(*_a, **_k):
        raise AssertionError("a seat rested by another process needs no usage fetch")

    spawn = FakeSpawn([(None, 1, [menubar_poll]), (b"resumed\n", 0)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0
    assert spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    assert ctx.load_state().active("codex") == "b@x.com"


def test_tick_does_not_kill_healthy_session_for_an_incidental_active_pointer_change(ctx):
    """Reconciliation without an explicit manual request must leave a healthy child alone."""
    _two_codex(ctx)  # active a

    def gui_switch():
        with ctx.locked():
            st = ctx.load_state()
            st.set_active("codex", "b@x.com")
            st.save()

    msgs = []
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=30.0))})
    spawn = FakeSpawn([(None, 5, [gui_switch, None, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 5
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert sum("still running on a@x.com" in m for m in msgs) == 1
    assert ctx.load_state().active("codex") == "b@x.com"   # the user's choice stands untouched


@pytest.mark.parametrize("auto_switch", [True, False])
def test_manual_codex_switch_resumes_live_session(ctx, monkeypatch, tmp_path, auto_switch):
    from acctsw import bridge
    monkeypatch.chdir(tmp_path)
    state = _two_codex(ctx)
    state.set_setting("auto_switch", auto_switch)
    state.save()
    path = _rollout_session(ctx, os.getcwd(), thread="manual-thread")
    rotated = make_codex_blob("a@x.com").replace('"refresh_token": "r"',
                                               '"refresh_token": "rotated"')
    homes = []
    messages = []

    def click():
        homes.append(os.environ["CODEX_HOME"])
        ctx.snapshot_set("codex", "a@x.com", rotated)
        _appender(path, token_count_line(primary=window(20), timestamp=_soon()))()
        result = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "b@x.com"})
        assert result["ok"]

    def resumed():
        homes.append(os.environ["CODEX_HOME"])

    def get(*args, **kwargs):
        raise AssertionError("an explicit switch must not require a usage endpoint")

    spawn = FakeSpawn([(None, -15, [click]), (None, 0, [resumed, None])])
    assert run(ctx, "codex", [], spawn=spawn, get=get, notify=messages.append,
               max_switches=0) == 0
    assert spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "manual-thread"]
    assert homes == [str(ctx.codex_home(email)) for email in ("a@x.com", "b@x.com")]
    assert ctx.load_state().active("codex") == "b@x.com"
    assert ctx.snapshot_get("codex", "a@x.com") == rotated
    assert ctx.load_state().get_seat("codex", "a@x.com")["limited_until"] is None
    assert any("switching to b@x.com" in message for message in messages)
    assert not any("next session" in message for message in messages)


@pytest.mark.parametrize("target", ["a@x.com", "b@x.com"])
def test_manual_switch_uses_latest_choice_during_shutdown(ctx, target):
    from acctsw.switch import switch
    _two_codex(ctx)

    def click(email):
        with ctx.locked():
            switch(ctx, ctx.load_state(), "codex", email, manual=True)

    calls = []

    def spawn(argv, on_output, on_tick=None):
        calls.append(list(argv))
        if len(calls) == 1:
            click("b@x.com")
            assert on_tick()
            click(target)  # user changes their mind while the old process exits
            return -15
        assert os.environ["CODEX_HOME"] == str(ctx.codex_home(target))
        assert not on_tick()  # consumed request must not restart the replacement again
        return 0

    assert run(ctx, "codex", [], spawn=spawn) == 0
    assert len(calls) == 2
    assert calls[1][-2:] == ["resume", "--last"]
    assert ctx.load_state().active("codex") == target


def test_manual_switch_to_current_seat_does_not_restart(ctx):
    from acctsw.switch import switch
    _two_codex(ctx)

    def click():
        with ctx.locked():
            switch(ctx, ctx.load_state(), "codex", "a@x.com", manual=True)

    spawn = FakeSpawn([(None, 0, [click, None])])
    assert run(ctx, "codex", [], spawn=spawn) == 0
    assert spawn.stops == 0


def test_codex_manual_request_does_not_interrupt_claude(ctx):
    from acctsw.switch import switch
    _two_codex(ctx)
    _two_claude(ctx)

    def click():
        with ctx.locked():
            switch(ctx, ctx.load_state(), "codex", "b@x.com", manual=True)

    messages = []
    spawn = FakeSpawn([(None, 0, [click, None])])
    assert run(ctx, "claude", [], spawn=spawn, notify=messages.append) == 0
    assert spawn.stops == 0
    assert messages == []


def test_tick_ignores_our_own_reactive_stamp(ctx):
    """"reactive" is OUR weakest guess (a stdout match we could not disprove). Reading it back as if
    another process had confirmed it would turn a false positive into a hop."""
    _two_codex(ctx)  # active a

    def our_own_guess():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=3)),
                                 source="reactive")
            st.save()

    msgs = []
    spawn = FakeSpawn([(None, 0, [our_own_guess, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)

    assert rc == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"
    assert msgs == []


def test_tick_rollout_healthy_dismisses_stdout_hint_without_network(ctx, monkeypatch, tmp_path):
    """Authority order: a token_count the child JUST wrote showing 2% used disproves any limit prose
    on screen. The dismissal is free — no usage fetch at all — and still spends the false-alarm
    budget, so persistent prose turns scanning off exactly as before."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    path = _rollout_session(ctx, cwd)

    def get(*_a, **_k):
        raise AssertionError("structured healthy evidence must dismiss prose without a fetch")

    prose = b"... you've hit your usage limit ...\n"
    chunks = [b"working\n", *([prose] * (L.MAX_FALSE_ALARMS + 1))]
    hooks = [_appender(path, token_count_line(primary=window(2.0), timestamp=_soon()))]
    msgs = []
    spawn = FakeSpawn([(chunks, 0, hooks)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert any("ignoring limit" in m for m in msgs)          # the dismissals were counted
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None


def test_tick_hard_signal_stays_when_landing_preflight_fails(ctx, monkeypatch, tmp_path):
    """Both seats can share ONE creditless workspace. Before committing a hard hop the landing seat
    is proven against the live endpoint; when it is out too, the fetch rests it, the child is left
    alone and the user is told the workspace — not the seat — is the problem. No ping-pong."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    path = _rollout_session(ctx, cwd)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_credits_depleted_body())})
    msgs = []
    spawn = FakeSpawn([(None, 9, [_appender(path, task_complete_error_line(timestamp=_soon()))])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 9 and rc != L.EXIT_GAVE_UP
    assert spawn.stops == 0 and len(spawn.calls) == 1
    state = ctx.load_state()
    assert state.active("codex") == "a@x.com"
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"
    assert state.get_seat("codex", "b@x.com")["limited_until"] is not None   # sibling rested too
    assert sum("workspace" in m and "staying on this seat" in m for m in msgs) == 1


@pytest.mark.parametrize("corroborated", [False, True])
def test_tick_ambiguous_attachment_requires_usage_corroboration(ctx, monkeypatch, tmp_path,
                                                                corroborated):
    """Two sessions in ONE directory: the rollout we attached to may not be ours, so a limit line in
    it is not permission to kill a healthy session. With the endpoint throttled the child lives; with
    the endpoint agreeing the seat is maxed, the hop proceeds."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    other = _rollout_session(ctx, cwd, thread="01aaaaaa")
    mine = _rollout_session(ctx, cwd, thread="01bbbbbb")
    get = fake_get({P.CODEX_USAGE_URL: (
        (200, codex_ok_body(primary=100.0, p_reset=iso(now() + timedelta(hours=3))))
        if corroborated else (429, ""))})

    def advance_both():
        _appender(other, token_count_line(primary=window(3.0), timestamp=_soon()), bump=3.0)()
        _appender(mine, token_count_line(primary=window(100.0, resets_at=_reset_epoch()),
                                         timestamp=_soon()), bump=6.0)()

    scripts = [(None, 1 if corroborated else 0, [advance_both])]
    if corroborated:
        scripts.append((b"resumed\n", 0))
    spawn = FakeSpawn(scripts)

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    if corroborated:
        assert rc == 0 and spawn.stops == 1
        assert spawn.calls[1][-2:] == ["resume", "--last"]
        assert ctx.load_state().active("codex") == "b@x.com"
    else:
        assert rc == 0 and spawn.stops == 0 and len(spawn.calls) == 1
        assert ctx.load_state().active("codex") == "a@x.com"
        assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None


def test_hard_banner_still_acts_when_no_rollout_attached(ctx):
    """With no rollout to consult, the banner is all we have and it still acts — now with the 0.153.4
    "ask your workspace owner to refill" wording. The one fetch it costs is the landing pre-flight,
    never a corroboration probe."""
    _two_codex(ctx)  # active a
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        return 200, codex_ok_body(primary=10.0, secondary=10.0)

    spawn = FakeSpawn([((REFILL_BANNER + "\n").encode(), 1), (b"resumed\n", 0)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0 and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"
    assert gets == [P.CODEX_USAGE_URL]


def test_hard_banner_waits_for_structured_event_when_rollout_attached(ctx, monkeypatch, tmp_path):
    """When we ARE tailing this child's log, the banner is demoted to a hint: killing a session on a
    string whose wording changes between releases is exactly what we are getting away from. Without
    a structured event the live child is untouched."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    path = _rollout_session(ctx, cwd)

    def get(*_a, **_k):
        raise AssertionError("a deferred banner must not probe usage either")

    quiet = token_count_line(primary=None, secondary=None, timestamp=_soon())
    spawn = FakeSpawn([([b"booting\n", (REFILL_BANNER + "\n").encode()], 0, [_appender(path, quiet)])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None


def test_detect_hard_limit_matches_refill_wording():
    """The exact 0.153.4 line that no longer matched — and the narration that still must not."""
    assert detect_hard_limit("codex", REFILL_BANNER)
    assert detect_hard_limit("codex", "■ Your workspace is out of credits. Add credits to continue.")
    assert not detect_hard_limit("claude", REFILL_BANNER)
    for benign in (
        "the routed Codex workspace is out of credits",
        "the routed Codex workspace is out of credits. Add credits to continue",
        "Codex printed: Your workspace is out of credits. Add credits to continue.",
    ):
        assert not detect_hard_limit("codex", benign)


def test_pty_spawn_calls_on_tick_when_idle_and_under_output():
    """The heartbeat must fire on a SILENT child (nothing to select on) and must be able to stop it —
    the structured sources decide, and a child that prints nothing still has to be supervised."""
    ticks = {"n": 0}

    rc = L.pty_spawn(["/bin/sh", "-c", "sleep 1.2; echo hi"], lambda c: False,
                     on_tick=lambda: bool(ticks.__setitem__("n", ticks["n"] + 1)) or False,
                     tick_interval=0.2)
    assert rc == 0
    assert ticks["n"] >= 1                      # ticked while the child was idle

    stops = {"n": 0}

    def stopper():
        stops["n"] += 1
        return stops["n"] >= 2

    started = time.monotonic()
    rc = L.pty_spawn(["/bin/sh", "-c", "sleep 30"], lambda c: False,
                     on_tick=stopper, tick_interval=0.2)
    assert time.monotonic() - started < 10      # the tick terminated it instead of waiting 30s
    assert rc != 0


def test_tick_costs_one_stat_when_nothing_changed(ctx, monkeypatch):
    """The heartbeat runs every 2s for a whole session, so it has to stay cheap: one stat per tick,
    and the state file is only PARSED when its mtime actually moved."""
    _two_codex(ctx)
    loads = {"n": 0}
    real_load = L.Context.load_state
    monkeypatch.setattr(L.Context, "load_state",
                        lambda self: (loads.__setitem__("n", loads["n"] + 1), real_load(self))[1])
    stats = {"n": 0}
    real_stat = os.stat

    def counting_stat(path, *a, **k):
        if not isinstance(path, int) and str(path) == str(ctx.state_file):
            stats["n"] += 1
        return real_stat(path, *a, **k)

    monkeypatch.setattr(L.os, "stat", counting_stat)
    seen = {}

    def spawn(argv, on_output, on_tick=None):
        before = loads["n"]
        stats["n"] = 0
        for _ in range(50):
            assert on_tick() is False
        seen["loads"] = loads["n"] - before
        seen["stats"] = stats["n"]
        return 0

    assert run(ctx, "codex", [], spawn=spawn, get=fake_get({})) == 0
    assert seen["loads"] <= 1    # only the first tick reads state; the rest stop at the stat
    assert seen["stats"] >= 50   # ...and every tick does pay that stat


def test_tick_blocked_decision_is_suppressed_then_retried(ctx, monkeypatch):
    """A seat rested for the next five hours with nowhere to land must not re-run the whole decision
    (and re-notify) every two seconds, even though the poll that rested it keeps rewriting state."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    calls = {"n": 0}
    real_handle = L.handle_limit
    monkeypatch.setattr(L, "handle_limit",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         real_handle(*a, **k))[1])

    def poll_rests_it():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "solo@x.com", iso(now() + timedelta(hours=3)),
                                 source="usage")
            st.save()   # every tick sees a NEW revision, as a live menubar poll would produce

    msgs = []
    spawn = FakeSpawn([(None, 3, [poll_rests_it] * 12)])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)

    assert rc == 3 and spawn.stops == 0
    assert calls["n"] == 1                                     # decided once, then suppressed
    assert sum("staying on this seat" in m for m in msgs) == 1


def test_exit_after_structured_limit_hops_even_if_endpoint_unreachable(ctx, monkeypatch, tmp_path):
    """Codex often just ends the turn with usage_limit_exceeded and quits. That log line is positive
    evidence in hand, so the hop happens on exit even though the usage endpoint is unreachable and
    the ambiguous attachment kept us from acting mid-session."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    other = _rollout_session(ctx, cwd, thread="01aaaaaa")
    mine = _rollout_session(ctx, cwd, thread="01bbbbbb")
    get = fake_get({P.CODEX_USAGE_URL: (0, "")})   # endpoint down: nothing can corroborate anything

    def advance_both():
        _appender(other, token_count_line(primary=window(3.0), timestamp=_soon()), bump=3.0)()
        _appender(mine, task_complete_error_line(timestamp=_soon()), bump=6.0)()

    spawn = FakeSpawn([(None, 1, [advance_both]), (b"resumed\n", 0)])
    msgs = []

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0
    assert spawn.stops == 0                                 # the child ended on its own
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limit_source"] == "hard" and seat["limit_detail"] == OUT_OF_CREDITS
    assert any("hopping to b@x.com" in m for m in msgs)


def test_exit_after_structured_limit_preflights_the_landing_seat(ctx, monkeypatch, tmp_path):
    """The post-exit hop takes the SAME decision tail as a live one, so a hard signal still has to
    prove the landing seat. With the whole workspace out of credits there is nowhere to go: no hop,
    both seats rested, and the resumed session never ping-pongs between two creditless siblings."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    other = _rollout_session(ctx, cwd, thread="01aaaaaa")
    mine = _rollout_session(ctx, cwd, thread="01bbbbbb")
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        if len(gets) == 1:
            return 429, ""   # the mid-session probe can't corroborate → the child is left alone
        return 200, codex_credits_depleted_body()   # the pre-flight: the sibling is out too

    def advance_both():
        _appender(other, token_count_line(primary=window(3.0), timestamp=_soon()), bump=3.0)()
        _appender(mine, task_complete_error_line(timestamp=_soon()), bump=6.0)()

    msgs = []
    spawn = FakeSpawn([(None, 1, [advance_both])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 1 and rc != L.EXIT_GAVE_UP
    assert len(spawn.calls) == 1 and spawn.stops == 0        # no hop, no relaunch
    assert len(gets) == 2                                    # probe, then the landing pre-flight
    state = ctx.load_state()
    assert state.active("codex") == "a@x.com"
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"
    assert state.get_seat("codex", "b@x.com")["limited_until"] is not None
    assert sum("workspace" in m and "staying on this seat" in m for m in msgs) == 1


def _frozen_clock(monkeypatch):
    """Freeze BOTH clocks the supervisor reads: wall time (launcher + selection) and the monotonic
    one its cooldowns run on. Time then only moves when a test says so, which is what lets a rest
    expire with NO state write at all — exactly how a rest usually ends, and the case a change gate
    built on the state file's mtime can never see.
    """
    from acctsw import selection as SEL
    clock = {"t": now(), "mono": 1000.0}
    monkeypatch.setattr(L, "now", lambda: clock["t"])
    monkeypatch.setattr(SEL, "now", lambda: clock["t"])
    monkeypatch.setattr(L.time, "monotonic", lambda: clock["mono"])

    def advance(seconds):
        clock["t"] = clock["t"] + timedelta(seconds=seconds)
        clock["mono"] += seconds

    clock["advance"] = advance
    return clock


def test_tick_recovers_when_a_seat_frees_while_blocked(ctx, monkeypatch, tmp_path):
    """Blocked on a confirmed limit with every sibling resting, the session used to sit on the dead
    seat forever while the watcher merely announced that a seat had freed. When b's rest expires —
    by the clock, with nothing writing state — the next tick must carry the work over to it."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    clock = _frozen_clock(monkeypatch)
    state = _two_codex(ctx)  # active a
    state.set_limited_until("codex", "b@x.com", iso(clock["t"] + timedelta(seconds=1)),
                            source="usage")
    state.save()
    path = _rollout_session(ctx, cwd)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=10.0, secondary=10.0))})

    def time_passes():
        # b's rest expires and the re-check window passes. NOTHING writes state.json, so only a
        # clock-driven re-check can notice — the child would otherwise finish on the dead seat.
        clock["advance"](L.TICK_BLOCK_S + 10)

    msgs = []
    spawn = FakeSpawn([
        # exit 0: if the tick does not hop, this child simply completes and there is no second launch
        (None, 0, [_appender(path, task_complete_error_line(timestamp=_soon())), time_passes]),
        (b"resumed\n", 0),
    ])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0
    assert spawn.stops == 1                                  # it really did stop and hop, eventually
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    assert ctx.load_state().active("codex") == "b@x.com"
    assert any("staying on this seat" in m for m in msgs)    # ...after first being blocked
    assert any("hopping to b@x.com" in m for m in msgs)


def test_tick_recovery_never_fires_for_a_healthy_launch_seat(ctx, monkeypatch, tmp_path):
    """The recovery re-check may only rescue a session that is genuinely stuck. Once the seat we are
    running on is no longer rested (a poll proved it healthy again), a freed sibling is just another
    available seat — killing the live child for it would be the false-positive class all over again."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    clock = _frozen_clock(monkeypatch)
    state = _two_codex(ctx)  # active a
    state.set_limited_until("codex", "b@x.com", iso(clock["t"] + timedelta(seconds=1)),
                            source="usage")
    state.save()
    path = _rollout_session(ctx, cwd)
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=10.0, secondary=10.0))})

    def poll_clears_the_running_seat():
        clock["advance"](L.TICK_BLOCK_S + 10)                # b frees, the re-check window passes...
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "a@x.com", None)   # ...and a is proven healthy again
            st.save()

    msgs = []
    spawn = FakeSpawn([(None, 0, [_appender(path, task_complete_error_line(timestamp=_soon())),
                                  poll_clears_the_running_seat, None, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0
    assert spawn.stops == 0 and len(spawn.calls) == 1        # the healthy child was left alone
    assert ctx.load_state().active("codex") == "a@x.com"
    assert not any("hopping to" in m for m in msgs)


def test_hard_preflight_failure_restores_the_users_active_seat(ctx, monkeypatch, tmp_path):
    """The tail borrows ``active`` to name the seat this child actually runs on. When the hard
    landing pre-flight then refuses, that borrow must be given back: no hop happened, so a seat the
    user picked in the GUI still has to apply to their next session."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a, and a is the launch seat
    path = _rollout_session(ctx, cwd, thread="01bbbbbb")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_credits_depleted_body())})  # b is out too

    def gui_switch_then_limit():
        with ctx.locked():
            st = ctx.load_state()
            st.set_active("codex", "b@x.com")   # the user re-points the seat, mid-session
            st.save()
        _appender(path, task_complete_error_line(timestamp=_soon()), bump=6.0)()

    msgs = []
    spawn = FakeSpawn([(None, 3, [gui_switch_then_limit, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 3
    assert spawn.stops == 0 and len(spawn.calls) == 1        # nowhere to land: the child lives on
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"                # the user's choice survived untouched
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"   # ...and a was still rested
    assert sum("workspace" in m and "staying on this seat" in m for m in msgs) == 1


def test_probe_judges_the_launch_seat_not_active(ctx, monkeypatch, tmp_path):
    """The verify-before-kill probe must judge the seat this child was SPAWNED with. CODEX_HOME is
    process-global, so the credentials behind the fetch are that seat's — if another process moved
    ``active`` first, filing the answer under the sibling's name rests a healthy seat and rewrites
    its account_id, which reads as a re-subscription."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)  # active a, and a is the launch seat
    before_b = dict(ctx.load_state().get_seat("codex", "b@x.com"))
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})

    def gui_switch():
        with ctx.locked():
            st = ctx.load_state()
            st.set_active("codex", "b@x.com")
            st.save()

    spawn = FakeSpawn([
        ([b"booting\n", b"... you've hit your usage limit ...\n"], 1, [gui_switch]),
        (b"resumed\n", 0),
    ])

    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0 and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    seat_a, seat_b = state.get_seat("codex", "a@x.com"), state.get_seat("codex", "b@x.com")
    assert (seat_a.get("usage") or {}).get("windows")      # the fetch landed on the launch seat
    assert seat_a["limited_until"] is not None
    assert seat_b.get("usage") == before_b.get("usage")    # ...and never touched the sibling
    assert seat_b.get("account_id") == before_b.get("account_id")
    assert seat_b.get("limited_until") is None
    assert "moved_note" not in state.data                  # no phantom re-subscription
    assert state.active("codex") == "b@x.com"              # the hop still went where it should


def test_tick_limit_signals_do_not_refetch_while_blocked(ctx, monkeypatch, tmp_path):
    """A user pressing "continue" on a maxed seat writes a fresh usage_limit_exceeded per turn. Once
    we have decided and found nowhere to land, every later line must be recorded and ignored — not
    pay for another forced landing pre-flight."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    path = _rollout_session(ctx, cwd)
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        return 200, codex_credits_depleted_body()   # the sibling is out too: nowhere to land

    calls = {"n": 0}
    real_handle = L.handle_limit
    monkeypatch.setattr(L, "handle_limit",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         real_handle(*a, **k))[1])
    limit = _appender(path, task_complete_error_line(timestamp=_soon()))
    seen = {}
    spawn = FakeSpawn([(None, 4, [limit, limit, limit,
                                  lambda: seen.__setitem__("in_session", calls["n"])])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 4 and spawn.stops == 0
    assert seen["in_session"] == 1   # decided once for three identical limit events...
    assert len(gets) == 1            # ...and paid for exactly one landing pre-flight
    assert calls["n"] == 2           # (the second is the exit-time decision, on the same evidence)


def test_hard_banner_acts_when_attachment_is_ambiguous(ctx, monkeypatch, tmp_path):
    """The banner is only vetoed by a rollout we KNOW is this child's. With two sessions sharing the
    directory the attachment is a guess, so the fallback has to stay live — otherwise a wrong guess
    silences the structured source and the banner at the same time, and nothing ever hops."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    first = _rollout_session(ctx, cwd, thread="01aaaaaa")
    second = _rollout_session(ctx, cwd, thread="01bbbbbb")
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=10.0, secondary=10.0))})

    def both_move():
        # Neither line says anything about limits (a fresh session reports no windows yet), so the
        # attachment is ambiguous without any structured verdict to lean on either way.
        quiet = token_count_line(primary=None, secondary=None, timestamp=_soon())
        _appender(first, quiet, bump=3.0)()
        _appender(second, quiet, bump=6.0)()

    spawn = FakeSpawn([
        ([b"booting\n", (REFILL_BANNER + "\n").encode()], 1, [both_move]),
        (b"resumed\n", 0),
    ])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)

    assert rc == 0 and spawn.stops == 1
    assert spawn.calls[1][-2:] == ["resume", "--last"]
    state = ctx.load_state()
    assert state.active("codex") == "b@x.com"
    assert state.get_seat("codex", "a@x.com")["limit_source"] == "hard"


# --- the edges of the decision tail ------------------------------------------------------------

def test_handle_limit_never_shortens_a_rest_we_already_believe_in(ctx):
    """A structured signal can carry an EARLIER unlock than the one already on the seat: the 5-hour
    window reopens long before the weekly one that is actually blocking us. Re-anchoring to the
    nearer time would send the launcher back to a still-capped seat the moment it passes."""
    state = _two_codex(ctx)  # active a
    later = iso(now() + timedelta(hours=3))
    state.set_limited_until("codex", "a@x.com", later, source="usage")
    state.save()

    dec = handle_limit(ctx, state, "codex", get=fake_get({}), corroborated=True, hard=False,
                       reset_at=iso(now() + timedelta(hours=1)), source="usage")

    seat = state.get_seat("codex", "a@x.com")
    assert L.parse_iso(seat["limited_until"]) == L.parse_iso(later)
    assert dec.action == "switch" and dec.email == "b@x.com"


def test_run_auth_death_names_the_resting_sibling_instead_of_a_relogin(ctx):
    """Dead credentials while the only other seat is asleep. Both walls block the hop, but the
    remedies differ: telling this user to re-add a seat would send them to fix the wrong thing —
    the seat exists, it is merely resting. Say when it comes back, and leave the child running."""
    state = _two_codex(ctx)  # active a
    unlocks = iso(now() + timedelta(hours=2))
    state.set_limited_until("codex", "b@x.com", unlocks, source="usage")
    state.save()
    get = fake_get({P.CODEX_USAGE_URL: (401, "")})  # inconclusive: it must not dismiss the banner
    msgs = []
    spawn = FakeSpawn([(b"... your refresh token was revoked ...\n", 7)])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 7 and rc != L.EXIT_GAVE_UP
    assert spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"
    hints = [m for m in msgs if "sign in again" in m]
    assert len(hints) == 1
    assert "is resting until" in hints[0]
    assert "acctsw add" not in hints[0]   # the seat is already there; re-adding it fixes nothing


def test_tick_survives_a_state_file_that_disappears(ctx):
    """The store is written atomically, but nothing stops the data dir being taken away mid-session
    (a restore, a stray rm, an uninstall losing a race). The heartbeat reads it every two seconds;
    raising there would kill a child that is running perfectly well over pure bookkeeping."""
    _two_codex(ctx)

    def wipe_the_store():
        ctx.state_file.unlink()

    msgs = []
    spawn = FakeSpawn([(None, 0, [wipe_the_store, None, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)

    assert rc == 0 and spawn.stops == 0 and len(spawn.calls) == 1
    assert msgs == []


def test_tick_treats_a_touched_state_file_as_unchanged(ctx, monkeypatch):
    """The mtime is only a cheap pre-filter: anything that rewrites or touches the file moves it.
    The REVISION is what says the data moved. Without that second gate, a seat already judged would
    be re-decided — and re-notified — every time some other process breathed on the store."""
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    calls = {"n": 0}
    real_handle = L.handle_limit
    monkeypatch.setattr(L, "handle_limit",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         real_handle(*a, **k))[1])

    def poll_rests_it():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "solo@x.com", iso(now() + timedelta(hours=3)),
                                 source="usage")
            st.save()

    def touch_without_writing():
        stamp = ctx.state_file.stat().st_mtime_ns + 1_000_000_000
        os.utime(ctx.state_file, ns=(stamp, stamp))

    msgs = []
    spawn = FakeSpawn([(None, 3, [poll_rests_it] + [touch_without_writing] * 5)])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)

    assert rc == 3 and spawn.stops == 0
    assert calls["n"] == 1                                  # one real change, five bare touches
    assert sum("staying on this seat" in m for m in msgs) == 1


def test_tick_recovery_respects_a_switch_budget_that_is_already_spent(ctx, monkeypatch):
    """The clock-driven re-check exists to rescue a session stuck on a rested seat. It must not
    become a way around the switch bound: with the budget spent there is no hop to make, however
    healthy the sibling looks, so the re-check has to fall silent instead of re-deciding forever."""
    clock = _frozen_clock(monkeypatch)
    _two_codex(ctx)  # active a, healthy b

    def menubar_rests_a():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "a@x.com", iso(clock["t"] + timedelta(hours=3)),
                                 source="usage")
            st.save()

    def time_passes():
        clock["advance"](L.TICK_BLOCK_S + 10)   # the re-check window opens, with nothing written

    msgs = []
    spawn = FakeSpawn([(None, 4, [menubar_rests_a, None, time_passes, None, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append,
             max_switches=0)

    assert rc == 4 and spawn.stops == 0 and len(spawn.calls) == 1
    assert ctx.load_state().active("codex") == "a@x.com"    # the budget held; no hop happened
    assert sum("switch limit" in m for m in msgs) == 1      # ...and it was said exactly once


def test_tick_recovery_stays_quiet_while_no_seat_has_freed(ctx, monkeypatch):
    """Blocked on the only seat there is. The re-check runs on the CLOCK, so it fires again and
    again with nothing to find; each round must end in silence rather than a second notification or
    another decision, and the child has to keep its terminal throughout."""
    clock = _frozen_clock(monkeypatch)
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    calls = {"n": 0}
    real_handle = L.handle_limit
    monkeypatch.setattr(L, "handle_limit",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         real_handle(*a, **k))[1])

    def poll_rests_it():
        with ctx.locked():
            st = ctx.load_state()
            st.set_limited_until("codex", "solo@x.com", iso(clock["t"] + timedelta(hours=3)),
                                 source="usage")
            st.save()

    def time_passes():
        clock["advance"](L.TICK_BLOCK_S + 10)

    msgs = []
    sleeps = []
    spawn = FakeSpawn([(None, 6, [poll_rests_it, None, time_passes, None, time_passes, None])])

    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append,
             sleep=sleeps.append)

    assert rc == 6 and spawn.stops == 0 and len(spawn.calls) == 1
    assert calls["n"] == 1
    assert sum("staying on this seat" in m for m in msgs) == 1
    assert not any("hopping to" in m for m in msgs)


def test_tick_ambiguous_limit_lines_share_one_probe_cooldown(ctx, monkeypatch, tmp_path):
    """Two sessions in one directory, so every limit line needs the endpoint to agree before it may
    touch a live child. A maxed seat writes one such line PER TURN and a user can keep pressing
    continue — without the shared cooldown each of them would re-hit the endpoint that just told us
    nothing, which is the hammering the stdout path already guards against."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    _two_codex(ctx)  # active a
    other = _rollout_session(ctx, cwd, thread="01aaaaaa")
    mine = _rollout_session(ctx, cwd, thread="01bbbbbb")
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        return 429, ""   # throttled: it can corroborate nothing, now or on any retry

    def first_turn():
        _appender(other, token_count_line(primary=window(3.0), timestamp=_soon()), bump=3.0)()
        _appender(mine, task_complete_error_line(timestamp=_soon()), bump=6.0)()

    def another_turn():
        _appender(mine, task_complete_error_line(timestamp=_soon()), bump=9.0)()

    msgs = []
    spawn = FakeSpawn([(None, 0, [first_turn, another_turn, another_turn, another_turn])])

    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=msgs.append)

    assert rc == 0 and spawn.stops == 0 and len(spawn.calls) == 1
    assert gets == [P.CODEX_USAGE_URL]                      # four limit lines, one probe
    state = ctx.load_state()
    assert state.active("codex") == "a@x.com"
    assert state.get_seat("codex", "a@x.com").get("limited_until") is None
    assert msgs == []


@pytest.mark.parametrize('with_pty', [True, False])
def test_terminate_output_backpressure_returns_under_timeout(with_pty):
    """The macOS tty-close repro: fill an unread PTY, then terminate in a bounded thread."""
    import contextlib
    import subprocess
    master = None
    child = None
    if with_pty:
        pid, master = L.pty.fork()
        if pid == 0:
            os.execl('/bin/sh', 'sh', '-c', 'exec yes aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
        time.sleep(0.2)  # deliberately do not read the master before shutdown
    else:
        # Also exercise a caller without a separate process group: never signal pytest's group.
        child = subprocess.Popen(['/bin/sleep', '30'])
        pid = child.pid
    result = []
    worker = threading.Thread(target=lambda: result.append(L._terminate(pid, master)), daemon=True)
    try:
        started = time.monotonic()
        worker.start()
        worker.join(3)
        assert not worker.is_alive(), 'shutdown stopped draining the PTY'
        assert time.monotonic() - started < 3
        assert result and result[0] < 0
        if child is not None:
            child.returncode = result[0]  # _terminate already reaped it
        if master is not None:
            with pytest.raises(OSError):
                os.fstat(master)  # ownership was transferred, so it must be closed
    finally:
        if worker.is_alive():
            if master is not None:
                with contextlib.suppress(OSError):
                    os.close(master)  # releases the original buggy wait4 on macOS
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, L.signal.SIGKILL)  # only our own still-unreaped child
            worker.join(3)
        if child is not None:
            child.wait(timeout=3)


@pytest.mark.parametrize('master', [123, None])
def test_terminate_unreapable_child_closes_master_and_bounds_final_wait(monkeypatch, caplog, master):
    clock, events = [0.0], []
    monkeypatch.setattr(L.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(L.time, 'sleep', lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(L.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(L.os, 'killpg', lambda pid, sig: events.append(('signal', sig, clock[0])))
    monkeypatch.setattr(L.os, 'set_blocking', lambda *args: None)
    monkeypatch.setattr(L.os, 'close', lambda fd: events.append(('close', fd, clock[0])))
    def waitpid(pid, flags):
        assert flags == os.WNOHANG
        return 0, 0
    def select(read, write, exc, delay):
        clock[0] += delay
        return [], [], []
    monkeypatch.setattr(L.os, 'waitpid', waitpid)
    monkeypatch.setattr(L.select, 'select', select)
    assert L._terminate(4242, master) == -L.signal.SIGKILL
    expected = [('signal', L.signal.SIGTERM, 0), ('signal', L.signal.SIGKILL, 5)]
    if master is not None:
        expected.append(('close', master, 6))
    assert events == expected
    assert clock[0] == pytest.approx(7)
    assert 'leaving it for init' in caplog.text


def test_pty_stop_drains_sigterm_session_save(tmp_path):
    import sys
    saved = tmp_path / 'saved'
    script = tmp_path / 'chatty.py'
    script.write_text('''import os, signal, sys, time
from pathlib import Path
def stop(sig, frame):
    data = b'a' * (2 * 1024 * 1024)
    while data:
        data = data[os.write(1, data):]
    Path(sys.argv[1]).write_text('saved')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print('ready', flush=True)
while True: time.sleep(1)
''')
    assert L.pty_spawn([sys.executable, str(script), str(saved)], lambda data: b'ready' in data) == 0
    assert saved.read_text() == 'saved'


@pytest.mark.parametrize('via_signal', [True, False])
def test_pty_shutdown_handler_is_not_reentrant(monkeypatch, via_signal):
    handlers, calls, deaths = {}, [], []
    real_terminate = L._terminate
    real_kill = os.kill
    def install(sig, handler):
        handlers[sig] = handler
        return L.signal.SIG_DFL
    def terminate(pid, master_fd=None):
        calls.append(pid)
        handlers[L.signal.SIGWINCH](L.signal.SIGWINCH, None)
        handlers[L.signal.SIGHUP](L.signal.SIGHUP, None)
        handlers[L.signal.SIGTERM](L.signal.SIGTERM, None)
        return real_terminate(pid, master_fd)
    def kill(pid, sig):
        if pid == os.getpid():
            deaths.append(sig)  # never signal the pytest runner
        else:
            real_kill(pid, sig)
    def output(data):
        if via_signal:
            handlers[L.signal.SIGTERM](L.signal.SIGTERM, None)
            raise RuntimeError('simulated supervisor exit')
        return True
    monkeypatch.setattr(L.signal, 'signal', install)
    monkeypatch.setattr(L, '_terminate', terminate)
    monkeypatch.setattr(L.os, 'kill', kill)
    if via_signal:
        with pytest.raises(RuntimeError, match='simulated supervisor exit'):
            L.pty_spawn(['/bin/sh', '-c', 'echo ready; exec sleep 30'], output)
    else:
        L.pty_spawn(['/bin/sh', '-c', 'echo ready; exec sleep 30'], output)
    assert len(calls) == 1
    assert deaths == [L.signal.SIGTERM]
