"""Gaps the mutation sweep found in bridge, usage and accounts: each test pins a behaviour a mutant
of the sign-in classifier, the dead-credential backoff, the notices or the crossing actions broke
without any other test noticing."""
import json
from datetime import timedelta

import pytest

from acctsw import accounts, bridge, claudesync, seatkind
from acctsw import usage as U
from acctsw.usage import SignIn, Usage
from acctsw.util import iso, now
from tests.conftest import make_claude_blob, make_codex_blob
from tests.test_auto_handoff import probe, seed
from tests.test_signin import (BLANKED, LIVE_REFRESH, _age_attempt, _claude, _counting, _flag, _ms,
                               _seed_claude, _sign_out, _lock_keychain)
from tests.test_usage import claude_ok_body


def _add(ctx, email):
    ctx.cred["codex"].set_live(make_codex_blob(email))
    accounts.add(ctx, ctx.load_state(), "codex", email=email)


# --- sign-in classifier -----------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (0, None), (1, 1.0), (0.5, 0.5), (float("inf"), None), (-3, None), (True, None), ("5", None),
    (None, None)])
def test_epoch_ms_accepts_only_positive_finite_numbers(value, expected):
    assert U._epoch_ms(value) == expected


def test_login_usable_answers_a_real_false_for_every_unusable_shape():
    for tool, blob in (("claude", None), ("claude", "not json"), ("claude", "[]"),
                       ("claude", json.dumps({"claudeAiOauth": "x"})),
                       ("claude", json.dumps({})), ("codex", None), ("codex", "[]")):
        assert U.login_usable(tool, blob) is False, (tool, blob)


def test_only_claude_blobs_state_a_refresh_expiry():
    blob = _claude(refresh_expires=_ms(timedelta(days=3)))
    assert U.signin_expires_at("claude", blob) is not None
    assert U.signin_expires_at("codex", blob) is None


def test_signin_verdict_is_immutable():
    verdict = SignIn(False)
    with pytest.raises(Exception):
        verdict.signed_out = True


def test_classify_signin_judges_the_login_at_the_clock_it_was_given():
    past = now() - timedelta(days=2)
    blob = _claude(access="t", refresh="", expires=_ms(-timedelta(days=1)))
    assert U.classify_signin("claude", [blob], past).signed_out is False
    assert U.classify_signin("claude", [blob]).signed_out is True


def test_claude_login_present_and_wiped_answer_a_real_false():
    assert U.claude_login_present(None) is False
    assert U.claude_login_present(json.dumps({"claudeAiOauth": "x"})) is False
    assert U.claude_login_wiped(None) is False
    assert U.claude_login_wiped("garbage") is False


def _with_expiry(ms, access="a"):
    return _claude(access=access, refresh="r", expires=ms)


def test_launch_login_offers_nothing_when_the_snapshot_is_no_login_either():
    assert U.claude_launch_login(None, lambda: None, None) is None
    assert U.claude_launch_login(None, lambda: "{}", None) is None
    assert U.claude_launch_login(BLANKED, lambda: BLANKED, None) is None


def test_launch_login_compares_the_snapshot_to_the_last_seen_login_by_expiry():
    older, same, newer = (_with_expiry(1000.0, "o"), _with_expiry(2000.0, "s"),
                          _with_expiry(3000.0, "n"))
    last = _with_expiry(2000.0, "last")
    assert claudesync.expiry(last) == claudesync.expiry(same)
    # not wiped (item simply absent): an equal expiry is not older, so it may be installed
    assert U.claude_launch_login(None, lambda: same, last) == ("snapshot", same)
    assert U.claude_launch_login(None, lambda: newer, last) == ("snapshot", newer)
    assert U.claude_launch_login(None, lambda: older, last) is None
    # Claude wiped the item: the snapshot must be strictly newer than what it just proved dead
    wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}})
    assert U.claude_launch_login(wiped, lambda: same, last) is None
    assert U.claude_launch_login(wiped, lambda: newer, last) == ("snapshot", newer)
    # an expiry that cannot be read proves nothing fresher
    unstated = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}})
    assert U.claude_launch_login(None, lambda: unstated, last) is None


