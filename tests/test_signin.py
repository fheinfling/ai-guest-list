"""A seat whose login is gone everywhere the app could use it is SIGNED OUT, not a flaky poll.

When a Claude refresh token reaches ``refreshTokenExpiresAt``, Claude blanks the seat's item
(empty tokens, ``expiresAt`` 0) and our snapshot's refresh token is past its expiry too. Retrying
the usage endpoint cannot fix that, only a new login can: the seat must say so (needs_login), stay
out of every switch, be re-read only when its credential bytes change, and be announced once.
"""
import json
from datetime import timedelta

import pytest

from acctsw import accounts, bridge, claudesync, launcher, selection
from acctsw import usage as U
from acctsw.keychain import KeychainError, SecurityKeychain
from acctsw.util import iso, now, parse_iso
from tests.conftest import make_claude_blob, make_codex_blob
from tests.test_usage import claude_ok_body, codex_ok_body

BLANKED = json.dumps({"claudeAiOauth": {
    "accessToken": "", "refreshToken": "", "expiresAt": 0, "scopes": ["user:inference"],
    "subscriptionType": "max", "refreshTokenExpiresAt": 1}})


def _ms(delta):
    return (now() + delta).timestamp() * 1000


def _claude(access="x", refresh="y", expires=None, refresh_expires=None):
    value = json.loads(make_claude_blob())
    value["claudeAiOauth"].update(accessToken=access, refreshToken=refresh,
                                  expiresAt=0 if expires is None else expires)
    if refresh_expires is not None:
        value["claudeAiOauth"]["refreshTokenExpiresAt"] = refresh_expires
    return json.dumps(value)


EXPIRED_SNAPSHOT = _claude(access="old", refresh="old-r", expires=_ms(-timedelta(days=1)),
                           refresh_expires=_ms(-timedelta(hours=1)))
LIVE_REFRESH = _claude(access="old", refresh="live-r", expires=_ms(-timedelta(hours=1)),
                       refresh_expires=_ms(timedelta(days=20)))


def _seed_claude(ctx, emails=("a@test", "b@test")):
    """Two Claude seats; the LAST one added is active."""
    for email in emails:
        ctx.cred["claude"].set_live(make_claude_blob().replace('"x"', json.dumps(email)))
        accounts.add(ctx, ctx.load_state(), "claude", email=email)
    return ctx.load_state()


def _sign_out(ctx, email, snapshot=EXPIRED_SNAPSHOT, home=BLANKED):
    item = ctx.claude_cred(email)
    if home is None:
        item.keychain.delete(item.service, item.account)   # another tool removed the item
    else:
        item.set_live(home)
    ctx.snapshot_set("claude", email, snapshot)


def _lock_keychain(ctx, monkeypatch, reads=lambda service: True):
    """Reads of the chosen services fail the way the real backend fails on a locked keychain: the
    real ``SecurityKeychain`` code runs a ``security`` that exits non-zero (not 44, not found)."""
    real = ctx.keychain.get_raw
    locked = SecurityKeychain("/usr/bin/false")
    monkeypatch.setattr(ctx.keychain, "get_raw", lambda service, account: (
        locked.get_raw(service, account) if reads(service) else real(service, account)))


def _counting(calls, body=None):
    def get(url, headers, timeout):
        calls.append(headers.get("Authorization"))
        return 200, body or claude_ok_body()
    return get


def _view(ctx, tool, email):
    return next(s for s in accounts.list_seats(ctx.load_state(), tool) if s["email"] == email)


# --- the classifier ---------------------------------------------------------------------------

def test_blanked_item_and_expired_snapshot_are_signed_out():
    assert U.classify_signin("claude", [BLANKED, EXPIRED_SNAPSHOT]) == U.SignIn(True, None)


def test_blanked_item_with_a_live_refresh_snapshot_is_not_signed_out():
    verdict = U.classify_signin("claude", [BLANKED, LIVE_REFRESH])
    assert verdict.signed_out is False
    assert parse_iso(verdict.expires_at) > now() + timedelta(days=19)


