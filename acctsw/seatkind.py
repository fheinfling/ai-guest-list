"""Business vs personal seats: an automatic move between the two needs the user's explicit yes.

A seat's kind comes only from its stored plan label (``Seat.plan``, derived from the credential's
own plan claim — Claude's ``subscriptionType``, ChatGPT's ``chatgpt_plan_type``), so every install
decides it the same way from data it already has:

- Team, Enterprise (and ChatGPT Business, Edu) → ``"business"``
- Free, Pro, Max (and ChatGPT Plus) → ``"personal"``
- anything else, including no plan at all → ``"unknown"``

Every AUTOMATIC switch — in place or by restart — that moves a session between kinds, or involves
an unknown kind, is a crossing. Two unknown seats cross too: unknown is never proof of sameness.
A crossing is only taken with an approval for exactly that direction (``a>b`` never approves
``b>a``). A manual switch in the app is the user's own choice and records its approval itself.

State lives in ``tools.<tool>.crossings``::

    {"approvals": {"a>b": {"scope": "once"|"always", "at": iso, "via": "user"|"manual"}},
     "requests":  [{"from": a, "to": b, "session_id": str|None, "at": iso}]}

A "once" approval is consumed by the switch that uses it (``consume``) and lapses after
``ONCE_TTL`` if no switch does: a yes given for one stuck session must not quietly authorize a
crossing hours later. "always" stays until ``deny`` revokes it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .util import iso, now, parse_iso

BUSINESS = frozenset({"team", "enterprise", "business", "edu", "education"})
PERSONAL = frozenset({"free", "pro", "max", "plus"})
SCOPES = ("once", "always")
ONCE_TTL = timedelta(hours=1)
# A request nobody answered for this long belongs to a session that has long moved on.
REQUEST_TTL = timedelta(hours=12)


def kind(seat) -> str:
    """``"business"`` | ``"personal"`` | ``"unknown"`` for a seat dict (or a bare plan string)."""
    plan = seat.get("plan") if isinstance(seat, dict) else seat
    if not isinstance(plan, str):
        return "unknown"
    plan = plan.strip().lower()
    if plan in BUSINESS:
        return "business"
    if plan in PERSONAL:
        return "personal"
    return "unknown"


def _key(a: str, b: str) -> str:
    return f"{a}>{b}"


def _store(state, tool: str) -> dict:
    tools = state.data["tools"][tool]
    store = tools.get("crossings")
    if not isinstance(store, dict):
        store = tools["crossings"] = {}
    if not isinstance(store.get("approvals"), dict):
        store["approvals"] = {}
    if not isinstance(store.get("requests"), list):
        store["requests"] = []
    return store


def _peek(state, tool: str) -> dict:
    """Read-only view: never adds the empty skeleton to a state that has none."""
    store = state.data["tools"].get(tool, {}).get("crossings")
    return store if isinstance(store, dict) else {}


def crosses(state, tool: str, a: str | None, b: str | None) -> bool:
    """Whether moving a session from seat ``a`` to seat ``b`` changes kind (or can't tell)."""
    if not a or not b or a == b:
        return False
    ka = kind(state.get_seat(tool, a) or {})
    kb = kind(state.get_seat(tool, b) or {})
    return ka == "unknown" or kb == "unknown" or ka != kb


def _live(record, at: datetime) -> bool:
    if not isinstance(record, dict) or record.get("scope") not in SCOPES:
        return False
    if record["scope"] == "always":
        return True
    granted = parse_iso(record.get("at"))
    return granted is not None and at - granted < ONCE_TTL


def approved(state, tool: str, a: str | None, b: str | None, at: datetime | None = None) -> bool:
    """True when ``a``→``b`` needs no question: not a crossing, or approved in this direction."""
    if not crosses(state, tool, a, b):
        return True
    at = at or now()
    return _live((_peek(state, tool).get("approvals") or {}).get(_key(a, b)), at)


def request(state, tool: str, a: str, b: str, session_id=None, at: datetime | None = None) -> bool:
    """Ask the user about ``a``→``b`` for one session. False when that exact question is open."""
    store = _store(state, tool)
    sid = None if session_id is None else str(session_id)
    for r in store["requests"]:
        if r.get("from") == a and r.get("to") == b and r.get("session_id") == sid:
            return False
    store["requests"].append({"from": a, "to": b, "session_id": sid, "at": iso(at or now())})
    return True


def _drop_requests(store: dict, a: str, b: str) -> None:
    store["requests"] = [r for r in store["requests"]
                         if not (r.get("from") == a and r.get("to") == b)]


def approve(state, tool: str, a: str, b: str, scope: str, at: datetime | None = None, *,
            via: str = "user") -> None:
    """Approve ``a``→``b`` only (directional). Answers every open request for that direction."""
    if scope not in SCOPES:
        raise ValueError(f"bad crossing scope: {scope}")
    store = _store(state, tool)
    current = store["approvals"].get(_key(a, b))
    if not (scope == "once" and isinstance(current, dict) and current.get("scope") == "always"):
        store["approvals"][_key(a, b)] = {"scope": scope, "at": iso(at or now()), "via": via}
    _drop_requests(store, a, b)


def deny(state, tool: str, a: str, b: str) -> None:
    """No to ``a``→``b``: close its requests and revoke any approval in that direction."""
    store = _store(state, tool)
    store["approvals"].pop(_key(a, b), None)
    _drop_requests(store, a, b)


def consume(state, tool: str, a: str | None, b: str | None, at: datetime | None = None) -> bool:
    """Spend the approval a switch ``a``→``b`` is about to use. Call it in the same locked write
    that commits the switch. Returns whether the switch may proceed: True for a non-crossing or a
    live approval ("once" is removed here, "always" stays), False when there is nothing to spend."""
    if not crosses(state, tool, a, b):
        return True
    at = at or now()
    store = _peek(state, tool)
    record = (store.get("approvals") or {}).get(_key(a, b))
    if not _live(record, at):
        return False
    if record["scope"] == "once":
        del store["approvals"][_key(a, b)]
    return True


def withdraw(state, tool: str, session_id) -> None:
    """A session that ended (or no longer needs the move) retracts its open questions."""
    store = _peek(state, tool)
    if isinstance(store.get("requests"), list):
        sid = str(session_id)
        store["requests"] = [r for r in store["requests"] if r.get("session_id") != sid]


def pending(state, tool: str, at: datetime | None = None) -> list[dict]:
    """Open questions, one per direction (several sessions may wait on the same move)."""
    at = at or now()
    out: dict[str, dict] = {}
    for r in _peek(state, tool).get("requests") or []:
        if not isinstance(r, dict) or not r.get("from") or not r.get("to"):
            continue
        asked = parse_iso(r.get("at"))
        if asked is None or at - asked > REQUEST_TTL:
            continue
        if approved(state, tool, r["from"], r["to"], at):
            continue  # already answered (or the plans changed so it no longer crosses)
        key = _key(r["from"], r["to"])
        if key not in out or r["at"] < out[key]["at"]:
            out[key] = {"from": r["from"], "to": r["to"], "at": r["at"]}
    return sorted(out.values(), key=lambda r: (r["at"], r["from"], r["to"]))
