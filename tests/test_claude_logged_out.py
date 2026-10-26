"""A supervised Claude child that shows its own "Not logged in · Please run /login" banner.

Whatever the cause (another tool rewrote or deleted the seat's item, an old engine left it base64, a
race), the seat usually still has a good login: put it back where THAT child reads it, as raw JSON,
and restart on the same seat with resume. Only a seat with no usable login takes the dead-seat hop.
"""
import json

import pytest

from acctsw import accounts, claudehome, claudesync
from acctsw import launcher as L
from acctsw.keychain import _encode
from tests.conftest import make_claude_blob
from tests.test_usage import claude_ok_body

BANNER = "⏺ Not logged in · Please run /login\n".encode()


def setup(ctx):
    for email, token in [("b@test", "b"), ("a@test", "a")]:
        ctx.cred["claude"].set_live(make_claude_blob().replace('"x"', json.dumps(token)))
        accounts.add(ctx, ctx.load_state(), "claude", email=email)
    assert ctx.load_state().active("claude") == "a@test"


def _item(ctx, env):
    """The exact item a child with this env reads: keyed by its CLAUDE_CONFIG_DIR string."""
    return claudehome.service_name(env["CLAUDE_CONFIG_DIR"]), ctx.cred["claude"].account


def _ok(seen):
    def get(url, headers, timeout):
        seen.append(headers["Authorization"])
        return 200, claude_ok_body()
    return get


# --- detection --------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Not logged in · Please run /login",
    "⏺ Not logged in · Please run /login\n",
    "  ⎿  Not logged in · Please run /login\n> ",
    "? for shortcuts                     Not logged in · Run /login",
    "\x1b[31mNot logged in\x1b[39m · Run \x1b[1m/login\x1b[22m\r\n",
    "Not logged in · Please run\n/login\n",              # wrapped by the terminal
    "Not logged in • Run /login   ",                     # a different separator glyph
    "Login expired · Please run /login\n",
    "OAuth token revoked · Please run /login\n",
    "Missing API key · Run /login\n",
])
def test_detects_claude_logged_out_banners(text):
    assert L.detect_logged_out("claude", text)


@pytest.mark.parametrize("text", [
    "you are not logged in, please run /login to fix it",
    "If it says you're not logged in · run /login and retry",
    "the banner reads Not logged in · Please run /login.",
    "Claude showed `Not logged in · Run /login` in the status line",
    'assert detect("Not logged in · Please run /login")',
    "Not logged in · Please run /login to continue",
    "It said: Not logged in · Run /login\n",
    "Not logged in. Please run /login\n",
    "Then: Please run /login\n",
    "12 + Not logged in · Please run /login\n",
])
def test_prose_about_being_logged_out_is_not_the_banner(text):
    assert not L.detect_logged_out("claude", text)
    assert L.detect_event("claude", text) is None


def test_logged_out_banner_is_claude_only():
    assert not L.detect_logged_out("codex", "Not logged in · Run /login\n")


# --- same-seat recovery -----------------------------------------------------------------------

def test_missing_home_item_is_restored_as_raw_json_and_same_seat_resumes(ctx):
    setup(ctx)
    snapshot = ctx.snapshot_get("claude", "a@test")
    calls, msgs, auth = [], [], []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append((argv, env["CLAUDE_CONFIG_DIR"]))
        if len(calls) == 1:
            ctx.keychain.delete(*_item(ctx, env))   # another tool removed the child's item
            assert output(BANNER)
            return 1
        assert argv == ["claude", "--resume", "s1"]
        assert ctx.keychain.get_raw(*_item(ctx, env)) == snapshot   # raw JSON, never base64
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=_ok(auth),
                 notify=msgs.append) == 0
    assert [home for _, home in calls] == [str(ctx.claude_home("a@test"))] * 2
    assert auth == ["Bearer a"]      # the snapshot was proven before it was installed
    assert ctx.load_state().active("claude") == "a@test"
    assert any("restored its login" in m for m in msgs)
    assert not any("hopping" in m for m in msgs)


