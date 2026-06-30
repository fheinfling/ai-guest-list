"""Account selection: which seat should be on the floor.

Rules (from the plan):
- A seat is *available* if it has no ``limited_until`` or it is in the past.
- Prefer the currently active seat if it is available; else the first available seat.
- If ALL seats are limited, pick the one that unlocks soonest (min ``limited_until``) and report.

``exclude`` lets a caller drop specific seats from this one selection (e.g. the launcher excluding
a seat whose token just failed at runtime), WITHOUT persisting any "dead" flag — usage-poll auth
errors are NOT a reliable health signal here (a non-active seat routinely shows ``unauthorized`` from
a stale cached access token that is refreshed only when it becomes active).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .state import State
from .util import now, parse_iso


@dataclass
class Selection:
    email: str | None             # chosen seat (None if no seats exist)
    available: bool               # True if the chosen seat is usable right now
    unlocks_at: datetime | None   # when it unlocks, if currently limited
    all_limited: bool             # True if every seat is currently limited


def _limited_until(seat: dict, at: datetime) -> datetime | None:
    """Return the future unlock time for a seat, or None if it is available now."""
    until = parse_iso(seat.get("limited_until"))
    if until is None:
        return None
    return until if until > at else None


def choose(state: State, tool: str, at: datetime | None = None,
           exclude: frozenset | set | tuple = ()) -> Selection:
    at = at or now()
    accounts = {e: s for e, s in state.accounts(tool).items() if e not in exclude}
    if not accounts:
        return Selection(email=None, available=False, unlocks_at=None, all_limited=False)

    available = [e for e, s in accounts.items() if _limited_until(s, at) is None]
    if available:
        active = state.active(tool)
        chosen = active if active in available else available[0]
        return Selection(email=chosen, available=True, unlocks_at=None, all_limited=False)

    # All limited → soonest unlock wins.
    soonest_email = min(accounts, key=lambda e: _limited_until(accounts[e], at))
    return Selection(
        email=soonest_email,
        available=False,
        unlocks_at=_limited_until(accounts[soonest_email], at),
        all_limited=True,
    )
