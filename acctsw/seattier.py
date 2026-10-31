"""Plan tiers (Max, Pro, Team, ...) and moving a running session between them in place.

A hop between seats of different tiers is not taken in place unless the user said so: models and
limits differ across tiers, and Claude Code picked its model for the home seat's. Without a yes the
hop goes by restart, exactly as it always did. Three things allow it:

- the setting ``in_place_cross_tier`` ("always for all tiers");
- an approval for that pair of tiers, ``tools.<tool>.tier_approvals["max|team"] = {"at": iso}``
  (tiers are symmetric, so the key is the sorted pair, not a direction);
- equal tiers, or either tier unknown (nothing to compare).

The question is asked once per pair: ``tools.<tool>.tier_requests["max|team"] = {"at": iso,
"status": "open"|"approved"|"denied"}``. Answers are remembered even if the global setting is
later turned off, so neither the launcher nor the UI asks again.
A tier approval never implies a business/personal crossing approval (``seatkind``).
"""
from __future__ import annotations

import json
from datetime import datetime

from .util import iso, now

SETTING = "in_place_cross_tier"


def tier(seat_or_blob) -> str | None:
    """The lowercase plan tier of a seat dict (its ``plan``), a Keychain blob (its
    ``subscriptionType``) or a bare plan string. None when unknown."""
    raw = None
    if isinstance(seat_or_blob, dict):
        raw = seat_or_blob.get("plan")
    elif isinstance(seat_or_blob, str):
        text = seat_or_blob.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            raw = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
        else:
            raw = text
    return raw.strip().lower() if isinstance(raw, str) and raw.strip() else None


def pair(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _peek(state, tool: str, name: str) -> dict:
    store = state.data["tools"].get(tool, {}).get(name)
    return store if isinstance(store, dict) else {}


def _store(state, tool: str, name: str) -> dict:
    tools = state.data["tools"][tool]
    if not isinstance(tools.get(name), dict):
        tools[name] = {}
    return tools[name]


def allowed(state, tool: str, a_tier: str | None, b_tier: str | None) -> bool:
    """Whether an in-place hop between these tiers needs no question."""
    if not a_tier or not b_tier or a_tier == b_tier:
        return True
    if state.settings().get(SETTING) is True:
        return True
    return isinstance(_peek(state, tool, "tier_approvals").get(pair(a_tier, b_tier)), dict)


def request(state, tool: str, a_tier: str | None, b_tier: str | None,
            at: datetime | None = None) -> bool:
    """Ask once about this pair. False when it is already allowed, open or answered "no"."""
    if allowed(state, tool, a_tier, b_tier) or not a_tier or not b_tier:
        return False
    store = _store(state, tool, "tier_requests")
    key = pair(a_tier, b_tier)
    if key in store:
        return False
    store[key] = {"at": iso(at or now()), "status": "open"}
    return True


def approve(state, tool: str, a_tier: str, b_tier: str, at: datetime | None = None) -> None:
    """"Always for this pair": record it and close the question."""
    key = pair(a_tier, b_tier)
    _store(state, tool, "tier_approvals")[key] = {"at": iso(at or now())}
    _store(state, tool, "tier_requests").pop(key, None)


def approve_all(state) -> None:
    """"Always for all tiers": the setting; every open question is answered."""
    state.set_setting(SETTING, True)
    for tool in state.data["tools"]:
        for rec in _peek(state, tool, "tier_requests").values():
            if isinstance(rec, dict) and rec.get("status") == "open":
                rec["status"] = "approved"


def deny(state, tool: str, a_tier: str, b_tier: str, at: datetime | None = None) -> None:
    """"No" for this pair: remembered, and any approval for it is revoked."""
    key = pair(a_tier, b_tier)
    _store(state, tool, "tier_approvals").pop(key, None)
    _store(state, tool, "tier_requests")[key] = {"at": iso(at or now()), "status": "denied"}


def pending(state, tool: str) -> list[dict]:
    """Open questions, oldest first, as ``{"a", "b", "at"}`` (the sorted pair of tiers)."""
    out = []
    for key, rec in _peek(state, tool, "tier_requests").items():
        a, _, b = key.partition("|")
        if (isinstance(rec, dict) and rec.get("status") == "open" and a and b
                and not allowed(state, tool, a, b)):
            out.append({"a": a, "b": b, "at": rec.get("at")})
    return sorted(out, key=lambda r: (str(r["at"]), r["a"], r["b"]))