def test_base64_home_item_is_rewritten_raw_and_same_seat_resumes(ctx):
    setup(ctx)
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(env["CLAUDE_CONFIG_DIR"])
        if len(calls) == 1:
            blob = ctx.keychain.get_raw(*_item(ctx, env))
            ctx.keychain.set_raw(*_item(ctx, env), _encode(blob))   # what 1.2.0 left behind
            assert output(BANNER)
            return 1
        assert json.loads(ctx.keychain.get_raw(*_item(ctx, env)))["claudeAiOauth"]["accessToken"] == "a"
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=_ok([])) == 0
    assert calls == [str(ctx.claude_home("a@test"))] * 2


def test_child_that_exits_on_the_banner_is_recovered_the_same_way(ctx):
    """`claude -p` prints the banner and exits by itself; the exit path must not lose it."""
    setup(ctx)
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(env["CLAUDE_CONFIG_DIR"])
        if len(calls) == 1:
            ctx.keychain.delete(*_item(ctx, env))
            output(BANNER)   # the child is already gone; whatever the callback answers
            return 1
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=_ok([])) == 0
    assert calls == [str(ctx.claude_home("a@test"))] * 2


@pytest.mark.parametrize("damage", ["wiped", "no_snapshot", "snapshot_disproved"])
def test_seat_without_a_usable_login_takes_the_dead_seat_hop(ctx, damage):
    setup(ctx)
    calls, msgs = [], []
    wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}})

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append((argv, env["CLAUDE_CONFIG_DIR"]))
        if len(calls) == 1:
            if damage == "wiped":   # Claude's own verdict on the bytes it held (invalid_grant)
                ctx.keychain.set_raw(*_item(ctx, env), wiped)
            else:
                ctx.keychain.delete(*_item(ctx, env))
                if damage == "no_snapshot":
                    ctx.keychain.delete(ctx.keychain_service, ctx.snapshot_key("claude", "a@test"))
            assert output(BANNER)
            return 1
        assert argv == ["claude", "--resume", "s1"]
        return 0

    def get(url, headers, timeout):   # only seat a's snapshot token is revoked
        return (401, "") if headers["Authorization"] == "Bearer a" else (200, claude_ok_body())

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=get,
                 notify=msgs.append) == 0
    assert [home for _, home in calls] == [str(ctx.claude_home(e)) for e in ("a@test", "b@test")]
    assert any("sign in again" in m and "hopping to b@test" in m for m in msgs)
    if damage == "wiped":
        assert ctx.keychain.get_raw(claudehome.service_name(str(ctx.claude_home("a@test"))),
                                    ctx.cred["claude"].account) == wiped   # never overwritten


def test_unverifiable_snapshot_is_never_installed(ctx):
    """A snapshot is WRITTEN only on positive proof: a stale one can wipe the seat for everyone."""
    setup(ctx)
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(env["CLAUDE_CONFIG_DIR"])
        if len(calls) == 1:
            ctx.keychain.delete(*_item(ctx, env))
            assert output(BANNER)
            return 1
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn,
                 get=lambda *a: (503, "")) == 0
    assert calls == [str(ctx.claude_home("a@test")), str(ctx.claude_home("b@test"))]
    assert ctx.claude_cred("a@test").get_live() is None


def test_restore_loop_guard_falls_back_to_the_dead_seat_hop(ctx):
    setup(ctx)
    calls, msgs = [], []
    probes = {"n": 0}

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(env["CLAUDE_CONFIG_DIR"])
        if len(calls) <= 2:
            assert output(BANNER)   # the restart did not help: the banner is back at once
            return 1
        return 0

    def get(url, headers, timeout):   # the restore's check passes; the token then stops working
        probes["n"] += 1
        return (200, claude_ok_body()) if probes["n"] == 1 else (401, "")

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=get,
                 notify=msgs.append) == 0
    a, b = str(ctx.claude_home("a@test")), str(ctx.claude_home("b@test"))
    assert calls == [a, a, b]   # exactly one same-seat restart, then the ordinary hop
    assert sum("restored its login" in m for m in msgs) == 1


