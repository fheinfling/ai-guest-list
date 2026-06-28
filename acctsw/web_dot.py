"""Single source of truth for the menu-bar dot state (Python).

Both the native glyph (via ``state["dot"]`` from the bridge) and the JS preview render the same
value. render.mjs keeps a JS copy only as a browser-preview fallback; a golden fixture
(tests/fixtures/dot_cases.json) is asserted by BOTH the python and node tests so they can't drift.
"""
from __future__ import annotations

from typing import Any


def _needs_hello(seat: dict) -> bool:
    return (seat.get("usage") or {}).get("error") == "unauthorized"


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
