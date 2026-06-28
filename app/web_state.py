"""Pure-Python mirror of render.mjs `dotState` — used to pick the menu-bar glyph natively.

Kept in lock-step with app/web/render.mjs (the JS is the source of truth for the popover; this is
only for the status-item title). Unit-tested in tests/test_web_state.py.
"""
from __future__ import annotations

from typing import Any


def _needs_hello(seat: dict) -> bool:
    usage = seat.get("usage") or {}
    return usage.get("error") == "unauthorized"


def dot_for(state: dict[str, Any]) -> str:
    """Return one of: 'switched' | 'hello' | 'resting' | 'fresh'."""
    tools = (state or {}).get("tools", {})
    seats = list((tools.get("codex") or {}).get("seats", [])) + \
        list((tools.get("claude") or {}).get("seats", []))
    if state.get("recently_switched"):
        return "switched"
    if any(_needs_hello(s) for s in seats):
        return "hello"
    if any(s.get("active") and s.get("limited") for s in seats):
        return "resting"
    return "fresh"
