"""Feature tests for the supervised launcher — auto-switch + resume, via a scripted fake spawn.

No real PTY, no network: `spawn` is injected and `get` returns canned usage.
"""
import json
import os

import pytest

from acctsw import accounts as acct
from acctsw import launcher as L
from acctsw.launcher import (NoSeats, build_cmd, resume_cmd, detect_limit, detect_auth_dead,
                             detect_hard_limit, handle_limit, handle_auth_dead, run)
from acctsw.util import now, iso
from datetime import timedelta
from tests.conftest import make_claude_blob, make_codex_blob
from tests.test_usage import fake_get, claude_ok_body, codex_ok_body
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
    """Returns scripted (output, status) per call; records argv of each launch. ``output`` may be
    a list of chunks to exercise repeated on_output calls within ONE child session; like the real
    pty_spawn, the child "dies" (remaining chunks dropped) once on_output asks for a stop."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def __call__(self, argv, on_output):
        out, status = self.scripts[len(self.calls)]
        self.calls.append(list(argv))
        for chunk in (out if isinstance(out, list) else [out]):
            if on_output(chunk):
                break
        return status


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
    assert ctx.load_state().active("codex") == "b@x.com"
    assert any("hopping to b@x.com" in m for m in msgs)


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
    assert state.get_seat("codex", "primary@example.test")["limited_until"] is not None
    assert any("primary+codex@example.test" in m for m in msgs)


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


def test_run_unverifiable_limit_never_gives_up(ctx):
    """End-to-end guard against the false 'all seats resting' kill: every usage probe fails to
    connect (e.g. the Headroom proxy in front of the endpoint is flapping), so a limit line can
    never be corroborated. The supervisor must keep the session alive on the same seat and return
    the child's own exit code — never EXIT_GAVE_UP, never a 5h rest."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (0, "")})   # endpoint unreachable on every probe
    spawn = FakeSpawn([
        (b"... you've hit your usage limit ...\n", 1),  # kill-path: probe can't confirm → resume
        (b"resumed, all good\n", 0),                    # same seat, carried on to a clean finish
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0 and rc != L.EXIT_GAVE_UP
    assert spawn.calls[1][-2:] == ["resume", "--last"]       # resumed the SAME seat's work
    assert ctx.load_state().active("codex") == "a@x.com"     # never switched away
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None  # never rested


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


def test_run_gives_up_with_relogin_hint_when_only_seat_revoked(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    msgs = []
    spawn = FakeSpawn([(b"refresh token was revoked\n", 1)])
    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert rc == L.EXIT_GAVE_UP
    assert any("sign in again" in m for m in msgs)


def test_run_respects_max_switches(ctx, monkeypatch):
    _two_codex(ctx)
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})  # genuinely maxed
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    # every launch hits a real limit; with max_switches=1 we get: launch, switch, launch(limit)->stop
    spawn = FakeSpawn([(b"usage limit reached\n", 1)] * 5)
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=1, notify=lambda m: None)
    assert len(spawn.calls) == 2  # initial + one resume, then bail
    assert rc == L.EXIT_GAVE_UP


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
    as out: rest it reactively (so choose() won't re-pick it) and hop to the healthy seat."""
    state = _two_codex(ctx)  # active a
    body = json.dumps({"rate_limit": {"limit_reached": True,
                       "primary_window": {"used_percent": 50},
                       "secondary_window": {"used_percent": 50}}})   # maxed flag, no window≥100, no reset
    get = fake_get({P.CODEX_USAGE_URL: (200, body)})
    dec = L.handle_exhausted(ctx, state, "codex", get=get)
    assert dec.action == "switch" and dec.email == "b@x.com"
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None and seat["limit_source"] == "reactive"


def test_run_codex_home_preserved_on_exception(ctx):
    """Codex isolation: a crash must not corrupt/lose the active account's per-account home
    (the source of truth codex maintains); the finally mirrors home → ~/.codex."""
    state = _two_codex(ctx)  # active a, home(a) populated
    before = ctx.snapshot_get("codex", "a@x.com")

    def boom(argv, on_output):
        raise RuntimeError("pty exploded")

    with pytest.raises(RuntimeError):
        run(ctx, "codex", [], spawn=boom, get=fake_get({}))
    assert ctx.snapshot_get("codex", "a@x.com") == before        # home intact
    assert ctx.cred["codex"].get_live() == before                # mirrored home → live


def test_run_claude_resume_uses_continue(ctx):
    for em in ("c1@x.com", "c2@x.com"):
        ctx.cred["claude"].set_live(__import__("tests.conftest", fromlist=["make_claude_blob"]).make_claude_blob())
        st = ctx.load_state()
        acct.add(ctx, st, "claude", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "claude", "c1@x.com")
    get = fake_get({P.CLAUDE_USAGE_URL: (429, "")})
    spawn = FakeSpawn([(b"usage limit reached\n", 1), (b"resumed\n", 0)])
    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert spawn.calls[1][-1] == "--continue"


def test_run_claude_waits_and_resumes_when_all_seats_resting(ctx):
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
    assert spawn.calls[0][-1] == "--continue"
    assert any("waiting until" in m for m in msgs)
    assert ctx.load_state().get_seat("claude", "c1@x.com").get("limited_until") is None


def test_run_claude_wait_activates_soonest_unlocked_seat(ctx):
    state = _two_claude(ctx)  # active c1
    c1_reset = iso(now() + timedelta(seconds=60))
    c2_reset = iso(now() + timedelta(seconds=30))
    state.set_limited_until("claude", "c2@x.com", c2_reset, source="usage")
    state.save()
    get = fake_get({P.CLAUDE_USAGE_URL: (200, claude_ok_body(five=100.0,
                                                             five_reset=c1_reset))})
    sleeps = []
    spawn = FakeSpawn([(b"usage limit reached\n", 1), (b"resumed\n", 0)])

    rc = run(ctx, "claude", [], spawn=spawn, get=get, notify=lambda m: None,
             sleep=sleeps.append)

    assert rc == 0
    assert len(sleeps) == 1
    assert spawn.calls[1][-1] == "--continue"
    assert ctx.load_state().active("claude") == "c2@x.com"


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


def test_pty_spawn_nonzero_exit_propagates():
    rc = L.pty_spawn(["/bin/sh", "-c", "exit 7"], lambda c: False)
    assert rc == 7


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
