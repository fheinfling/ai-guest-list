"""Feature tests for add/remove/list/status and the switch sync-back primitive."""
import json

import pytest

from acctsw import accounts as acct
from acctsw.errors import MissingSnapshot, NoLiveCreds, UnknownSeat
from acctsw.switch import switch, sync_back
from tests.conftest import make_codex_blob, make_claude_blob


def _add_codex(ctx, email):
    """Simulate signing in as `email` (live creds) then adding the seat."""
    ctx.cred["codex"].set_live(make_codex_blob(email))
    state = ctx.load_state()
    seat = acct.add(ctx, state, "codex", name=email.split("@")[0])
    return state, seat


def test_add_stamps_account_fingerprint(ctx):
    _, seat = _add_codex(ctx, "a@x.com")
    assert seat["account_id"] == "acct:a@x.com"   # from the codex token's account_id


def test_shared_account_seats_are_flagged_and_warned(ctx):
    """Two codex seats backed by the SAME ChatGPT account (e.g. a Gmail '+alias' login) share one
    quota — they must be flagged and a warning surfaced, since switching between them can't help."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="same"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("a+codex@x.com", account_id="same"))
    acct.add(ctx, ctx.load_state(), "codex", email="a+codex@x.com")
    state = ctx.load_state()
    seats = acct.list_seats(state, "codex")
    assert all(s["shared_account"] for s in seats)
    a = next(s for s in seats if s["email"] == "a@x.com")
    assert a["shared_account_with"] == ["a+codex@x.com"]
    warnings = acct.status(ctx, state)["warnings"]
    assert len(warnings) == 1 and "same account" in warnings[0]
    assert "a@x.com" in warnings[0] and "a+codex@x.com" in warnings[0]


def test_plus_alias_same_user_is_still_flagged_as_shared(ctx):
    """The real duplicate: ONE person, one subscription, two logins (Gmail '+alias'). Same user id
    AND same account id → one quota, so both seats are flagged and the warning still fires."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="same", user_id="user-a"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("a+codex@x.com", account_id="same", user_id="user-a"))
    acct.add(ctx, ctx.load_state(), "codex", email="a+codex@x.com")
    state = ctx.load_state()
    seats = acct.list_seats(state, "codex")
    assert all(s["shared_account"] for s in seats)
    a = next(s for s in seats if s["email"] == "a@x.com")
    assert a["shared_account_with"] == ["a+codex@x.com"]
    assert a["shared_workspace_with"] == []          # already said as shared_account, not twice
    assert len(acct.status(ctx, state)["warnings"]) == 1


def test_team_members_are_not_flagged_as_shared(ctx):
    """Two members of ONE Team/Business workspace share the account id but NOT the person: each has
    their own 5h/weekly windows, so they are real headroom — no flag, no warning, just a note that
    they sit in the same workspace."""
    ctx.cred["codex"].set_live(make_codex_blob("me@corp.com", account_id="ws-1", user_id="user-me"))
    acct.add(ctx, ctx.load_state(), "codex", email="me@corp.com")
    ctx.cred["codex"].set_live(make_codex_blob("you@corp.com", account_id="ws-1", user_id="user-you"))
    acct.add(ctx, ctx.load_state(), "codex", email="you@corp.com")
    state = ctx.load_state()
    seats = acct.list_seats(state, "codex")
    assert not any(s["shared_account"] for s in seats)
    assert all(s["shared_account_with"] == [] for s in seats)
    me = next(s for s in seats if s["email"] == "me@corp.com")
    assert me["account_id"] == "user-me" and me["workspace_id"] == "ws-1"
    assert me["shared_workspace_with"] == ["you@corp.com"]
    assert acct.status(ctx, state)["warnings"] == []