def test_signin_for_classifies_at_the_given_clock(ctx):
    _seed_claude(ctx)
    ctx.snapshot_set("claude", "a@test", BLANKED)
    blob = _claude(access="t", refresh="r", expires=_ms(-timedelta(days=1)),
                   refresh_expires=_ms(-timedelta(days=1)))
    verdict, _digest, login = U._signin_for(ctx, "claude", "a@test", blob,
                                            now() - timedelta(days=2))
    assert verdict.signed_out is False and login == blob


def test_a_quarantined_claude_home_is_signed_out_whatever_its_bytes(ctx):
    _seed_claude(ctx)
    home = ctx.claude_home("a@test")
    home.mkdir(parents=True, exist_ok=True)
    (home / ".acctsw-quarantined").write_text("")
    blob = _claude(access="t", refresh="r")
    assert U._signin_for(ctx, "claude", "a@test", blob) == (
        SignIn(True), U._credential_digest(blob), None)


def test_a_codex_seat_is_not_judged_by_a_claude_home_that_happens_to_exist(ctx):
    _add(ctx, "a@x.com")
    home = ctx.claude_home("a@x.com")
    home.mkdir(parents=True, exist_ok=True)
    (home / ".acctsw-quarantined").write_text("")
    blob = make_codex_blob("a@x.com")
    assert U._signin_for(ctx, "codex", "a@x.com", blob) == (
        SignIn(False), U._credential_digest(blob), blob)


def test_a_usable_blob_is_digested_alone_not_with_its_snapshot(ctx):
    _seed_claude(ctx)
    ctx.snapshot_set("claude", "a@test", LIVE_REFRESH)
    blob = _claude(access="t", refresh="r")
    _verdict, digest, login = U._signin_for(ctx, "claude", "a@test", blob)
    assert digest == U._credential_digest(blob) and login == blob


# --- dead-credential backoff ------------------------------------------------------------------

def test_dead_unchanged_is_exactly_bounded_by_max_backoff():
    at = now()

    def prev(age, **extra):
        return {"error": "no_token", "dead_credential_digest": "d",
                "last_attempted_at": iso(at - timedelta(seconds=age)), **extra}
    assert U._dead_unchanged(prev(U.MAX_BACKOFF_SECONDS - 1), "d", at) is True
    assert U._dead_unchanged(prev(U.MAX_BACKOFF_SECONDS), "d", at) is False
    assert U._dead_unchanged(prev(10), "other", at) is False
    assert U._dead_unchanged(prev(10), None, at) is False
    assert U._dead_unchanged({"error": "network", "dead_credential_digest": "d",
                              "last_attempted_at": iso(at)}, "d", at) is False
    assert U._dead_unchanged({"error": "no_token", "dead_credential_digest": "d"}, "d", at) is False
    assert U._dead_unchanged(None, "d", at) is False