def test_expired_access_token_with_a_live_refresh_token_is_routine():
    """Claude renews it on its next request: that is not a logout (see _claude_access_token_expired)."""
    assert U.login_usable("claude", LIVE_REFRESH)
    no_stated_expiry = _claude(access="", refresh="r", expires=0)
    assert U.login_usable("claude", no_stated_expiry)


def test_live_access_token_carries_a_login_whose_refresh_already_ran_out():
    blob = _claude(access="a", refresh="r", expires=_ms(timedelta(hours=2)),
                   refresh_expires=_ms(-timedelta(minutes=1)))
    assert U.login_usable("claude", blob)


@pytest.mark.parametrize("blob", [
    None, "", "not json", "[]", json.dumps({"other": {}}),
    json.dumps({"claudeAiOauth": None}),
    _claude(access="", refresh=""),
    _claude(access="", refresh=None),
    _claude(access="", refresh="r", refresh_expires=_ms(-timedelta(seconds=1))),
    _claude(access="a", refresh="", expires=_ms(-timedelta(seconds=1))),
])
def test_claude_blobs_without_a_usable_login(blob):
    assert not U.login_usable("claude", blob)
    assert U.classify_signin("claude", [blob]).signed_out


def test_refresh_expiry_exactly_now_is_expired():
    at = now()
    blob = _claude(access="", refresh="r", refresh_expires=at.timestamp() * 1000)
    assert not U.login_usable("claude", blob, at)
    assert U.login_usable("claude", blob, at - timedelta(milliseconds=1))


def test_codex_without_any_token_is_signed_out_and_with_either_is_not():
    empty = json.dumps({"auth_mode": "ChatGPT", "tokens": {"id_token": "h.e.s", "access_token": "",
                                                          "refresh_token": ""}})
    assert U.classify_signin("codex", [empty]).signed_out
    assert U.classify_signin("codex", [json.dumps({"tokens": None})]).signed_out
    assert not U.classify_signin("codex", [make_codex_blob("a@x.com")]).signed_out
    refresh_only = json.loads(make_codex_blob("a@x.com"))
    refresh_only["tokens"]["access_token"] = ""
    assert not U.classify_signin("codex", [json.dumps(refresh_only)]).signed_out
    access_only = json.loads(make_codex_blob("a@x.com"))
    access_only["tokens"]["refresh_token"] = ""
    assert not U.classify_signin("codex", [json.dumps(access_only)]).signed_out


def test_codex_states_no_refresh_expiry():
    assert U.classify_signin("codex", [make_codex_blob("a@x.com")]).expires_at is None


def test_expiry_comes_from_the_first_usable_credential_in_precedence_order():
    home = _claude(refresh_expires=_ms(timedelta(days=2)))
    verdict = U.classify_signin("claude", [home, LIVE_REFRESH])
    assert parse_iso(verdict.expires_at) < now() + timedelta(days=3)


def test_snapshot_is_consulted_only_when_the_home_copy_is_dead(ctx):
    _seed_claude(ctx, ["a@test"])
    reads = []
    original = ctx.snapshot_get
    ctx.snapshot_get = lambda tool, email: reads.append(email) or original(tool, email)
    verdict, _, login = U._signin_for(ctx, "claude", "a@test", LIVE_REFRESH)
    assert verdict.signed_out is False and reads == [] and login == LIVE_REFRESH
    ctx.snapshot_set("claude", "a@test", EXPIRED_SNAPSHOT)
    verdict, _, login = U._signin_for(ctx, "claude", "a@test", BLANKED)
    assert verdict.signed_out is True and reads == ["a@test"] and login is None


