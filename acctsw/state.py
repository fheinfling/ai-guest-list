"""The non-secret state store (``~/.account-switcher/state.json``).

Holds only metadata — NEVER credentials (those live in the Keychain). Shape::

    {
      "version": 1,
      "tools": {
        "codex":  {"active": "<email>|null", "accounts": {"<email>": <Seat>}},
        "claude": {"active": "<email>|null", "accounts": {"<email>": <Seat>}}
      },
      "keys": {"<id>": <key-seat metadata>},
      "settings": {"auto_switch": true, "same_tool_only": true, "notify": true,
                   "restart_app": false, "celebrations": true, "supervise_shell": true,
                   "theme": "light", "confirm_key_switch": true, "key_fallback": false},
      "engine": {"last_writer": "<version>", "newest_writer": "<version>",
                 "min_writer": "<version>"}
    }

``engine`` is stamped by every save (see ``State.save``/``writer_block``). It is absent from
files written before the stamp existed, which every reader must keep accepting.

A ``Seat`` is::

    {"email": str, "name": str, "added_at": iso, "limited_until": iso|null,
     "usage": {<cached usage snapshot>}|null}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import TOOLS, __version__
from .engine import older_than, parse_version
from .errors import EngineTooOld
from .util import now, iso, write_json

STATE_VERSION = 1

# The oldest engine allowed to MOVE CREDENTIALS (switch seats, write a live login) in a store this
# engine has written. Every save records max(stored, this) in ``engine.min_writer``, so the floor
# only ever rises, even when an older-but-guarded engine saves afterwards. Bump it in the same
# change that makes an incompatible credential/Keychain format: a guarded engine below the floor
# then refuses to write its old format over the new one (the 1.1.x engine wrote Claude's item
# base64 and signed every session out). Engines from before this guard ignore the stamp — the
# app's wrapper/session drift warnings cover those. 1.3.1: raw-JSON Claude items (#63),
# per-account Claude homes (1.3.0) and isolated sign-in (1.3.1).
MIN_CREDENTIAL_WRITER = "1.3.1"

# Escape hatch for a deliberate downgrade. Moving credentials with an engine the store says is too
# old is exactly the corruption the guard exists to stop, so it is never implied.
ALLOW_OLD_ENGINE_ENV = "ACCTSW_ALLOW_OLD_ENGINE"


def _newer(a, b):
    """The newer of two version strings; an unparseable value never wins over a known one."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None:
        return b
    if pb is None:
        return a
    return a if pa >= pb else b


def writer_block(data: dict, *, version: str = __version__) -> str | None:
    """Why an engine at ``version`` must not move credentials in this store, or None if it may.

    Pure and cheap (one dict read) so every credential write can afford it."""
    stamp = data.get("engine") if isinstance(data, dict) else None
    if not isinstance(stamp, dict):
        return None  # written before the stamp existed: nothing newer has claimed this store
    floor = stamp.get("min_writer")
    if not older_than(version, floor):
        return None
    if os.environ.get(ALLOW_OLD_ENGINE_ENV) == "1":
        return None
    from pathlib import Path as _P
    where = _P(__file__).resolve().parent.parent
    newest = stamp.get("newest_writer") or floor
    return (f"this engine (acctsw {version}, from {where}) is older than the one that manages "
            f"your seats ({newest}), which needs at least {floor} to move logins safely, so it "
            f"won't switch seats or write credentials. Update it — `git pull` in that source "
            f"checkout, or reinstall AI Guest List and run `acctsw install` — then restart this "
            f"terminal. (A deliberate downgrade can set {ALLOW_OLD_ENGINE_ENV}=1.)")


def _label_from_email(email: str) -> str:
    """A short, human seat name from an email local-part (e.g. 'work@x.com' → 'Work').

    Keeps a ``+tag`` so alias accounts stay distinguishable (a.b+codex@x → 'A B +codex').
    """
    local = (email or "").split("@")[0]
    tag = ""
    if "+" in local:
        local, tag = local.split("+", 1)
        tag = f" +{tag}" if tag else ""
    parts = [p for p in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if p]
    base = " ".join(p.capitalize() for p in parts) or (local or "seat")
    return base + tag

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_switch": True,
    "same_tool_only": True,   # "keep me on the same tool"
    "notify": True,           # "tell me when it switches"
    "restart_app": False,     # "restart the Codex app after a swap"
    "celebrations": True,     # "little celebrations"
    "supervise_shell": True,  # wire terminal codex/claude through the supervised cx/cl launchers
    "strategy": "soonest_back",  # "soonest_back" | "most_headroom"
    "theme": "light",         # the design default
    "confirm_key_switch": True,  # consent for each hop onto metered API usage
    "key_fallback": False,    # metered seats must be explicitly opted into
}


