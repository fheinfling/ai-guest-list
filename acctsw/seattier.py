"""Plan tiers (Max, Pro, Team, ...) and moving a running session between them in place.

Since the user decision on 2026-11-20, ``State`` forces ``in_place_cross_tier`` True, even when
an older store says False. Cross-tier hops therefore need no per-pair question, and old open or
denied requests cannot block them. Models and limits can still differ between plans.

The legacy pair approval/request machinery remains for compatibility: approvals live in
``tools.<tool>.tier_approvals["max|team"] = {"at": iso}``, and requests in
``tools.<tool>.tier_requests["max|team"] = {"at": iso, "status": "open"|"approved"|"denied"}``.
Pairs are symmetric. With the forced setting, ``request`` never opens a question and ``pending``
never returns one. Business/personal crossing approval (``seatkind``) remains separate.
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