def test_distinct_account_seats_are_not_flagged(ctx):
    """Genuinely separate accounts (distinct account_id) give real headroom → no flag, no warning."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="acct-a"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("b@x.com", account_id="acct-b"))
    acct.add(ctx, ctx.load_state(), "codex", email="b@x.com")
    state = ctx.load_state()
    assert not any(s["shared_account"] for s in acct.list_seats(state, "codex"))
    assert acct.status(ctx, state)["warnings"] == []


def test_add_clears_stale_auth_error(ctx):
    """Re-capturing a seat (after re-login) must reset its usage error/backoff so the app
    auto-recovers instead of staying stuck on 'log in' behind the error backoff."""
    state, _ = _add_codex(ctx, "a@x.com")
    u = state.get_seat("codex", "a@x.com").setdefault("usage", {})
    u.update(error="unauthorized", error_streak=20, stale=True,
             fetched_at="2026-01-01T00:00:00+00:00",
             last_attempted_at="2026-07-31T00:00:00+00:00")
    state.save()
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    u2 = ctx.load_state().get_seat("codex", "a@x.com")["usage"]
    assert u2["error"] is None and u2["error_streak"] == 0
    assert u2["stale"] is False and u2["fetched_at"] is None
    assert u2["last_attempted_at"] is None                     # forces an immediate re-check


def test_reconcile_codex_clears_error_on_changed_creds(ctx):
    state, _ = _add_codex(ctx, "a@x.com")
    state.get_seat("codex", "a@x.com").setdefault("usage", {})["error"] = "unauthorized"
    state.save()
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com") + " ")  # different bytes, same account
    em = acct.reconcile_codex(ctx, ctx.load_state())
    assert em == "a@x.com"
    assert ctx.load_state().get_seat("codex", "a@x.com")["usage"]["error"] is None


def test_add_snapshots_and_activates(ctx):
    state, seat = _add_codex(ctx, "a@x.com")
    assert seat["email"] == "a@x.com"
    assert state.active("codex") == "a@x.com"
    # snapshot stored in our keychain
    assert ctx.snapshot_get("codex", "a@x.com") is not None


def test_add_without_live_creds_errors(ctx):
    state = ctx.load_state()
    with pytest.raises(NoLiveCreds):
        acct.add(ctx, state, "codex")


def test_add_claude_uses_supplied_email(ctx):
    # Claude blob carries no email → identity must be supplied (M6/CLI passes it via auth status)
    ctx.cred["claude"].set_live(make_claude_blob())
    state = ctx.load_state()
    seat = acct.add(ctx, state, "claude", email="c@x.com")
    assert seat["email"] == "c@x.com"
    assert state.active("claude") == "c@x.com"


def test_switch_syncs_back_then_installs(ctx):
    # add two codex seats
    _add_codex(ctx, "a@x.com")
    state, _ = _add_codex(ctx, "b@x.com")  # now live + active = b
    # mutate b's LIVE creds (simulate a refresh-token rotation while b was active)
    rotated = make_codex_blob("b@x.com").replace('"refresh_token": "r"', '"refresh_token": "ROT"')
    ctx.cred["codex"].set_live(rotated)

    switch(ctx, state, "codex", "a@x.com")

    # active is now a, and a's snapshot is installed live
    assert state.active("codex") == "a@x.com"
    live = json.loads(ctx.cred["codex"].get_live())
    assert live["tokens"]["id_token"].split(".")[1]  # is a-token (email a)
    # crucially: b's rotated creds were synced back to its snapshot, not lost
    b_snap = json.loads(ctx.snapshot_get("codex", "b@x.com"))
    assert b_snap["tokens"]["refresh_token"] == "ROT"


def test_switch_unknown_seat(ctx):
    state, _ = _add_codex(ctx, "a@x.com")
    with pytest.raises(UnknownSeat):
        switch(ctx, state, "codex", "ghost@x.com")


def test_switch_missing_snapshot(ctx):
    state, _ = _add_codex(ctx, "a@x.com")
    state.upsert_seat("codex", "b@x.com")  # registered but never snapshotted
    with pytest.raises(MissingSnapshot):
        switch(ctx, state, "codex", "b@x.com")


def test_remove_deletes_snapshot_and_seat(ctx):
    state, _ = _add_codex(ctx, "a@x.com")
    assert acct.remove(ctx, state, "codex", "a@x.com") is True
    assert ctx.snapshot_get("codex", "a@x.com") is None
    assert state.get_seat("codex", "a@x.com") is None
    assert state.active("codex") is None
    assert acct.remove(ctx, state, "codex", "a@x.com") is False


def test_sync_back_noop_without_active(ctx):
    state = ctx.load_state()
    assert sync_back(ctx, state, "codex") is False


def test_status_structure(ctx):
    _add_codex(ctx, "a@x.com")
    state, _ = _add_codex(ctx, "b@x.com")
    data = acct.status(ctx, state)
    assert set(data["tools"]) == {"codex", "claude"}
    codex = data["tools"]["codex"]
    assert codex["active"] == "b@x.com"
    assert len(codex["seats"]) == 2
    assert codex["selection"]["email"] in {"a@x.com", "b@x.com"}


def test_list_seats_marks_active_and_limited(ctx):
    from acctsw.util import now, iso
    from datetime import timedelta
    _add_codex(ctx, "a@x.com")
    state, _ = _add_codex(ctx, "b@x.com")
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=1)))
    seats = {s["email"]: s for s in acct.list_seats(state, "codex")}
    assert seats["b@x.com"]["active"] is True
    assert seats["a@x.com"]["limited"] is True


def test_reconcile_codex_captures_fresh_live_into_home(ctx):
    """ISO-B1: a fresh/out-of-band ~/.codex is captured into the owning account's home + adopted."""
    _add_codex(ctx, "a@x.com")
    state, _ = _add_codex(ctx, "b@x.com")          # active=b
    # user logs into 'a' out-of-band (plain codex) with a rotated token
    rotated = make_codex_blob("a@x.com").replace('"refresh_token": "r"', '"refresh_token": "FRESH"')
    ctx.cred["codex"].set_live(rotated)
    reconciled = acct.reconcile_codex(ctx, state)
    assert reconciled == "a@x.com"
    assert ctx.load_state().active("codex") == "a@x.com"        # adopted the out-of-band login
    snap = json.loads(ctx.snapshot_get("codex", "a@x.com"))
    assert snap["tokens"]["refresh_token"] == "FRESH"           # captured into a's home