def test_forced_refresh_retries_a_dead_credential_that_has_not_changed(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    summary = U.refresh(ctx, ctx.load_state(), "claude", only="a@test", force=True,
                        get=_counting([]), user_agent="test")
    assert summary["claude"]["a@test"] == "no_token"
    _age_attempt(ctx, "a@test", U.USAGE_MIN_REFRESH_SECONDS * 4)
    summary = U.refresh(ctx, ctx.load_state(), "claude", only="a@test",
                        get=_counting([]), user_agent="test")
    assert summary["claude"]["a@test"] == "cached"


def test_a_credential_read_that_fails_during_the_commit_is_reported_unreadable(ctx, monkeypatch):
    _seed_claude(ctx)

    def get(url, headers, timeout):
        _lock_keychain(ctx, monkeypatch)   # the keychain locks while the request is in flight
        return 200, claude_ok_body()
    summary = U.refresh_live(ctx, "claude", only="a@test", force=True, get=get, user_agent="test")
    assert summary["claude"]["a@test"] == "unreadable"


def test_a_successful_detached_poll_binds_the_authenticated_credential(ctx):
    _seed_claude(ctx)
    summary = U.refresh_live(ctx, "claude", only="a@test", force=True, get=_counting([]),
                             user_agent="test")
    assert summary["claude"]["a@test"] == "ok"
    digest = ctx.load_state().get_seat("claude", "a@test")["usage"]["credential_digest"]
    assert isinstance(digest, str) and len(digest) == 64


def test_error_streak_counts_consecutive_transient_failures(ctx):
    _add(ctx, "a@x.com")
    state = ctx.load_state()
    streaks = []
    for _ in range(3):
        U.store_fetch(state, "codex", "a@x.com", Usage(error="network", fetched_at=iso(now())))
        streaks.append(state.get_seat("codex", "a@x.com")["usage"]["error_streak"])
    assert streaks == [1, 2, 3]


# --- notices ----------------------------------------------------------------------------------

def test_notice_titles_name_the_seat_or_fall_back_to_its_address_local_part(ctx):
    state = _seed_claude(ctx)
    state.get_seat("claude", "a@test")["name"] = None
    _flag(state, "a@test", True)
    assert accounts.take_signin_notices(state)[0][0]["title"].startswith("a needs you")
    state.get_seat("claude", "b@test")["name"] = "Work"
    _flag(state, "b@test", True)
    assert accounts.take_signin_notices(state)[0][0]["title"].startswith("Work needs you")


def test_bridge_notices_with_nothing_new_take_no_lock_and_return_a_list(ctx, monkeypatch):
    _seed_claude(ctx)

    def locked():
        raise AssertionError("the quiet path must not take the state lock")
    monkeypatch.setattr(ctx, "locked", locked)
    assert bridge._take_signin_notices(ctx) == []


def test_bridge_notices_are_returned_and_their_marks_persist(ctx):
    state = _seed_claude(ctx)
    _flag(state, "a@test", True)
    state.save()
    assert len(bridge._take_signin_notices(ctx)) == 1
    assert ctx.load_state().get_seat("claude", "a@test")["signin_notified"]["signed_out"] is True
    assert bridge._take_signin_notices(ctx) == []


# --- crossings --------------------------------------------------------------------------------

def _two_plans(ctx, first="team", second="plus"):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    state = ctx.load_state()
    state.get_seat("codex", "a@x.com")["plan"] = first
    state.get_seat("codex", "b@x.com")["plan"] = second
    state.save()


def _approvals(ctx):
    return ctx.load_state().data["tools"]["codex"].get("crossings", {}).get("approvals", {})


@pytest.mark.crossings
def test_pending_crossings_carry_names_and_kinds(ctx):
    _two_plans(ctx)
    state = ctx.load_state()
    state.get_seat("codex", "a@x.com")["name"] = "Work"
    state.get_seat("codex", "b@x.com")["name"] = None
    seatkind.request(state, "codex", "a@x.com", "b@x.com", "s1")
    state.save()
    pending = bridge.snapshot_state(ctx)["pending_crossings"]
    assert len(pending) == 1
    assert {k: pending[0][k] for k in ("tool", "from", "to", "from_name", "to_name",
                                       "from_kind", "to_kind")} == {
        "tool": "codex", "from": "a@x.com", "to": "b@x.com", "from_name": "Work",
        "to_name": "b@x.com", "from_kind": "business", "to_kind": "personal"}


@pytest.mark.crossings
def test_pending_crossings_name_a_seat_that_is_gone_by_its_address(ctx):
    _two_plans(ctx)
    state = ctx.load_state()
    seatkind.request(state, "codex", "a@x.com", "b@x.com", "s1")
    state.save()
    state = ctx.load_state()
    state.data["tools"]["codex"]["accounts"].pop("b@x.com")
    state.save()
    pending = bridge._pending_crossings(ctx.load_state(), now())
    assert pending[0]["to_name"] == "b@x.com" and pending[0]["to_kind"] == "unknown"


def _cross(ctx, action, **extra):
    return bridge.handle(ctx, {"action": action, "tool": "codex", "from": "a@x.com",
                               "to": "b@x.com", **extra})


@pytest.mark.crossings
@pytest.mark.parametrize("scope", ["once", "always"])
def test_approve_crossing_persists_the_approval_and_answers_the_question(ctx, scope):
    _two_plans(ctx)
    state = ctx.load_state()
    seatkind.request(state, "codex", "a@x.com", "b@x.com", "s1")
    state.save()
    result = _cross(ctx, "approve_crossing", scope=scope)
    assert result["ok"] is True and result["state"]["pending_crossings"] == []
    assert _approvals(ctx)["a@x.com>b@x.com"]["scope"] == scope
    assert seatkind.approved(ctx.load_state(), "codex", "a@x.com", "b@x.com")
    assert not seatkind.approved(ctx.load_state(), "codex", "b@x.com", "a@x.com")


@pytest.mark.crossings
def test_approve_crossing_defaults_to_once(ctx):
    _two_plans(ctx)
    assert _cross(ctx, "approve_crossing")["ok"] is True
    assert _approvals(ctx)["a@x.com>b@x.com"]["scope"] == "once"


@pytest.mark.crossings
def test_deny_crossing_revokes_an_approval_and_never_grants(ctx):
    _two_plans(ctx)
    assert _cross(ctx, "approve_crossing", scope="always")["ok"] is True
    result = _cross(ctx, "deny_crossing")
    assert result["ok"] is True and "state" in result
    assert "a@x.com>b@x.com" not in _approvals(ctx)
    assert _cross(ctx, "deny_crossing")["ok"] is True
    assert _approvals(ctx) == {}


@pytest.mark.crossings
@pytest.mark.parametrize("message", [
    {"tool": "nope", "from": "a@x.com", "to": "b@x.com"},
    {"tool": "codex", "from": 5, "to": "b@x.com"},
    {"tool": "codex", "from": "a@x.com", "to": None},
    {"tool": "codex", "from": "a@x.com", "to": "a@x.com"},
])
@pytest.mark.parametrize("action", ["approve_crossing", "deny_crossing"])
def test_malformed_crossing_requests_are_refused_without_touching_state(ctx, action, message):
    _two_plans(ctx)
    rev = ctx.load_state().data["rev"]
    assert bridge.handle(ctx, {"action": action, **message}) == {
        "ok": False, "error": "bad crossing request"}
    assert ctx.load_state().data["rev"] == rev and _approvals(ctx) == {}


@pytest.mark.crossings
def test_approve_crossing_refuses_an_unknown_scope_but_deny_ignores_scope(ctx):
    _two_plans(ctx)
    assert _cross(ctx, "approve_crossing", scope="forever") == {
        "ok": False, "error": "bad crossing scope: forever"}
    assert _approvals(ctx) == {}
    assert _cross(ctx, "deny_crossing", scope="forever")["ok"] is True


@pytest.mark.crossings
@pytest.mark.parametrize("gone", ["from", "to"])
@pytest.mark.parametrize("action", ["approve_crossing", "deny_crossing"])
def test_crossing_actions_refuse_a_seat_that_left_the_list(ctx, action, gone):
    _two_plans(ctx)
    message = {"action": action, "tool": "codex", "from": "a@x.com", "to": "b@x.com"}
    message[gone] = "ghost@x.com"
    rev = ctx.load_state().data["rev"]
    result = bridge.handle(ctx, message)
    assert result["ok"] is False and result["error"] == "that seat is no longer on the list"
    assert "state" in result and ctx.load_state().data["rev"] == rev


@pytest.mark.crossings
def test_an_automatic_handoff_across_kinds_spends_its_once_approval(ctx, monkeypatch):
    summary = seed(ctx, spare_count=1)
    probe(monkeypatch)
    state = ctx.load_state()
    seatkind.approve(state, "codex", "seat0@test.example", "seat1@test.example", "once")
    state.save()
    result = bridge._auto_switch_after_usage(ctx, summary)
    assert result and result["status"] == "switched" and result["to"] == "seat1@test.example"
    assert _approvals(ctx) == {}
