"""The credential swap primitive — the heart of safe account switching.

Order matters (per plan):
  1. SYNC-BACK FIRST: copy the outgoing account's *current live* creds back into its keychain
     snapshot. Codex/Claude rotate refresh tokens, so the live copy is always the freshest and
     must be preserved or the stored snapshot goes stale and stops working.
  2. Install the chosen account's snapshot into the canonical location ATOMICALLY.
  3. Update active in state and persist.

Only one account is ever active per tool (sequential), so there is no concurrency hazard.
"""
from __future__ import annotations

from .context import Context
from .errors import MissingSnapshot, UnknownSeat
from .state import State
from .util import iso, now


def sync_back(ctx: Context, state: State, tool: str) -> bool:
    """Persist the live creds of the currently-active seat into its snapshot. Returns True if done.

    Guard: if the live creds belong to a *different* account than ``state.active`` (e.g. the user ran
    a stock login or switched in the GUI), capture/adopt a known identity or skip the sync-back
    instead of corrupting the old seat's snapshot with foreign bytes. Codex identifies from its JWT;
    Claude must reconcile through ``claude auth status --json`` because its blob has no email.
    """
    if tool == "claude":
        # Lazy import avoids accounts -> usage/state imports becoming a module cycle. An unknown or
        # unreadable CLI identity is deliberately a no-op: proceeding was the permanent guard bypass
        # that let account B's Keychain bytes overwrite active seat A.
        from .accounts import reconcile_claude
        if reconcile_claude(ctx, state) is None:
            return False
    active = state.active(tool)
    if not active:
        return False
    live = ctx.cred[tool].get_live()
    if not live:
        return False
    live_email = ctx.cred[tool].email_of(live)
    if live_email is not None and live_email != active:
        return False  # mismatch — don't clobber the active seat's store
    ctx.snapshot_set(tool, active, live)   # codex → per-account home; claude → keychain
    return True


def switch(ctx: Context, state: State, tool: str, email: str, *, sync: bool = True) -> None:
    """Make ``email`` the active seat for ``tool``.

    ``sync=False`` skips the sync-back-from-mirror — used by the launcher for codex, where codex
    maintains the account's own home directly (the home, not ~/.codex, is the source of truth).
    """
    if email not in state.accounts(tool):
        raise UnknownSeat(f"no seat '{email}' for {tool}")

    # 1. sync-back outgoing (no-op if switching to the same / no active seat)
    if sync:
        sync_back(ctx, state, tool)

    # 2. install chosen account's stored creds into the canonical location (the active mirror)
    blob = ctx.snapshot_get(tool, email)
    if blob is None:
        raise MissingSnapshot(f"stored creds for {tool}:{email} not found — re-add this seat")
    ctx.cred[tool].set_live(blob)

    # 3. record active
    state.set_active(tool, email)
    state.set_last_on_floor(tool, email, iso(now()))
    state.save()