def test_reconcile_codex_ignores_unknown_identity(ctx):
    state, _ = _add_codex(ctx, "a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("stranger@x.com"))  # not a seat
    assert acct.reconcile_codex(ctx, state) is None
    assert ctx.load_state().active("codex") == "a@x.com"          # unchanged


def test_reconcile_claude_captures_and_adopts_out_of_band_login(ctx, monkeypatch):
    """Claude blobs carry no email: auth-status identity must route fresh Keychain bytes to the
    matching known seat instead of leaving state on the old account."""
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    acct.add(ctx, ctx.load_state(), "claude", email="a@x.com")
    ctx.cred["claude"].set_live(make_claude_blob("pro"))
    state = ctx.load_state()
    state.upsert_seat("claude", "b@x.com")
    ctx.snapshot_set("claude", "b@x.com", make_claude_blob("max"))
    state.save()
    monkeypatch.setattr(acct.identity, "claude_status_email", lambda _: "b@x.com")

    assert acct.reconcile_claude(ctx, state) == "b@x.com"
    loaded = ctx.load_state()
    assert loaded.active("claude") == "b@x.com"
    stored = json.loads(ctx.snapshot_get("claude", "b@x.com"))
    assert stored["claudeAiOauth"]["subscriptionType"] == "pro"


def test_forbidden_non_active_seat_needs_login_but_401_does_not(ctx):
    """Parked 401 tokens are routine, while a 403 entitlement loss must be visible on every seat."""
    _add_codex(ctx, "a@x.com")
    state, _ = _add_codex(ctx, "b@x.com")  # active b
    state.get_seat("codex", "a@x.com")["usage"] = {"error": "unauthorized"}
    state.get_seat("codex", "b@x.com")["usage"] = {"error": None}
    seats = {s["email"]: s for s in acct.list_seats(state, "codex")}
    assert seats["a@x.com"]["needs_login"] is False

    state.get_seat("codex", "a@x.com")["usage"]["error"] = "forbidden"
    seats = {s["email"]: s for s in acct.list_seats(state, "codex")}
    assert seats["a@x.com"]["needs_login"] is True
    assert seats["a@x.com"]["entitlement_revoked"] is True
    assert seats["a@x.com"]["status"] == "needs-login"


def test_list_seats_projects_live_session_onto_matching_seat(ctx, monkeypatch):
    """The heartbeat belongs only to its named seat; active state alone must not invent a session."""
    state, _ = _add_codex(ctx, "a@x.com")
    started = "2026-07-31T10:00:00+00:00"
    monkeypatch.setattr(acct.session, "active_session",
                        lambda data_dir, tool: {"email": "a@x.com", "pid": 42,
                                                "started_at": started})
    seat = acct.list_seats(state, "codex", data_dir=ctx.data_dir)[0]
    assert seat["in_session"] is True and seat["session_started_at"] == started
