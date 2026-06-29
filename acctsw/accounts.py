"""Seat management: add (snapshot the live account), remove, list, status."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .context import Context
from .errors import CannotIdentify, NoLiveCreds
from .identity import live_email
from .selection import choose
from .state import State
from .util import now, parse_iso, jwt_payload
import json

# raw plan code -> display label (spec §4: Business|Team|Pro|Max|Free)
_PLAN_LABELS = {"business": "Business", "team": "Team", "enterprise": "Enterprise",
                "pro": "Pro", "plus": "Plus", "max": "Max", "free": "Free"}


def plan_of(tool: str, blob: str | None) -> str | None:
    """Best-effort plan/tier from a credential blob (codex JWT / claude oauth)."""
    if not blob:
        return None
    try:
        d = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    raw = None
    if tool == "codex":
        p = jwt_payload((d.get("tokens") or {}).get("id_token", ""))
        raw = (p.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or p.get("chatgpt_plan_type")
    else:
        raw = (d.get("claudeAiOauth") or {}).get("subscriptionType")
    return _PLAN_LABELS.get(str(raw).lower(), str(raw).title()) if raw else None


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
    ctx.snapshot_set(tool, em, live)
    seat = state.upsert_seat(tool, em, name=name, plan=plan_of(tool, live))
    state.set_active(tool, em)  # the freshly signed-in account is what's live now
    _creds_refreshed(state, tool, em)
    state.save()
    return seat


def _creds_refreshed(state: State, tool: str, em: str) -> None:
    """A seat's stored credentials just changed (re-login / capture / out-of-band login). Clear any
    stale auth error and reset the usage backoff so the NEXT poll re-validates immediately and the
    'log in' badge clears — instead of the seat staying stuck behind the (up to 1h) error backoff.
    Without this, fixing a revoked seat never reflects in the app until the backoff expires."""
    seat = state.get_seat(tool, em)
    if seat is None:
        return
    u = seat.get("usage")
    if not isinstance(u, dict):  # upsert_seat seeds usage as None until the first poll
        u = {}
        seat["usage"] = u
    u["error"] = None
    u["error_streak"] = 0
    u["stale"] = False
    u["fetched_at"] = None      # force the next refresh to actually run (not skipped as "cached")


def reconcile_codex(ctx: Context, state: State) -> str | None:
    """Capture a fresh/out-of-band ~/.codex into the matching account's home (ISO-B1).

    Plain `codex`/the GUI rotate ~/.codex in place; before we use or switch homes we capture those
    fresh creds into the owning account's home (it's the freshest copy), and adopt them as active if
    the user signed into a different known seat out-of-band. Unknown identities are left untouched.
    Returns the reconciled email, or None.
    """
    live = ctx.cred["codex"].get_live()
    if not live:
        return None
    em = ctx.cred["codex"].email_of(live)
    if not em or em not in state.accounts("codex"):
        return None
    changed = ctx.snapshot_get("codex", em) != live
    ctx.snapshot_set("codex", em, live)            # ~/.codex is the freshest copy for `em`
    dirty = False
    if changed:
        # creds rotated/re-logged out-of-band → clear any stale auth error so the app auto-recovers.
        _creds_refreshed(state, "codex", em)
        dirty = True
    if state.active("codex") != em:
        state.set_active("codex", em)              # honor an out-of-band login/switch
        dirty = True
    if dirty:
        state.save()
    return em


def remove(ctx: Context, state: State, tool: str, email: str) -> bool:
    """Remove a seat: delete its keychain snapshot and its state entry. Returns True if it existed."""
    ctx.snapshot_delete(tool, email)
    existed = state.remove_seat(tool, email)
    state.save()
    return existed


def _usage_pct(seat: dict, win: str):
    w = ((seat.get("usage") or {}).get("windows") or {}).get(win) or {}
    return w.get("used_pct")


def _seat_view(seat: dict, *, active: bool, at: datetime) -> dict[str, Any]:
    until = parse_iso(seat.get("limited_until"))
    limited = until is not None and until > at
    usage = seat.get("usage") or {}
    return {
        "email": seat["email"],
        "name": seat.get("name") or seat["email"].split("@")[0],
        "plan": seat.get("plan"),
        "active": active,
        "limited": limited,
        "limited_until": seat.get("limited_until") if limited else None,
        # needs-login only when the ACTIVE seat's LIVE creds fail. A non-active seat's cached access
        # token expiring (401) is normal — its refresh token still works when switched to — so we
        # don't cry "logged out"; it shows as ready with last-known usage.
        "needs_login": active and usage.get("error") == "unauthorized",
        "usage5h": _usage_pct(seat, "5h"),
        "usageWeek": _usage_pct(seat, "weekly"),
        "usage": usage or None,
        "added_at": seat.get("added_at"),
        "last_on_floor": seat.get("last_on_floor"),
    }


def _assign_statuses(seats: list[dict[str, Any]]) -> None:
    """Set exactly one status per seat (spec §5): active|ready|resting|queued|needs-login.

    'queued' (up next) applies only when EVERY seat of a provider is resting/needs-login — the
    soonest-to-reset is held as up-next instead of resting.
    """
    usable = [s for s in seats if not s["needs_login"]]
    resting_usable = [s for s in usable if s["limited"] and not s["active"]]
    all_capped = bool(usable) and all(s["limited"] for s in usable)
    soonest = None
    if all_capped:
        capped = [s for s in usable if s["limited"]]
        soonest = min(capped, key=lambda s: s["limited_until"] or "")["email"] if capped else None
    for s in seats:
        if s["needs_login"]:
            s["status"] = "needs-login"
        elif s["active"]:
            s["status"] = "active"
        elif all_capped and s["email"] == soonest:
            s["status"] = "queued"
        elif s["limited"]:
            s["status"] = "resting"
        else:
            s["status"] = "ready"


def list_seats(state: State, tool: str, at: datetime | None = None) -> list[dict[str, Any]]:
    at = at or now()
    active = state.active(tool)
    seats = [
        _seat_view(seat, active=(email == active), at=at)
        for email, seat in state.accounts(tool).items()
    ]
    _assign_statuses(seats)
    return seats


def status(ctx: Context, state: State, at: datetime | None = None) -> dict[str, Any]:
    """Full structured status for `status --json` and the menubar."""
    at = at or now()
    settings = state.settings()
    out: dict[str, Any] = {"settings": settings, "tools": {}}
    n_rest = n_ready = 0
    for tool in ("codex", "claude"):
        sel = choose(state, tool, at)
        seats = list_seats(state, tool, at)
        n_rest += sum(1 for s in seats if s["status"] in ("resting", "queued"))
        n_ready += sum(1 for s in seats if s["status"] in ("ready", "active"))
        out["tools"][tool] = {
            "active": state.active(tool),
            "plan_label": "CHATGPT BUSINESS" if tool == "codex" else "CLAUDE CODE",
            "seats": seats,
            "selection": {
                "email": sel.email, "available": sel.available,
                "all_limited": sel.all_limited,
                "unlocks_at": sel.unlocks_at.isoformat() if sel.unlocks_at else None,
            },
        }
    out["counts"] = {"resting": n_rest, "ready": n_ready}
    return out
