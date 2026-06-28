"""Seat management: add (snapshot the live account), remove, list, status."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .context import Context
from .errors import CannotIdentify, NoLiveCreds
from .identity import live_email
from .selection import choose
from .state import State
from .util import now, parse_iso


def add(ctx: Context, state: State, tool: str, *, name: str | None = None,
        email: str | None = None) -> dict[str, Any]:
    """Snapshot the currently-live account for ``tool`` into a seat.

    The caller is expected to have signed in via the official flow first (so the live creds are
    the account being added). After adding, that account becomes the active seat (it *is* live).
    """
    live = ctx.cred[tool].get_live()
    if not live:
        raise NoLiveCreds(f"no live {tool} credentials — sign in with the official tool first")
    em = email or live_email(ctx, tool)
    if not em:
        raise CannotIdentify(f"could not determine the account email for {tool}")
    ctx.keychain.set(ctx.keychain_service, ctx.snapshot_key(tool, em), live)
    seat = state.upsert_seat(tool, em, name=name)
    state.set_active(tool, em)  # the freshly signed-in account is what's live now
    state.save()
    return seat


def remove(ctx: Context, state: State, tool: str, email: str) -> bool:
    """Remove a seat: delete its keychain snapshot and its state entry. Returns True if it existed."""
    ctx.keychain.delete(ctx.keychain_service, ctx.snapshot_key(tool, email))
    existed = state.remove_seat(tool, email)
    state.save()
    return existed


def _seat_view(seat: dict, *, active: bool, at: datetime) -> dict[str, Any]:
    until = parse_iso(seat.get("limited_until"))
    limited = until is not None and until > at
    return {
        "email": seat["email"],
        "name": seat.get("name", seat["email"]),
        "active": active,
        "limited": limited,
        "limited_until": seat.get("limited_until") if limited else None,
        "usage": seat.get("usage"),
        "added_at": seat.get("added_at"),
    }


def list_seats(state: State, tool: str, at: datetime | None = None) -> list[dict[str, Any]]:
    at = at or now()
    active = state.active(tool)
    return [
        _seat_view(seat, active=(email == active), at=at)
        for email, seat in state.accounts(tool).items()
    ]


def status(ctx: Context, state: State, at: datetime | None = None) -> dict[str, Any]:
    """Full structured status for `status --json` and the menubar."""
    at = at or now()
    out: dict[str, Any] = {"settings": state.settings(), "tools": {}}
    for tool in ("codex", "claude"):
        sel = choose(state, tool, at)
        out["tools"][tool] = {
            "active": state.active(tool),
            "seats": list_seats(state, tool, at),
            "selection": {
                "email": sel.email,
                "available": sel.available,
                "all_limited": sel.all_limited,
                "unlocks_at": sel.unlocks_at.isoformat() if sel.unlocks_at else None,
            },
        }
    return out
