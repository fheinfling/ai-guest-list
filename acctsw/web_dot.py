"""Single source of truth for the menu-bar door state (Python).

Both the native glyph (via ``state["door"]`` from the bridge) and the JS preview render the same
value. render.mjs keeps a JS copy only as a browser-preview fallback; a golden fixture
(tests/fixtures/door_cases.json) is asserted by BOTH the python and node tests so they can't drift.
"""
from __future__ import annotations

from typing import Any


def _all_seats(state: dict[str, Any]) -> list[dict]:
    tools = (state or {}).get("tools", {})
    return list((tools.get("codex") or {}).get("seats", [])) + \
        list((tools.get("claude") or {}).get("seats", []))


def door_for(state: dict[str, Any]) -> str:
    """The menu-bar door (icon handoff): 'open' onto the disco when a model is free, 'shut' when every
    seat is unavailable. Only ready/active statuses keep the door open, matching the header counts,
    and never a signed-out seat whatever its status says: it has no login to take the floor with.
    The door is shut ONLY when there are seats
    and none are free — a fresh install (no seats) stays 'open' (welcoming)."""
    seats = _all_seats(state)
    free = any(s.get("status") in ("ready", "active") and not s.get("signed_out") for s in seats)
    return "open" if (free or not seats) else "shut"
