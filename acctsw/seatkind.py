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
``b>a``). A move back to the session's own home seat (the one it was launched on, whose login it
holds) is never a crossing: pass ``home=`` and ``b == home`` is exempt. A manual switch in the app
is the user's own choice: it answers the open questions onto its target (``chosen``) and leaves no
approval behind that could later authorize an automatic crossing.

State lives in ``tools.<tool>.crossings``::

    {"approvals": {"a>b": {"scope": "once"|"always", "at": iso, "via": "user"|"manual"}},
     "requests":  [{"from": a, "to": b, "session_id": str|None, "at": iso,
                    "reason": "limit"|"switch"}]}

A "once" approval is consumed by the switch that uses it (``consume``) and lapses after
``ONCE_TTL`` if no switch does: a yes given for one stuck session must not quietly authorize a
crossing hours later. "always" stays until ``deny`` revokes it.
"""
from __future__ import annotations

import re
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
    # Providers spell plans freely ("Team", "self_serve_business_prolite", "max_5x"), so judge by the
    # words in the name. A business word wins: a business plan that is also "pro" is still business.
    words = set(re.split(r"[^a-z0-9]+", plan.strip().lower()))
    if words & BUSINESS:
        return "business"
    if words & PERSONAL:
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


def crosses(state, tool: str, a: str | None, b: str | None, *, home: str | None = None) -> bool:
    """Whether moving a session from seat ``a`` to seat ``b`` changes kind (or can't tell).
    Never when ``b`` is the session's ``home``: the session belongs to that account."""
    if not a or not b or a == b or (home and b == home):
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


def approved(state, tool: str, a: str | None, b: str | None, at: datetime | None = None, *,
             home: str | None = None) -> bool:
    """True when ``a``→``b`` needs no question: not a crossing, or approved in this direction."""
    if state.settings()["crossing_auto"] or not crosses(state, tool, a, b, home=home):
        return True
    at = at or now()
    return _live((_peek(state, tool).get("approvals") or {}).get(_key(a, b)), at)


def request(state, tool: str, a: str, b: str, session_id=None, at: datetime | None = None, *,
            home: str | None = None, reason: str = "limit") -> bool:
    """Ask the user about ``a``→``b`` for one session. False when that exact question is open,
    or when there is nothing to ask (not a crossing)."""
    if not crosses(state, tool, a, b, home=home):
        return False
    store = _store(state, tool)
    sid = None if session_id is None else str(session_id)
    for r in store["requests"]:
        if r.get("from") == a and r.get("to") == b and r.get("session_id") == sid:
            return False
    store["requests"].append({"from": a, "to": b, "session_id": sid, "at": iso(at or now()),
                              "reason": reason})
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


def consume(state, tool: str, a: str | None, b: str | None, at: datetime | None = None, *,
            home: str | None = None) -> bool:
    """Spend the approval a switch ``a``→``b`` is about to use. Call it in the same locked write
    that commits the switch. Returns whether the switch may proceed: True for a non-crossing or a
    live approval ("once" is removed here, "always" stays), False when there is nothing to spend."""
    if state.settings()["crossing_auto"] or not crosses(state, tool, a, b, home=home):
        return True
    at = at or now()
    store = _peek(state, tool)
    record = (store.get("approvals") or {}).get(_key(a, b))
    if not _live(record, at):
        return False
    if record["scope"] == "once":
        del store["approvals"][_key(a, b)]
    return True


def chosen(state, tool: str, a: str, b: str) -> None:
    """The user moved ``a``→``b`` by hand: that answers the open questions in that direction and
    spends a "once" approval for it. Nothing is granted: a later automatic ``a``→``b`` still asks
    (an "always" the user gave stays)."""
    store = _peek(state, tool)
    record = (store.get("approvals") or {}).get(_key(a, b))
    if isinstance(record, dict) and record.get("scope") != "always":
        del store["approvals"][_key(a, b)]
    if isinstance(store.get("requests"), list):
        _drop_requests(store, a, b)


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
            out[key] = {"from": r["from"], "to": r["to"], "at": r["at"],
                        "reason": r.get("reason", "limit")}
    return sorted(out.values(), key=lambda r: (r["at"], r["from"], r["to"]))