def test_outside_a_claude_home_a_usable_snapshot_backs_a_dead_blob_and_is_what_gets_sent(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    accounts.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    verdict, _, login = U._signin_for(ctx, "codex", "a@x.com", json.dumps({"tokens": {}}))
    assert verdict.signed_out is False
    assert login == ctx.snapshot_get("codex", "a@x.com")


def _verdicts(ctx, email):
    seat = ctx.load_state().get_seat("claude", email)
    return seat.get("signin"), seat.get("usage")


def test_an_unreadable_snapshot_is_no_proof_of_logout(ctx, monkeypatch):
    """The home item is gone, so the verdict rests on the snapshot, and the keychain refuses to
    read it: every poll path keeps the stored verdict, sends nothing and announces nothing."""
    _seed_claude(ctx)
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    _sign_out(ctx, "a@test", home=None, snapshot=LIVE_REFRESH)
    _lock_keychain(ctx, monkeypatch, reads=lambda service: service == ctx.keychain_service)
    with pytest.raises(KeychainError):
        U._signin_for(ctx, "claude", "a@test", None)
    before = _verdicts(ctx, "a@test")
    assert before[0]["signed_out"] is False
    calls = []
    summary = U.refresh_live(ctx, "claude", only="a@test", force=True, get=_counting(calls),
                             user_agent="test")
    assert summary["claude"]["a@test"] == "unreadable"
    state = ctx.load_state()
    assert U.refresh(ctx, state, "claude", only="a@test", force=True, get=_counting(calls),
                     user_agent="test")["claude"]["a@test"] == "unreadable"
    launcher._verify_capacity(ctx, "claude", _counting(calls), at=now(), force=True,
                              exclude={"b@test"}, ua="test")
    assert calls == [] and _verdicts(ctx, "a@test") == before
    assert accounts.take_signin_notices(ctx.load_state())[0] == []


def test_a_locked_keychain_signs_nobody_out(ctx, monkeypatch):
    """Every credential read fails: no seat is announced signed out, the door is not shut."""
    _seed_claude(ctx)
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    before = {e: _verdicts(ctx, e) for e in ("a@test", "b@test")}
    _lock_keychain(ctx, monkeypatch)
    calls = []
    summary = U.refresh_live(ctx, "claude", force=True, get=_counting(calls), user_agent="test")
    assert summary["claude"] == {"a@test": "unreadable", "b@test": "unreadable"}
    sel = launcher._verify_capacity(ctx, "claude", _counting(calls), at=now(), force=True,
                                    ua="test")
    assert calls == [] and sel.email is not None
    assert {e: _verdicts(ctx, e) for e in before} == before
    assert accounts.take_signin_notices(ctx.load_state())[0] == []


# --- polling ----------------------------------------------------------------------------------

def test_signed_out_seat_is_recorded_without_any_request_or_retry_streak(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    calls = []
    summary = U.refresh_live(ctx, "claude", get=_counting(calls), user_agent="test")
    assert summary["claude"]["a@test"] == "no_token"
    assert calls == ["Bearer b@test"]   # only the healthy seat reached the network
    seat = ctx.load_state().get_seat("claude", "a@test")
    assert seat["signin"] == {"signed_out": True, "expires_at": None}
    assert seat["usage"]["error_streak"] == 0
    assert seat["usage"]["dead_credential_digest"]


def test_a_signed_out_job_never_reaches_the_network_whatever_it_carries():
    job = U.UsageFetchJob(tool="claude", email="a@test", blob=LIVE_REFRESH, credential_digest="",
                          added_at=None, active=False, user_agent="test", signin=U.SignIn(True))
    calls = []
    assert U._fetch_job(job, _counting(calls)).usage.error == "no_token" and calls == []


def test_dead_tokens_that_are_not_blank_never_reach_the_network(ctx):
    """A home item still holding tokens whose every expiry has passed: there is nothing to send."""
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=EXPIRED_SNAPSHOT)
    calls = []
    summary = U.refresh_live(ctx, "claude", only="a@test", get=_counting(calls), user_agent="test")
    assert summary["claude"]["a@test"] == "no_token" and calls == []
    assert _view(ctx, "claude", "a@test")["signed_out"] is True


GOOD = _claude(access="good", refresh="good-r", expires=_ms(timedelta(hours=1)),
               refresh_expires=_ms(timedelta(days=20)))
OLD_SHAPE = json.dumps({"claudeAiOauth": {"accessToken": "old", "refreshToken": "old-r",
                                          "expiresAt": _ms(timedelta(hours=1))}})
NEWER = _claude(access="newer", refresh="newer-r", expires=_ms(timedelta(hours=2)),
                refresh_expires=_ms(timedelta(days=20)))


@pytest.mark.parametrize("home,snapshot,last,restorable", [
    # Claude wiped the item early (invalid_grant): the snapshot is the very login it proved dead,
    # even though its refreshTokenExpiresAt is still ahead.
    (BLANKED, GOOD, GOOD, False),
    # Wiped, and an old-shape snapshot (no refresh expiry) this process never saw in the home.
    (BLANKED, OLD_SHAPE, None, False),
    # Wiped, but the snapshot is strictly newer than what Claude held there: it may go back.
    (BLANKED, NEWER, GOOD, True),
    # Another tool deleted the item: the seat's snapshot goes back.
    (None, GOOD, None, True),
], ids=["wiped-same-bytes", "wiped-old-shape", "wiped-newer-snapshot", "item-deleted"])
def test_signed_in_means_a_launch_would_get_a_login(ctx, home, snapshot, last, restorable):
    """The seat's verdict is plan_claude_login's decision: signed in exactly when the launcher would
    put a login back, and then the usage fetch sends that login, not the dead home item."""
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", snapshot=snapshot, home=home)
    claudesync.tracker(ctx).seen.pop("a@test", None)
    if last is not None:
        claudesync.tracker(ctx).seen["a@test"] = claudesync.Seen(last, "a@test")
    with ctx.locked():
        plan = launcher.plan_claude_login(ctx, ctx.load_state(), "a@test")
    assert (plan is not None) is restorable
    calls = []
    U.refresh_live(ctx, "claude", only="a@test", get=_counting(calls), user_agent="test")
    view = _view(ctx, "claude", "a@test")
    assert view["signed_out"] is (not restorable)
    chosen = selection.choose(ctx.load_state(), "claude", exclude={"b@test"}).email
    if restorable:
        token = json.loads(snapshot)["claudeAiOauth"]["accessToken"]
        assert calls == [f"Bearer {token}"] and plan[:2] == ("restore", snapshot)
        assert chosen == "a@test"
    else:
        assert calls == [] and chosen is None


def _age_attempt(ctx, email, seconds, tool="claude"):
    state = ctx.load_state()
    usage = state.get_seat(tool, email)["usage"]
    usage["last_attempted_at"] = iso(now() - timedelta(seconds=seconds))
    state.save()
    return usage["last_attempted_at"]


def test_same_dead_bytes_are_not_polled_again_until_max_backoff(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    attempted = _age_attempt(ctx, "a@test", U.USAGE_MIN_REFRESH_SECONDS * 4)
    rev = ctx.load_state().data["rev"]
    for force in (False, True):
        summary = U.refresh_live(ctx, "claude", only="a@test", force=force,
                                 get=_counting([]), user_agent="test")
        assert summary["claude"]["a@test"] == "cached"
    state = ctx.load_state()
    assert state.data["rev"] == rev   # not even a state write
    assert state.get_seat("claude", "a@test")["usage"]["last_attempted_at"] == attempted

    _age_attempt(ctx, "a@test", U.MAX_BACKOFF_SECONDS)
    summary = U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    assert summary["claude"]["a@test"] == "no_token"
    assert ctx.load_state().get_seat("claude", "a@test")["usage"]["error_streak"] == 0


def test_a_fresh_login_resumes_polling_at_once(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    _age_attempt(ctx, "a@test", U.USAGE_MIN_REFRESH_SECONDS)
    # Claude itself (a /login in the seat's home) writes a new item; no engine hook runs.
    ctx.claude_cred("a@test").set_live(_claude(access="fresh", refresh="fresh-r",
                                               refresh_expires=_ms(timedelta(days=30))))
    calls = []
    summary = U.refresh_live(ctx, "claude", only="a@test", get=_counting(calls), user_agent="test")
    assert summary["claude"]["a@test"] == "ok" and calls == ["Bearer fresh"]
    view = _view(ctx, "claude", "a@test")
    assert view["signed_out"] is False and view["needs_login"] is False


def test_a_changed_snapshot_alone_also_resumes(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=None)
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    _age_attempt(ctx, "a@test", U.USAGE_MIN_REFRESH_SECONDS)
    ctx.snapshot_set("claude", "a@test", LIVE_REFRESH)
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    assert ctx.load_state().get_seat("claude", "a@test")["signin"]["signed_out"] is False


def test_legacy_no_token_streak_backs_off_at_the_base_cadence_only():
    legacy = {"error": "no_token", "error_streak": 57}
    assert U._backoff_seconds(legacy, U.USAGE_MIN_REFRESH_SECONDS) == U.USAGE_MIN_REFRESH_SECONDS
    assert U._backoff_seconds({"error": "network", "error_streak": 57},
                              U.USAGE_MIN_REFRESH_SECONDS) == U.MAX_BACKOFF_SECONDS


def test_transient_errors_still_grow_their_streak(ctx):
    _seed_claude(ctx, ["a@test"])
    U.refresh_live(ctx, "claude", get=lambda *_: (0, ""), user_agent="test")
    usage = ctx.load_state().get_seat("claude", "a@test")["usage"]
    assert usage["error"] == "network" and usage["error_streak"] == 1
    assert "dead_credential_digest" not in usage


def test_launcher_refresh_classifies_and_gates_the_same_way(ctx):
    state = _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=EXPIRED_SNAPSHOT)
    calls = []
    summary = U.refresh(ctx, state, "claude", only="a@test", force=True,
                        get=_counting(calls), user_agent="test")
    assert summary["claude"]["a@test"] == "no_token" and calls == []
    assert state.get_seat("claude", "a@test")["signin"]["signed_out"] is True
    state.get_seat("claude", "a@test")["usage"]["last_attempted_at"] = iso(
        now() - timedelta(seconds=U.USAGE_MIN_REFRESH_SECONDS * 4))
    summary = U.refresh(ctx, state, "claude", only="a@test", get=_counting(calls),
                        user_agent="test")
    assert summary["claude"]["a@test"] == "cached"


def test_launcher_capacity_check_classifies_and_gates_the_same_way(ctx):
    """The launcher's wait/watcher sweep must not send dead tokens or re-read identical dead bytes."""
    _seed_claude(ctx)   # b@test active
    _sign_out(ctx, "a@test", home=EXPIRED_SNAPSHOT)
    calls = []
    sel = launcher._verify_capacity(ctx, "claude", _counting(calls), at=now(), force=True,
                                    exclude={"b@test"}, ua="test")
    assert sel.email is None and calls == []
    seat = ctx.load_state().get_seat("claude", "a@test")
    assert seat["signin"]["signed_out"] is True and seat["usage"]["error_streak"] == 0
    attempted = _age_attempt(ctx, "a@test", launcher.POLL_MIN_FETCH_S * 4)
    rev = ctx.load_state().data["rev"]
    launcher._verify_capacity(ctx, "claude", _counting(calls), at=now(), force=False,
                              exclude={"b@test"}, ua="test")
    state = ctx.load_state()
    assert state.data["rev"] == rev
    assert state.get_seat("claude", "a@test")["usage"]["last_attempted_at"] == attempted
    ctx.claude_cred("a@test").set_live(_claude(access="fresh", refresh="fresh-r"))
    sel = launcher._verify_capacity(ctx, "claude", _counting(calls), at=now(), force=False,
                                    exclude={"b@test"}, ua="test")
    assert calls == ["Bearer fresh"] and sel.email == "a@test" and sel.available


def test_launcher_capacity_check_drops_a_verdict_whose_snapshot_changed_mid_fetch(ctx):
    """The verdict rested on the snapshot (home item gone). If the snapshot changes while the
    request runs, the result describes credentials the seat no longer holds: nothing is stored."""
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=None, snapshot=GOOD)
    claudesync.tracker(ctx).seen.pop("a@test", None)
    before = _verdicts(ctx, "a@test")

    def get(url, headers, timeout):
        ctx.snapshot_set("claude", "a@test", EXPIRED_SNAPSHOT)   # e.g. a sibling's write lands
        return 200, claude_ok_body()
    launcher._verify_capacity(ctx, "claude", get, at=now(), force=True,
                              exclude={"b@test"}, ua="test")
    assert _verdicts(ctx, "a@test") == before


def test_launcher_capacity_check_reads_credentials_outside_the_state_lock(ctx, monkeypatch):
    """Keychain reads are subprocesses: classifying the seats must not hold the cross-process
    flock that the app's poll and every other launcher wait on."""
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=None, snapshot=GOOD)
    held = []
    real = ctx.keychain.get_raw

    def spy(service, account):
        held.append(ctx._store_lock_held())
        return real(service, account)
    monkeypatch.setattr(ctx.keychain, "get_raw", spy)
    first_fetch = []

    def get(url, headers, timeout):
        first_fetch.append(len(held))
        return 200, claude_ok_body()
    launcher._verify_capacity(ctx, "claude", get, at=now(), force=True, ua="test")
    classify = held[:first_fetch[0]]
    assert classify and not any(classify)


