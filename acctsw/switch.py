"""The credential swap primitive — the heart of safe account switching.

Order matters (per plan):
  1. SYNC-BACK FIRST: copy the outgoing account's *current live* creds back into its keychain
     snapshot. Codex/Claude rotate refresh tokens, so the live copy is always the freshest and
     must be preserved or the stored snapshot goes stale and stops working.
  2. Install the chosen account's snapshot into the canonical location ATOMICALLY.
  3. Update active in state and persist.

Supervised children use private homes. The canonical location is only the stock-tool mirror.
"""
from __future__ import annotations

from .context import Context
from .errors import MissingSnapshot, UnknownSeat
from .identity import ClaudeLiveIdentity
from .state import State
from .util import iso, now


def sync_back(ctx: Context, state: State, tool: str, *,
              live_identity: ClaudeLiveIdentity | None = None) -> bool:
    """Persist the live creds of the currently-active seat into its snapshot. Returns True if done.

    Guard: if the live creds belong to a *different* account than ``state.active`` (e.g. the user ran
    a stock login or switched in the GUI), capture/adopt a known identity or skip the sync-back
    instead of corrupting the old seat's snapshot with foreign bytes. Codex identifies from its JWT;
    Claude must reconcile through the OAuth profile endpoint because its blob has no email.
    Lock-owning Claude callers must resolve and pass ``live_identity`` before taking the flock.
    """
    if tool == "claude":
        # Lazy import avoids accounts -> usage/state imports becoming a module cycle. An unknown or
        # unreadable CLI identity is deliberately a no-op: proceeding was the permanent guard bypass
        # that let account B's Keychain bytes overwrite active seat A.
        from .accounts import reconcile_claude
        if reconcile_claude(ctx, state, live_identity=live_identity) is None:
            return False
    active = state.active(tool)
    if not active:
        return False
    if tool == "codex":
        from .session import active_session
        running = active_session(ctx.data_dir, tool, email=active)
        if running and running.get("email") == active:
            # The child rotates credentials in its private home. A GUI/CLI switch changes the
            # mirror before the supervisor hands off; copying that older mirror back would lose
            # the running child's latest refresh token.
            return False
    live = ctx.cred[tool].get_live()
    if not live:
        return False
    live_email = ctx.cred[tool].email_of(live)
    if tool == "codex" and live_email != active:
        return False  # unknown or mismatch — never clobber the active seat's store
    if tool != "codex" and live_email is not None and live_email != active:
        return False
    ctx.snapshot_set(tool, active, live)   # codex → per-account home; claude → keychain
    return True


def switch(ctx: Context, state: State, tool: str, email: str, *, sync: bool = True,
           manual: bool = False,
           mirror: bool = True,
           save: bool = True,
           live_identity: ClaudeLiveIdentity | None = None) -> None:
    """Make ``email`` the active seat for ``tool``.

    ``sync=False`` skips sync-back from the stock mirror. Both supervised tools maintain their
    own homes; ``mirror=False`` also leaves stock credentials alone (supervised Claude hops).
    ``manual=True`` records a request for supervised Codex sessions to resume on the selected seat.
    ``save=False`` lets a lock-owning caller persist additional hop metadata in the same write.
    """
    if email not in state.accounts(tool):
        raise UnknownSeat(f"no seat '{email}' for {tool}")

    # 1. sync-back outgoing (no-op if switching to the same / no active seat)
    if sync:
        sync_back(ctx, state, tool, live_identity=live_identity)

    # 2. install chosen account's stored creds into the canonical location (the active mirror)
    if tool == "claude":
        from . import claudehome
        if not ctx._store_lock_held():
            claudehome.prepare(ctx, email)
        claudehome.ensure_home(ctx, email)
        blob = claudehome.load(ctx, email)
    else:
        blob = ctx.snapshot_get(tool, email)
    if blob is None:
        raise MissingSnapshot(f"stored creds for {tool}:{email} not found — re-add this seat")
    if mirror:
        if tool == "claude":
            claudehome.mirror_identity(ctx, email)
        ctx.set_live(tool, blob)
        if tool == "claude":
            from .accounts import _claude_digest
            state.data["tools"][tool]["captured_stock_digest"] = _claude_digest(blob)

    # 3. record active
    state.set_active(tool, email)
    state.set_last_on_floor(tool, email, iso(now()))
    if manual and tool == "codex":
        # An explicit click/CLI request authorizes a live handoff. Ordinary active-pointer
        # changes (login reconciliation, usage polling) must never interrupt a healthy child.
        # The caller holds the state lock and save() increments this revision. It already
        # gives every click a unique ID without an extra stdlib dependency in older bundles.
        state.data["tools"][tool]["manual_switch"] = {
            "id": int(state.data.get("rev", 0)) + 1, "email": email,
        }
    if save:
        state.save()
