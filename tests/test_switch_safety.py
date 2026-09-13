"""Partial-failure & corruption-safety tests for the switch primitive (reviewer gap #1-3)."""
import contextlib
import json

import pytest

from acctsw import accounts as acct
from acctsw import bridge
from acctsw.errors import CredentialIdentityMismatch, MissingSnapshot, UnsafeCodexMirror
from acctsw.identity import ClaudeLiveIdentity
from acctsw.switch import switch, sync_back
from tests.conftest import make_codex_blob, make_claude_blob


def _add_codex(ctx, email):
    ctx.cred["codex"].set_live(make_codex_blob(email))
    state = ctx.load_state()
    acct.add(ctx, state, "codex", email=email)
    return state


def test_set_live_failure_leaves_live_and_active_unchanged(ctx):
    """If installing the new snapshot fails, the canonical creds AND state.active stay intact."""
    _add_codex(ctx, "a@x.com")
    state = _add_codex(ctx, "b@x.com")  # active=b, live=b
    before_live = ctx.cred["codex"].get_live()

    def boom(_blob):
        raise OSError("disk full")
    ctx.cred["codex"].set_live = boom  # type: ignore[assignment]

    with pytest.raises(OSError):
        switch(ctx, state, "codex", "a@x.com")

    # live unchanged, and state never advanced (save() not reached)
    assert ctx.load_state().active("codex") == "b@x.com"
    # restore real method to read live
    del ctx.cred["codex"].set_live  # type: ignore[attr-defined]
    assert ctx.cred["codex"].get_live() == before_live


def test_missing_snapshot_preserves_outgoing_live(ctx):
    """Aborting on MissingSnapshot must not lose the outgoing account's (synced-back) creds."""
    state = _add_codex(ctx, "a@x.com")
    state.upsert_seat("codex", "b@x.com")  # registered, never snapshotted
    state.save()
    with pytest.raises(MissingSnapshot):
        switch(ctx, state, "codex", "b@x.com")
    # a's live is intact and its snapshot still present
    assert ctx.cred["codex"].get_live() is not None
    assert ctx.snapshot_get("codex", "a@x.com") is not None


def test_claude_set_live_updates_same_item_no_duplicate(ctx):
    loc = ctx.cred["claude"]
    loc.set_live(make_claude_blob("max"))
    loc.set_live(make_claude_blob("pro"))
    # exactly one (service, account) entry exists in the fake keychain
    keys = [k for k in ctx.keychain._store if k[0] == "Claude Code-credentials"]
    assert len(keys) == 1
    assert json.loads(loc.get_live())["claudeAiOauth"]["subscriptionType"] == "pro"


def test_sync_back_skips_on_codex_account_mismatch(ctx):
    """If live creds belong to a different account than state.active, don't clobber the snapshot."""
    state = _add_codex(ctx, "a@x.com")  # active=a, snapshot a saved
    a_snapshot_before = ctx.snapshot_get("codex", "a@x.com")
    # user logs in as a DIFFERENT account out-of-band → live is now c
    ctx.cred["codex"].set_live(make_codex_blob("c@x.com"))
    assert sync_back(ctx, state, "codex") is False
    # a's snapshot was NOT overwritten with c's creds
    assert ctx.snapshot_get("codex", "a@x.com") == a_snapshot_before


def test_sync_back_skips_codex_blob_without_a_jwt_email(ctx):
    """An unreadable identity is not permission to overwrite a known seat."""
    state = _add_codex(ctx, "a@x.com")
    before = ctx.snapshot_get("codex", "a@x.com")
    ctx.cred["codex"].set_live('{"tokens": {"id_token": "not-a-jwt"}}')

    assert sync_back(ctx, state, "codex") is False
    assert ctx.snapshot_get("codex", "a@x.com") == before


def test_codex_snapshot_write_rejects_mismatched_jwt_and_preserves_destination(ctx):
    """A valid alias JWT can never overwrite the plain-email seat's private auth.json."""
    _add_codex(ctx, "plain@x.com")
    before = ctx.snapshot_get("codex", "plain@x.com")

    with pytest.raises(CredentialIdentityMismatch):
        ctx.snapshot_set("codex", "plain@x.com", make_codex_blob("plain+codex@x.com"))

    assert ctx.snapshot_get("codex", "plain@x.com") == before