def test_app_poll_drops_a_verdict_whose_snapshot_changed_mid_fetch(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test", home=None, snapshot=GOOD)
    claudesync.tracker(ctx).seen.pop("a@test", None)
    before = _verdicts(ctx, "a@test")

    def get(url, headers, timeout):
        ctx.snapshot_set("claude", "a@test", EXPIRED_SNAPSHOT)
        return 200, claude_ok_body()
    summary = U.refresh_live(ctx, "claude", only="a@test", get=get, user_agent="test")
    assert summary["claude"]["a@test"] == "stale" and _verdicts(ctx, "a@test") == before


def test_an_authenticated_answer_clears_a_stored_signed_out(ctx):
    """The launcher's in-session probe stores a fetch without a verdict of its own; a 200 there
    proves the login works, so the seat must stop asking for a sign-in."""
    state = _seed_claude(ctx)
    _flag(state, "a@test", True)
    u = U.fetch_claude("token", user_agent="test", get=lambda *_: (200, claude_ok_body()))
    assert U.store_fetch(state, "claude", "a@test", u, blob=GOOD) == "ok"
    signin = state.get_seat("claude", "a@test")["signin"]
    assert signin["signed_out"] is False
    assert parse_iso(signin["expires_at"]) > now() + timedelta(days=19)
    failed = U.Usage(error="network", fetched_at=iso(now()))
    _flag(state, "b@test", True)
    U.store_fetch(state, "claude", "b@test", failed, blob=GOOD)
    assert state.get_seat("claude", "b@test")["signin"]["signed_out"] is True


def test_codex_seat_with_its_login_removed_is_signed_out(ctx):
    for email in ("a@x.com", "b@x.com"):
        ctx.cred["codex"].set_live(make_codex_blob(email))
        accounts.add(ctx, ctx.load_state(), "codex", email=email)
    (ctx.codex_home("a@x.com") / "auth.json").unlink()   # `codex logout` in that home
    calls = []

    def get(url, headers, timeout):
        calls.append(url)
        return 200, codex_ok_body()
    summary = U.refresh_live(ctx, "codex", only="a@x.com", get=get)
    assert summary["codex"]["a@x.com"] == "no_token" and calls == []
    view = _view(ctx, "codex", "a@x.com")
    assert view["signed_out"] is True and view["needs_login"] is True
    assert view["signin_expires_at"] is None and view["signin_expiring"] is False


def test_re_adding_a_seat_clears_the_verdict_before_the_next_poll(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", only="a@test", get=_counting([]), user_agent="test")
    ctx.cred["claude"].set_live(_claude(access="new", refresh="new-r"))
    accounts.add(ctx, ctx.load_state(), "claude", email="a@test")
    assert _view(ctx, "claude", "a@test")["signed_out"] is False


# --- the seat view ----------------------------------------------------------------------------

@pytest.mark.parametrize("active", [True, False])
def test_signed_out_needs_login_on_active_and_parked_seats(ctx, active):
    _seed_claude(ctx)   # b@test is active
    email = "b@test" if active else "a@test"
    _sign_out(ctx, email)
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    view = _view(ctx, "claude", email)
    assert view["active"] is active
    assert view["signed_out"] is True and view["needs_login"] is True
    assert view["status"] == "needs-login"
    other = _view(ctx, "claude", "a@test" if active else "b@test")
    assert other["signed_out"] is False and other["needs_login"] is False


def test_payload_carries_the_signin_fields(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    seats = bridge.snapshot_state(ctx)["tools"]["claude"]["seats"]
    json.dumps(seats)
    by_email = {s["email"]: s for s in seats}
    assert by_email["a@test"]["signed_out"] is True and by_email["a@test"]["needs_login"] is True
    for seat in seats:
        assert {"signed_out", "signin_expires_at", "signin_expiring"} <= seat.keys()


def _expiring_view(left: timedelta):
    at = now()
    seat = {"email": "a@test", "signin": {"signed_out": False, "expires_at": iso(at + left)}}
    return accounts._seat_view(seat, active=False, at=at)


@pytest.mark.parametrize("left,expiring", [
    (timedelta(seconds=U.SIGNIN_WARN_S), True),                          # exactly 3 days
    (timedelta(seconds=U.SIGNIN_WARN_S - 1), True),                      # just under
    (timedelta(seconds=U.SIGNIN_WARN_S + 1), False),                     # just over
    (timedelta(seconds=1), True),
    (timedelta(0), False),                                               # expiring right now
    (-timedelta(seconds=1), False),                                      # already past
])
def test_signin_expiring_boundary(left, expiring):
    view = _expiring_view(left)
    assert view["signin_expiring"] is expiring
    assert parse_iso(view["signin_expires_at"]) is not None


def test_signin_window_is_three_days():
    assert U.SIGNIN_WARN_S == 3 * 86400


def test_signed_out_view_reports_no_expiry():
    at = now()
    seat = {"email": "a@test",
            "signin": {"signed_out": True, "expires_at": iso(at + timedelta(days=1))}}
    view = accounts._seat_view(seat, active=False, at=at)
    assert view["signin_expires_at"] is None and view["signin_expiring"] is False


def test_poll_stores_the_refresh_expiry_for_the_view(ctx):
    _seed_claude(ctx, ["a@test"])
    ctx.claude_cred("a@test").set_live(_claude(refresh_expires=_ms(timedelta(days=2))))
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    view = _view(ctx, "claude", "a@test")
    assert view["signin_expiring"] is True and view["needs_login"] is False
    assert view["signin_expires_at"].endswith("+00:00")


def test_seats_never_polled_have_neutral_signin_fields(ctx):
    _seed_claude(ctx, ["a@test"])
    view = _view(ctx, "claude", "a@test")
    assert (view["signed_out"], view["signin_expires_at"], view["signin_expiring"]) == (
        False, None, False)


# --- selection --------------------------------------------------------------------------------

def test_signed_out_seat_is_never_chosen(ctx):
    _seed_claude(ctx)   # b@test active
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    state = ctx.load_state()
    assert selection.choose(state, "claude", exclude={"b@test"}).email is None
    decision = launcher.handle_auth_dead(ctx, state, "claude")
    assert decision.action == "give_up" and decision.email != "a@test"


def test_signed_out_active_seat_still_hops_to_a_usable_one(ctx):
    _seed_claude(ctx)   # b@test active
    _sign_out(ctx, "b@test")
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    state = ctx.load_state()
    sel = selection.choose(state, "claude")
    assert sel.email == "a@test" and sel.available
    assert launcher.handle_auth_dead(ctx, state, "claude") == launcher.Decision("switch", "a@test")


def test_signed_out_seat_is_not_next_best_when_everything_else_rests(ctx):
    _seed_claude(ctx)
    _sign_out(ctx, "a@test")
    U.refresh_live(ctx, "claude", get=_counting([]), user_agent="test")
    state = ctx.load_state()
    state.set_limited_until("claude", "b@test", iso(now() + timedelta(hours=1)))
    sel = selection.choose(state, "claude")
    assert sel.email == "b@test" and sel.all_limited and not sel.available


# --- notifications ----------------------------------------------------------------------------

def _flag(state, email, signed_out, expires_at=None, tool="claude"):
    state.get_seat(tool, email)["signin"] = {"signed_out": signed_out, "expires_at": expires_at}


def test_signed_out_is_announced_once_and_again_after_a_recovery(ctx):
    state = _seed_claude(ctx)
    _flag(state, "a@test", True)
    notices, changed = accounts.take_signin_notices(state)
    assert changed and len(notices) == 1 and "a@test" in notices[0]["text"]
    assert accounts.take_signin_notices(state) == ([], False)
    state.save()
    assert accounts.take_signin_notices(ctx.load_state()) == ([], False)   # survives a restart
    _flag(state, "a@test", False)
    assert accounts.take_signin_notices(state) == ([], True)              # recovery re-arms
    _flag(state, "a@test", True)
    assert len(accounts.take_signin_notices(state)[0]) == 1


def test_expiring_is_announced_once_until_the_seat_leaves_the_window(ctx):
    state = _seed_claude(ctx)
    soon = iso(now() + timedelta(days=1))
    _flag(state, "a@test", False, soon)
    notices, _ = accounts.take_signin_notices(state)
    assert len(notices) == 1 and "a@test" in notices[0]["text"]
    assert accounts.take_signin_notices(state) == ([], False)
    # The stated expiry moves within the window (another poll, a different credential's stamp):
    # still the same news.
    _flag(state, "a@test", False, iso(now() + timedelta(days=2)))
    assert accounts.take_signin_notices(state)[0] == []
    _flag(state, "a@test", False, iso(now() + timedelta(days=10)))  # a fresh login: quiet, re-armed
    assert accounts.take_signin_notices(state) == ([], True)
    _flag(state, "a@test", False, iso(now() + timedelta(days=2)))   # ...and its own expiry nears
    assert len(accounts.take_signin_notices(state)[0]) == 1


def test_bridge_poll_delivers_each_notice_once_and_honours_the_setting(ctx, monkeypatch):
    _seed_claude(ctx)
    monkeypatch.setattr(bridge.usage_mod, "refresh_live", lambda *_a, **_k: {"claude": {}})
    monkeypatch.setattr(claudesync, "converge", lambda *_a, **_k: None)
    state = ctx.load_state()
    _flag(state, "a@test", True)
    state.save()
    first = bridge.handle(ctx, {"action": "usage", "tool": "claude"})
    second = bridge.handle(ctx, {"action": "usage", "tool": "claude"})
    assert len(first["signin_notices"]) == 1 and second["signin_notices"] == []

    state = ctx.load_state()
    state.set_setting("notify", False)
    _flag(state, "b@test", True)
    state.save()
    muted = bridge.handle(ctx, {"action": "usage", "tool": "claude"})
    assert muted["signin_notices"] == []
    state = ctx.load_state()
    assert state.get_seat("claude", "b@test")["signin_notified"]["signed_out"] is True
    state.set_setting("notify", True)
    state.save()
    assert bridge.handle(ctx, {"action": "usage", "tool": "claude"})["signin_notices"] == []
