"""Feature tests for the supervised launcher — auto-switch + resume, via a scripted fake spawn.

No real PTY, no network: `spawn` is injected and `get` returns canned usage.
"""
import pytest

from acctsw import accounts as acct
from acctsw import launcher as L
from acctsw.launcher import NoSeats, build_cmd, resume_cmd, detect_limit, handle_limit, run
from acctsw.util import now, iso
from datetime import timedelta
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get, codex_ok_body
from acctsw import paths as P


# --- pure helpers -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Error: you've hit your usage limit", "rate limit exceeded", "5-hour limit reached",
    "HTTP 429 Too Many Requests", "you are out of credits",
])
def test_detect_limit_positive(text):
    assert detect_limit("codex", text) or detect_limit("claude", text)


def test_detect_limit_negative():
    assert not detect_limit("codex", "compiling project, running tests, all green")


@pytest.mark.parametrize("benign", [
    "the cache resets at midnight", "please try again in a moment",
    "approaching the recursion limit", "rate limiting middleware installed",
])
def test_detect_limit_no_false_positive_on_benign(benign):
    assert not detect_limit("codex", benign)
    assert not detect_limit("claude", benign)


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


def test_run_respects_max_switches(ctx):
    _two_codex(ctx)
    get = fake_get({P.CODEX_USAGE_URL: (429, "")})
    # every launch hits a limit; with max_switches=1 we get: launch, switch, launch(limit)->stop
    spawn = FakeSpawn([(b"usage limit\n", 1)] * 5)
    rc = run(ctx, "codex", [], spawn=spawn, get=get, max_switches=1, notify=lambda m: None)
    assert len(spawn.calls) == 2  # initial + one resume, then bail
    assert rc == L.EXIT_GAVE_UP


def test_run_propagates_nonzero_clean_exit(ctx):
    _two_codex(ctx)
    spawn = FakeSpawn([(b"build failed\n", 3)])  # no limit text → clean (failed) exit
    assert run(ctx, "codex", [], spawn=spawn, get=fake_get({})) == 3


def test_run_syncs_back_on_exception(ctx):
    """B2: sync-back must run even if spawn raises mid-loop."""
    state = _two_codex(ctx)  # active a
    # rotate a's live token, then make spawn explode
    rotated = make_codex_blob("a@x.com").replace('"refresh_token": "r"', '"refresh_token": "ROT"')
    ctx.cred["codex"].set_live(rotated)

    def boom(argv, on_output):
        raise RuntimeError("pty exploded")

    with pytest.raises(RuntimeError):
        run(ctx, "codex", [], spawn=boom, get=fake_get({}))
    # a's rotated token was synced back to its snapshot despite the crash
    import json
    snap = json.loads(ctx.keychain.get(ctx.keychain_service, "codex:a@x.com"))
    assert snap["tokens"]["refresh_token"] == "ROT"


def test_run_claude_resume_uses_continue(ctx):
    for em in ("c1@x.com", "c2@x.com"):
        ctx.cred["claude"].set_live(__import__("tests.conftest", fromlist=["make_claude_blob"]).make_claude_blob())
        st = ctx.load_state()
        acct.add(ctx, st, "claude", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "claude", "c1@x.com")
    get = fake_get({P.CLAUDE_USAGE_URL: (429, "")})
    spawn = FakeSpawn([(b"usage limit\n", 1), (b"resumed\n", 0)])
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