def test_switch_refuses_a_context_whose_canonical_mirror_is_a_private_seat_home(ctx):
    """A leaked managed CODEX_HOME must not turn an alias switch into a private-home write."""
    state = _add_codex(ctx, "plain@x.com")
    _add_codex(ctx, "plain+codex@x.com")
    state = ctx.load_state()
    switch(ctx, state, "codex", "plain@x.com")
    before = ctx.snapshot_get("codex", "plain@x.com")
    ctx.cred["codex"].auth_path = ctx.codex_home("plain@x.com") / "auth.json"

    with pytest.raises(UnsafeCodexMirror):
        switch(ctx, state, "codex", "plain+codex@x.com", sync=False)

    assert ctx.snapshot_get("codex", "plain@x.com") == before
    assert state.active("codex") == "plain@x.com"


def test_manual_switch_preserves_supervised_codex_token_rotation(ctx, monkeypatch):
    """A GUI swap must not save its old mirror over the running child's rotated private home."""
    from acctsw import session
    _add_codex(ctx, "spare@x.com")
    state = _add_codex(ctx, "running@x.com")
    rotated = make_codex_blob("running@x.com").replace('"refresh_token": "r"',
                                                      '"refresh_token": "ROTATED"')
    ctx.snapshot_set("codex", "running@x.com", rotated)
    monkeypatch.setattr(session, "active_session", lambda *_, **_kw: {"email": "running@x.com"})

    result = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "spare@x.com"})

    assert result["ok"]
    assert ctx.load_state().active("codex") == "spare@x.com"
    assert ctx.snapshot_get("codex", "running@x.com") == rotated
    assert ctx.cred["codex"].email_of(ctx.cred["codex"].get_live()) == "spare@x.com"


def test_sync_back_claude_unknown_identity_never_clobbers_old_seat(ctx, monkeypatch):
    """Claude email_of is permanently None; an unknown auth-status identity must stop account B's
    Keychain bytes from being saved under active seat A."""
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="a@x.com")
    before = ctx.snapshot_get("claude", "a@x.com")
    ctx.cred["claude"].set_live(make_claude_blob("pro"))
    monkeypatch.setattr(acct.identity, "claude_status_email", lambda _: "stranger@x.com")

    assert sync_back(ctx, state, "claude") is False
    assert ctx.snapshot_get("claude", "a@x.com") == before


def test_sync_back_claude_stale_blob_identity_is_noop(ctx):
    """An identity answer for blob A must not authorize blob B after a login races the lock."""
    blob_a = make_claude_blob("max")
    blob_b = make_claude_blob("pro")
    ctx.cred["claude"].set_live(blob_a)
    state = ctx.load_state()
    acct.add(ctx, state, "claude", email="a@x.com")
    before = ctx.snapshot_get("claude", "a@x.com")
    stale = ClaudeLiveIdentity(blob=blob_a, email="a@x.com")

    ctx.cred["claude"].set_live(blob_b)

    assert sync_back(ctx, state, "claude", live_identity=stale) is False
    assert ctx.snapshot_get("claude", "a@x.com") == before


def test_bridge_claude_switch_resolves_identity_before_state_lock(ctx, monkeypatch):
    """`claude auth status --json` may stall for 30 seconds. A UI switch must resolve it before
    taking the cross-process state flock while still tying that identity to the exact live blob."""
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    acct.add(ctx, ctx.load_state(), "claude", email="a@x.com")
    ctx.cred["claude"].set_live(make_claude_blob("pro"))
    acct.add(ctx, ctx.load_state(), "claude", email="b@x.com")  # active/live b

    held = {"value": False}
    real_locked = ctx.locked

    @contextlib.contextmanager
    def tracked_locked():
        with real_locked():
            held["value"] = True
            try:
                yield
            finally:
                held["value"] = False

    def status_email(_claude_bin):
        assert held["value"] is False, "Claude identity subprocess ran under the state flock"
        return "b@x.com"

    monkeypatch.setattr(ctx, "locked", tracked_locked)
    monkeypatch.setattr(acct.identity, "claude_status_email", status_email)

    result = bridge.handle(ctx, {"action": "switch", "tool": "claude", "email": "a@x.com"})

    assert result["ok"] is True
    assert ctx.load_state().active("claude") == "a@x.com"