def test_restore_restarts_count_against_the_relaunch_budget(ctx, monkeypatch):
    setup(ctx)
    monkeypatch.setattr(L, "RELOGIN_COOLDOWN_S", 0.0)
    monkeypatch.setattr(L, "PROBE_COOLDOWN_S", 0.0)
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(env["CLAUDE_CONFIG_DIR"])
        stopped = output(BANNER)
        return 1 if stopped else 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=_ok([]),
                 max_switches=2) == 0
    # Two restarts spend the budget; the third banner may neither restart nor hop.
    assert calls == [str(ctx.claude_home("a@test"))] * 3


def test_prose_in_a_live_session_never_restarts_it(ctx):
    setup(ctx)
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        for line in (b"you are not logged in, please run /login to fix it\n",
                     "the banner reads Not logged in · Please run /login.\n".encode(),
                     "Claude showed `Not logged in · Run /login`\n".encode()):
            assert not output(line)
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn,
                 get=lambda *a: pytest.fail("prose must not even probe")) == 0
    assert len(calls) == 1


def test_auto_switch_off_restores_the_login_but_leaves_the_child_alone(ctx):
    setup(ctx)
    state = ctx.load_state()
    state.settings()["auto_switch"] = False
    state.save()
    msgs = []

    def spawn(argv, output, on_tick=None, *, env=None):
        ctx.keychain.delete(*_item(ctx, env))
        assert not output(BANNER)
        assert ctx.keychain.get_raw(*_item(ctx, env)) == ctx.snapshot_get("claude", "a@test")
        return 0

    assert L.run(ctx, "claude", ["--session-id", "s1"], spawn=spawn, get=_ok([]),
                 notify=msgs.append) == 0
    assert any("auto-switch is off" in m for m in msgs)


# --- which login may be put back ----------------------------------------------------------------

def _blob(token, expires):
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "r-" + token,
                                         "expiresAt": expires}})


def test_plan_never_offers_a_snapshot_older_than_the_last_home_copy(ctx):
    setup(ctx)
    item = ctx.claude_cred("a@test")
    ctx.snapshot_set("claude", "a@test", _blob("old", 100))
    item.keychain.delete(item.service, item.account)
    tracker = claudesync.tracker(ctx)
    tracker.seen["a@test"] = claudesync.Seen(_blob("rotated", 200), "a@test")
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test") is None
    tracker.seen["a@test"] = claudesync.Seen(_blob("older", 50), "a@test")
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test")[:2] == ("restore", _blob("old", 100))


def test_plan_after_claude_wiped_the_item_needs_a_strictly_newer_snapshot(ctx):
    setup(ctx)
    item = ctx.claude_cred("a@test")
    item.set_live(json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "",
                                                "expiresAt": 0}}))
    tracker = claudesync.tracker(ctx)
    ctx.snapshot_set("claude", "a@test", _blob("same", 100))
    tracker.seen["a@test"] = claudesync.Seen(_blob("same", 100), "a@test")   # the bytes it wiped
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test") is None
    ctx.snapshot_set("claude", "a@test", _blob("newer", 300))
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test")[0] == "restore"


def test_plan_prefers_the_usable_home_item_and_refuses_quarantined_seats(ctx):
    setup(ctx)
    home_blob = ctx.claude_cred("a@test").get_live()
    ctx.snapshot_set("claude", "a@test", _blob("snap", 10 ** 12))
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test")[:2] == ("intact", home_blob)
    (ctx.claude_home("a@test") / ".acctsw-quarantined").write_text("1")
    assert L.plan_claude_login(ctx, ctx.load_state(), "a@test") is None


def test_apply_never_overwrites_a_login_that_landed_meanwhile(ctx):
    setup(ctx)
    item = ctx.claude_cred("a@test")
    item.keychain.delete(item.service, item.account)
    with ctx.locked():
        plan = L.plan_claude_login(ctx, ctx.load_state(), "a@test")
    fresh = _blob("fresh-login", 500)
    item.set_live(fresh)   # the user ran /login in the child before we wrote
    with ctx.locked():
        assert L.apply_claude_login(ctx, "a@test", *plan)
    assert item.get_live() == fresh
