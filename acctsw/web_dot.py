"""Single source of truth for the menu-bar dot state (Python).

Both the native glyph (via ``state["dot"]`` from the bridge) and the JS preview render the same
value. render.mjs keeps a JS copy only as a browser-preview fallback; a golden fixture
(tests/fixtures/dot_cases.json) is asserted by BOTH the python and node tests so they can't drift.
"""
from __future__ import annotations

from typing import Any


def dot_for(state: dict[str, Any]) -> str:
    """Aggregate menu-bar dot (spec §9): 'hello' (rose) · 'switched' (gold) · 'amber' · 'green'."""
    tools = (state or {}).get("tools", {})
    seats = list((tools.get("codex") or {}).get("seats", [])) + \
        list((tools.get("claude") or {}).get("seats", []))
    if any(s.get("status") == "needs-login" for s in seats):
        return "hello"
    if state.get("recently_switched"):
        return "switched"
    if any(s.get("status") in ("resting", "queued") for s in seats):
        return "amber"
    return "green"
