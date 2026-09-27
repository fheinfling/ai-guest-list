"""Consent for metered seats crosses the launcher/app boundary through the existing store.

Every hop gets a new record, even when the setting explicitly auto-approves it. The record is a
snapshot of what the user is approving, so the app needs neither credentials nor a catalog fetch.
All read/modify/write operations use Context's existing flock and State.save revision discipline.
No lock is held while the launcher waits; callers poll resolve() and stop on a terminal status
or a missing record. The future menubar consumes pending() and honours each record's notify flag.

Unanswered requests expire after two minutes by default. PID plus process start-time follows the
session heartbeat's liveness rules, including its fallback when ps is unavailable. Dead/expired
records are no longer prompts; terminal records remain until resolve() consumes them exactly once.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from . import TOOLS, session
from .context import Context
from .pricing import CACHED_STALE_AFTER, RATE_NAMES, Model, Rate
from .procenv import same_proc_start
from .state import State
from .util import iso, now, parse_iso

DEFAULT_TIMEOUT = timedelta(minutes=2)


def withdraw(state: State, *, at: datetime | None = None) -> bool:
    """Withdraw unconsumed consent when paid use is switched off. Caller owns the flock/save.

    Keep this terminal outcome even if the setting is enabled again before the launcher polls.
    An approval still waiting to be consumed is not permission to outlive the kill switch.
    """
    changed = False
    for record in state.data.get("handoffs", {}).values():
        if record["status"] in ("pending", "approved"):
            record.update(status="withdrawn", decision_source="setting", finished_at=iso(at or now()))
            changed = True
    return changed


def _price(model: Model, at: datetime) -> dict[str, Any]:
    """Serialize only pricing fields, preserving Decimal strings, units and explicit unknowns.

    URLs, raw provider responses and arbitrary model metadata are deliberately not copied. Cached
    models retain verified_at from their live fetch, so age does not reset at confirmation time.
    """
    def rates(values: dict[str, Rate]) -> dict[str, dict[str, Any]]:
        return {name: {"status": rate.status,
                       "value": str(rate.value) if rate.value is not None else None,
                       "same_as": rate.same_as}
                for name in RATE_NAMES for rate in (values.get(name, Rate()),)}

    fetched = parse_iso(model.verified_at)
    age = max(0, (at - fetched).total_seconds()) if fetched is not None else None
    known = [model.rate(name) is not None for name in ("input", "output")]
    return {"status": "known" if all(known) else "partial" if any(known) else "unknown",
            "estimate": True, "currency": model.currency,
            "token_unit": "per_million_tokens", "request_unit": "per_request",
            "rates": rates(model.rates),
            "long_context_rates": rates({**model.rates, **model.long_context_rates})
            if model.long_context_rates else {},
            "long_context_threshold": model.long_context_threshold,
            "source": model.source, "verified_at": model.verified_at,
            "age_seconds": age,
            "potentially_stale": age is not None and age > CACHED_STALE_AFTER.total_seconds()}


def _refresh(state: State, at: datetime) -> bool:
    """Retire stale prompts under the same lock as answers, so a late click cannot win."""
    changed = False
    for record in state.data.get("handoffs", {}).values():
        if record["status"] in ("dead", "expired", "withdrawn"):
            continue
        pid = record["pid"]
        alive = session._alive(pid)
        if alive:
            start = session._proc_start(pid)
            stored = record["process_start"]
            alive = (start is None or bool(start) and (
                not stored or same_proc_start(stored, start)))
        if not alive:
            record["status"] = "dead"
        elif record["status"] == "pending" and at >= parse_iso(record["expires_at"]):
            record["status"] = "expired"
        else:
            continue
        record["finished_at"] = iso(at)
        changed = True
    return changed


def request(ctx: Context, tool: str, from_seat: str | None, key_seat_id: str, *,
            model: Model | None = None, session_id: str | None = None,
            pinned: bool = False,
            timeout: timedelta = DEFAULT_TIMEOUT, at: datetime | None = None) -> str:
    """Record one hop and return its id. Caller supplies a Model obtained from pricing.

    An unavailable catalog is represented by model=None and explicit unknown rates. Catalog
    fetching belongs outside this protocol (and outside the flock). This function records consent;
    choose_key remains the separate policy gate for automatic fallback.
    """
    if tool not in TOOLS:
        raise ValueError("Unknown tool")
    if any(value is not None and not isinstance(value, str) for value in (from_seat, session_id)):
        raise ValueError("Seat and session identifiers must be strings, not metadata objects")
    if timeout <= timedelta(0):
        raise ValueError("Confirmation timeout must be positive")
    at = at or now()
    pid = os.getpid()
    process_start = session._proc_start(pid) or ""
    with ctx.locked():
        state = ctx.load_state()
        seat = state.data["keys"].get(key_seat_id)
        if seat is None:
            raise ValueError("Unknown key seat")
        if seat["harness"] != tool:
            raise ValueError("Key seat cannot take over a session of another harness")
        wire = "responses" if tool == "codex" else "messages"
        if model is not None and (model.provider != seat["provider"] or model.id != seat["model"]
                                  or model.wire_api != wire):
            raise ValueError("Price model must match the key seat and harness")
        price = _price(model or Model(seat["provider"], seat["model"], seat["model"], wire), at)
        previous = state.accounts(tool).get(from_seat) or state.data["keys"].get(from_seat) or {}
        auto = not state.settings()["confirm_key_switch"]
        id = uuid4().hex
        # Explicit allowlist: never serialize a seat, session, environment or catalog wholesale.
        record = {"id": id, "tool": tool,
                  "from_seat": {"id": from_seat,
                                "label": previous.get("name") or previous.get("label") or from_seat},
                  "key_seat": {field: seat[field] for field in ("id", "label", "provider", "model")},
                  "price": price, "session_id": session_id, "pid": pid,
                  "process_start": process_start, "created_at": iso(at),
                  "expires_at": iso(at + timeout), "notify": state.settings()["notify"],
                  "status": "approved" if auto else "pending",
                  "decision_source": "setting" if auto else None,
                  "answered_at": iso(at) if auto else None}
        if pinned:
            record["pinned"] = True  # copy differs; the consent/withdrawal protocol does not
        _refresh(state, at)
        state.data.setdefault("handoffs", {})[id] = record
        state.save()
    return id


def answer(ctx: Context, id: str, approved: bool, *, at: datetime | None = None) -> bool:
    """First answer wins; return False for missing, answered, expired or dead requests."""
    if type(approved) is not bool:
        raise ValueError("Answer must be a boolean")
    at = at or now()
    with ctx.locked():
        state = ctx.load_state()
        changed = _refresh(state, at)
        record = state.data.get("handoffs", {}).get(id)
        accepted = record is not None and record["status"] == "pending"
        if accepted:
            record.update(status="approved" if approved else "declined", decision_source="user",
                          answered_at=iso(at))
        if changed or accepted:
            state.save()
        return accepted


def pending(ctx: Context, tool: str | None = None, *,
            at: datetime | None = None) -> list[dict[str, Any]]:
    """Read live unanswered prompts in creation order, retiring expired/dead ones."""
    with ctx.locked():
        state = ctx.load_state()
        if _refresh(state, at or now()):
            state.save()
        return sorted((r for r in state.data.get("handoffs", {}).values()
                       if r["status"] == "pending" and (tool is None or r["tool"] == tool)),
                      key=lambda r: (r["created_at"], r["id"]))


def resolve(ctx: Context, id: str, *, at: datetime | None = None,
            require_enabled: bool = False) -> dict[str, Any] | None:
    """Poll without blocking: pending remains; a terminal record is returned and removed once.

    None means missing/already consumed, never approval. Only status='approved' permits the hop.
    Returned records belong to this read, so caller mutations cannot alter persisted state.
    Launchers require the live fallback setting too; the consent protocol alone remains usable
    independently of selection policy. This also catches settings written outside set_setting().
    """
    with ctx.locked():
        state = ctx.load_state()
        at = at or now()
        changed = (withdraw(state, at=at) if require_enabled
                   and not state.settings()["key_fallback"] else False)
        changed = _refresh(state, at) or changed
        records = state.data.get("handoffs", {})
        record = records.get(id)
        if record is not None and record["status"] != "pending":
            del records[id]
            changed = True
        if changed:
            state.save()
        return record