def _empty() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "tools": {t: {"active": None, "accounts": {}} for t in TOOLS},
        "keys": {},  # id -> metadata; secrets remain in Keychain
        "settings": dict(DEFAULT_SETTINGS),
    }


@dataclass
class State:
    path: Path
    data: dict[str, Any] = field(default_factory=_empty)

    # --- load / save -------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path=path, data=_empty())
        data = json.loads(path.read_text(encoding="utf-8"))
        # forward-compatible defaults
        data.setdefault("version", STATE_VERSION)
        data.setdefault("tools", {})
        for t in TOOLS:
            data["tools"].setdefault(t, {"active": None, "accounts": {}})
            data["tools"][t].setdefault("active", None)
            data["tools"][t].setdefault("accounts", {})
        data.setdefault("keys", {})
        settings = dict(DEFAULT_SETTINGS)
        settings.update(data.get("settings") or {})
        data["settings"] = settings
        return cls(path=path, data=data)

    def save(self) -> None:
        # Monotonic revision: lets a UI reject a stale snapshot (an in-flight usage poll that read an
        # older state, then arrived after a newer mutation like an add) instead of clobbering the
        # fresh view. Bumped under the same lock every mutation holds.
        self.data["rev"] = int(self.data.get("rev", 0)) + 1
        # Who wrote this store. The floor and the newest writer only rise, so an older engine's
        # save (even a harmless settings toggle) can't erase a newer engine's claim.
        stamp = self.data.get("engine") if isinstance(self.data.get("engine"), dict) else {}
        self.data["engine"] = {
            "last_writer": __version__,
            "newest_writer": _newer(stamp.get("newest_writer"), __version__),
            "min_writer": _newer(stamp.get("min_writer"), MIN_CREDENTIAL_WRITER),
        }
        write_json(self.path, self.data, mode=0o600)

    def require_credential_writer(self) -> None:
        """Raise ``EngineTooOld`` before this engine moves credentials into a newer engine's store."""
        reason = writer_block(self.data)
        if reason:
            raise EngineTooOld(reason)

    # --- accessors ---------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def set_setting(self, key: str, value: Any) -> None:
        self.data["settings"][key] = value
        if key == "key_fallback" and not value:
            from . import handoff
            handoff.withdraw(self)  # same transaction: a late approval cannot revive paid use

    def _tool(self, tool: str) -> dict[str, Any]:
        if tool not in TOOLS:
            raise ValueError(f"unknown tool: {tool}")
        return self.data["tools"][tool]

    def accounts(self, tool: str) -> dict[str, Any]:
        return self._tool(tool)["accounts"]

    def active(self, tool: str) -> str | None:
        return self._tool(tool)["active"]

    def set_active(self, tool: str, email: str | None) -> None:
        self._tool(tool)["active"] = email

    def set_last_on_floor(self, tool: str, email: str, at_iso: str) -> None:
        seat = self.get_seat(tool, email)
        if seat is not None:
            seat["last_on_floor"] = at_iso

    def get_seat(self, tool: str, email: str) -> dict[str, Any] | None:
        return self.accounts(tool).get(email)

    def upsert_seat(self, tool: str, email: str, name: str | None = None,
                    plan: str | None = None) -> dict[str, Any]:
        accts = self.accounts(tool)
        seat = accts.get(email)
        if seat is None:
            seat = {
                "email": email,
                "name": name or _label_from_email(email),  # short human name, not the raw email
                "plan": plan,
                "added_at": iso(now()),
                "last_on_floor": None,
                "limited_until": None,
                "limit_source": None,   # "usage" (proactive) | "reactive" (caught mid-session)
                                        # | "hard" (tool-side billing banner; never cleared early)
                "usage": None,
            }
            accts[email] = seat
        else:
            if name:
                seat["name"] = name
            if plan:
                seat["plan"] = plan
            seat.setdefault("last_on_floor", None)
        return seat

    def remove_seat(self, tool: str, email: str) -> bool:
        accts = self.accounts(tool)
        existed = accts.pop(email, None) is not None
        if self.active(tool) == email:
            self.set_active(tool, None)
        return existed

    def set_limited_until(self, tool: str, email: str, until_iso: str | None,
                          source: str | None = None) -> None:
        seat = self.get_seat(tool, email)
        if seat is not None:
            seat["limited_until"] = until_iso
            seat["limit_source"] = source if until_iso else None

    def set_usage(self, tool: str, email: str, usage: dict | None) -> None:
        seat = self.get_seat(tool, email)
        if seat is not None:
            seat["usage"] = usage
