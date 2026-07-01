"""Feature tests for the supervised launcher — auto-switch + resume, via a scripted fake spawn.

No real PTY, no network: `spawn` is injected and `get` returns canned usage.
"""
import pytest

from acctsw import accounts as acct
from acctsw import launcher as L
from acctsw.launcher import (NoSeats, build_cmd, resume_cmd, detect_limit, detect_auth_dead,
                             handle_limit, handle_auth_dead, run)
from acctsw.util import now, iso
from datetime import timedelta
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get, codex_ok_body
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
])
def test_detect_limit_no_false_positive_on_benign(benign):
    assert not detect_limit("codex", benign)
    assert not detect_limit("claude", benign)


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


class FakeSpawn:
    """Returns scripted (output, status) per call; records argv of each launch."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def __call__(self, argv, on_output):
        out, status = self.scripts[len(self.calls)]
        self.calls.append(list(argv))
        on_output(out)
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


def test_handle_limit_reactive_fallback_when_usage_says_fine(ctx):
    state = _two_codex(ctx)
    # usage endpoint errors → no authoritative reset; reactive fallback must still flag the seat
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    seat = state.get_seat("codex", "a@x.com")
    assert seat["limited_until"] is not None
    assert seat["limit_source"] == "reactive"
    assert dec.action == "switch" and dec.email == "b@x.com"


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


def test_run_resumes_same_seat_on_false_positive(ctx):
    """A benign-but-matching line on a healthy seat resumes the SAME seat (no switch, no rest)."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    spawn = FakeSpawn([
        (b"... you've hit your usage limit ...\n", 1),  # false positive: usage says a is fine
        (b"resumed, all good\n", 0),
    ])
    rc = run(ctx, "codex", ["--foo"], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == 0
    assert spawn.calls[1][-2:] == ["resume", "--last"]   # resumed, not a fresh build
    assert ctx.load_state().active("codex") == "a@x.com"  # never switched away
    assert ctx.load_state().get_seat("codex", "a@x.com").get("limited_until") is None


def test_run_bails_with_gave_up_code_after_repeated_false_positives(ctx):
    """A seat that keeps looking limited while usage says it's healthy stops supervising after the
    bound — and reports EXIT_GAVE_UP (the "we gave up" sentinel), not the killed child's status."""
    _two_codex(ctx)  # active a
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=20.0, secondary=70.0))})
    # every relaunch trips the detector again on healthy 'a' → resume, until the bound is exceeded
    spawn = FakeSpawn([(b"... you've hit your usage limit ...\n", 1)] * (L.MAX_FALSE_ALARMS + 2))
    rc = run(ctx, "codex", [], spawn=spawn, get=get, notify=lambda m: None)
    assert rc == L.EXIT_GAVE_UP
    assert len(spawn.calls) == L.MAX_FALSE_ALARMS + 1   # initial + MAX_FALSE_ALARMS resumes, then bail
    assert ctx.load_state().active("codex") == "a@x.com"  # never switched away


def test_seat_confirmed_healthy_tolerates_malformed_window(ctx):
    """A corrupt/partial usage blob (a null window) must not crash the healthy-seat guard."""
    state = _two_codex(ctx)  # active a
    state.set_usage("codex", "a@x.com", {"ok": True, "error": None, "limit_reached": False,
                                         "windows": {"primary": None, "secondary": {"used_pct": 20.0}}})
    state.save()
    summary = {"codex": {"a@x.com": "ok"}}  # endpoint reported success → guard inspects windows
    # null window must be skipped, not crash; the valid 20% window still confirms headroom
    assert L._seat_confirmed_healthy(state, "codex", "a@x.com", summary) is True


def test_handle_limit_gives_up_when_all_limited(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email="solo@x.com")
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    dec = handle_limit(ctx, state, "codex", get=get)
    assert dec.action == "give_up"


# --- run orchestration ------------------------------------------------------------------------

def test_run_no_seats_raises(ctx):
    with pytest.raises(NoSeats):
        run(ctx, "codex", [], spawn=FakeSpawn([]))


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
    # after an auth hop, a later limit must NOT re-select the seat that already died this run
    state = _two_codex(ctx)  # active a, b available
    dec = handle_limit(ctx, state, "codex", get=fake_get({}), exclude={"b@x.com"})
    assert dec.action == "give_up"          # b excluded, a just limited → nothing to switch to
    # sanity: without the exclude it WOULD switch to b
    state2 = _two_codex(ctx)
    assert handle_limit(ctx, state2, "codex", get=fake_get({})).action == "switch"


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


def test_run_gives_up_with_relogin_hint_when_only_seat_revoked(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("solo@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="solo@x.com")
    msgs = []
    spawn = FakeSpawn([(b"refresh token was revoked\n", 1)])
    rc = run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert rc == L.EXIT_GAVE_UP
    assert any("sign in again" in m for m in msgs)


def test_run_respects_max_switches(ctx):
    _two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    # every launch hits a limit; with max_switches=1 we get: launch, switch, launch(limit)->stop
    spawn = FakeSpawn([(b"usage limit reached\n", 1)] * 5)
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=1, notify=lambda m: None)
    assert len(spawn.calls) == 2  # initial + one resume, then bail
    assert rc == L.EXIT_GAVE_UP


def test_run_propagates_nonzero_clean_exit(ctx):
    _two_codex(ctx)
    spawn = FakeSpawn([(b"build failed\n", 3)])  # no limit text → clean (failed) exit
    assert run(ctx, "codex", [], spawn=spawn, get=fake_get({})) == 3


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
