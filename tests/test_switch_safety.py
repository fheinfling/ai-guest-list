"""Partial-failure & corruption-safety tests for the switch primitive (reviewer gap #1-3)."""
import json

import pytest

from acctsw import accounts as acct
from acctsw.errors import MissingSnapshot
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
    assert ctx.keychain.get(ctx.keychain_service, "codex:a@x.com") is not None


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
    a_snapshot_before = ctx.keychain.get(ctx.keychain_service, "codex:a@x.com")
    # user logs in as a DIFFERENT account out-of-band → live is now c
    ctx.cred["codex"].set_live(make_codex_blob("c@x.com"))
    assert sync_back(ctx, state, "codex") is False
    # a's snapshot was NOT overwritten with c's creds
    assert ctx.keychain.get(ctx.keychain_service, "codex:a@x.com") == a_snapshot_before
