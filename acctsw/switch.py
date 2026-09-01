"""The credential swap primitive — the heart of safe account switching.

Order matters (per plan):
  1. SYNC-BACK FIRST: copy the outgoing account's *current live* creds back into its keychain
     snapshot. Codex/Claude rotate refresh tokens, so the live copy is always the freshest and
     must be preserved or the stored snapshot goes stale and stops working.
  2. Install the chosen account's snapshot into the canonical location ATOMICALLY.
  3. Update active in state and persist.

Only one account is active per tool. Multiple Codex children may share it; the runtime lease guard
prevents credential replacement until those children have stopped.
"""
from __future__ import annotations

from .context import Context
from .errors import CodexBusy, MissingSnapshot, UnknownSeat
from .state import State


def sync_back(ctx: Context, state: State, tool: str) -> bool:
    """Persist the live creds of the currently-active seat into its snapshot. Returns True if done.

    Guard: if the live creds clearly belong to a *different* account than ``state.active`` (e.g.
    the user ran stock ``codex login`` or switched in the GUI — an in-scope scenario), we skip the
    sync-back instead of corrupting the active seat's snapshot with foreign creds. Detectable for
    Codex (email is in the JWT); best-effort for Claude (no email in the blob → proceed).
    """
    active = state.active(tool)
    if not active:
        return False
    live = ctx.cred[tool].get_live()
    if not live:
        return False
    live_email = ctx.cred[tool].email_of(live)
    if live_email is not None and live_email != active:
        return False  # mismatch — don't clobber the active seat's store
    ctx.snapshot_set(tool, active, live)   # codex → per-account auth store; claude → keychain
    return True


def switch(ctx: Context, state: State, tool: str, email: str, *, sync: bool = True) -> None:
    """Make ``email`` the active seat for ``tool``.

    ``sync=False`` remains available for recovery/import callers that have already preserved the
    outgoing canonical credentials. Normal launcher and manual switches always sync first.
    """
    if email not in state.accounts(tool):
        raise UnknownSeat(f"no seat '{email}' for {tool}")

    if email == state.active(tool):
        return

    # Codex now stays on its canonical home. Replacing canonical auth.json while a supervised child
    # is running could make that child refresh the wrong seat's credentials, so manual changes fail
    # fast. The launcher catches this typed error, waits interruptibly, and retries after its stopped
    # child no longer competes with any still-running session.
    def _install() -> None:
        # 1. sync-back outgoing (no-op if no active seat)
        if sync:
            sync_back(ctx, state, tool)

        # 2. install chosen account's stored creds into the canonical location.
        blob = ctx.snapshot_get(tool, email)
        if blob is None:
            raise MissingSnapshot(f"stored creds for {tool}:{email} not found — re-add this seat")
        ctx.cred[tool].set_live(blob)

        # 3. record active
        state.set_active(tool, email)
        state.save()

    if tool == "codex":
        from . import codexruntime
        with codexruntime.gate(ctx.data_dir) as root:
            if codexruntime.running_count_locked(root):
                raise CodexBusy(
                    "can't switch Codex seats while supervised Codex sessions are active — "
                    "close them or let auto-switch wait for them"
                )
            _install()
    else:
        _install()
