"""Single source of truth for the menu-bar dot state (Python).

Both the native glyph (via ``state["dot"]`` from the bridge) and the JS preview render the same
value. render.mjs keeps a JS copy only as a browser-preview fallback; a golden fixture
(tests/fixtures/dot_cases.json) is asserted by BOTH the python and node tests so they can't drift.
"""
from __future__ import annotations

from typing import Any


def _all_seats(state: dict[str, Any]) -> list[dict]:
    tools = (state or {}).get("tools", {})
    return list((tools.get("codex") or {}).get("seats", [])) + \
        list((tools.get("claude") or {}).get("seats", []))


def dot_for(state: dict[str, Any]) -> str:
    """Aggregate menu-bar dot (spec §9): 'hello' (rose) · 'switched' (gold) · 'amber' · 'green'."""
    seats = _all_seats(state)
    if any(s.get("status") == "needs-login" for s in seats):
        return "hello"
    if state.get("recently_switched"):
        return "switched"
    # Keep the glyph's existing live-session behavior even though parked terminals now expose
    # their real health in the seat status (and therefore in the header counts).
    if any(s.get("status") in ("resting", "queued")
           and not (s.get("active") or s.get("in_session")) for s in seats):
        return "amber"
    return "green"


def door_for(state: dict[str, Any]) -> str:
    """The menu-bar door (icon handoff): 'open' onto the disco when a model is free, 'shut' when every
    seat is rate-limited. Ready seats and usable live credentials/terminals keep the door open.
    The door is shut ONLY when there are seats
    and none are free — a fresh install (no seats) stays 'open' (welcoming, matches the green dot)."""
    seats = _all_seats(state)
    free = any(s.get("status") in ("ready", "active") or
               (s.get("status") != "needs-login" and (s.get("active") or s.get("in_session")))
               for s in seats)
    return "open" if (free or not seats) else "shut"
