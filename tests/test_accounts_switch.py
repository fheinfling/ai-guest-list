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
