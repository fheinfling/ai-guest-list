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


def sync_back(ctx: Context, state: State, tool: str) -> bool:
    """Persist the live creds of the currently-active seat into its snapshot. Returns True if done."""
    active = state.active(tool)
    if not active:
        return False
    live = ctx.cred[tool].get_live()
    if not live:
        return False
    ctx.keychain.set(ctx.keychain_service, ctx.snapshot_key(tool, active), live)
    return True


def switch(ctx: Context, state: State, tool: str, email: str) -> None:
    """Make ``email`` the active seat for ``tool``."""
    if email not in state.accounts(tool):
        raise UnknownSeat(f"no seat '{email}' for {tool}")

    # 1. sync-back outgoing (no-op if switching to the same / no active seat)
    sync_back(ctx, state, tool)

    # 2. install chosen snapshot into the canonical location
    blob = ctx.keychain.get(ctx.keychain_service, ctx.snapshot_key(tool, email))
    if blob is None:
        raise MissingSnapshot(f"snapshot for {tool}:{email} not found — re-add this seat")
    ctx.cred[tool].set_live(blob)

    # 3. record active
    state.set_active(tool, email)
    state.save()
