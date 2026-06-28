"""The non-secret state store (``~/.account-switcher/state.json``).

Holds only metadata — NEVER credentials (those live in the Keychain). Shape::

    {
      "version": 1,
      "tools": {
        "codex":  {"active": "<email>|null", "accounts": {"<email>": <Seat>}},
        "claude": {"active": "<email>|null", "accounts": {"<email>": <Seat>}}
      },
      "settings": {"auto_switch": true, "same_tool_only": true, "headroom": false,
                   "notify": true, "restart_app": false, "celebrations": true, "theme": "dark"}
    }

A ``Seat`` is::

    {"email": str, "name": str, "added_at": iso, "limited_until": iso|null,
     "usage": {<cached usage snapshot>}|null}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import TOOLS
from .util import now, iso, write_json

STATE_VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_switch": True,
    "same_tool_only": True,   # "keep me on the same tool"
    "headroom": False,        # "save credit" toggle
    "notify": True,           # "tell me when it switches"
    "restart_app": False,     # "restart Codex after a swap"
    "celebrations": True,     # "little celebrations"
    "theme": "dark",
}


def _empty() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "tools": {t: {"active": None, "accounts": {}} for t in TOOLS},
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
        data = json.loads(path.read_text())
        # forward-compatible defaults
        data.setdefault("version", STATE_VERSION)
        data.setdefault("tools", {})
        for t in TOOLS:
            data["tools"].setdefault(t, {"active": None, "accounts": {}})
            data["tools"][t].setdefault("active", None)
            data["tools"][t].setdefault("accounts", {})
        settings = dict(DEFAULT_SETTINGS)
        settings.update(data.get("settings") or {})
        data["settings"] = settings
        return cls(path=path, data=data)

    def save(self) -> None:
        write_json(self.path, self.data, mode=0o600)

    # --- accessors ---------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def set_setting(self, key: str, value: Any) -> None:
        self.data["settings"][key] = value

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

    def get_seat(self, tool: str, email: str) -> dict[str, Any] | None:
        return self.accounts(tool).get(email)

    def upsert_seat(self, tool: str, email: str, name: str | None = None) -> dict[str, Any]:
        accts = self.accounts(tool)
        seat = accts.get(email)
        if seat is None:
            seat = {
                "email": email,
                "name": name or email,
                "added_at": iso(now()),
                "limited_until": None,
                "limit_source": None,   # "usage" (proactive) | "reactive" (caught mid-session)
                "usage": None,
            }
            accts[email] = seat
        elif name:
            seat["name"] = name
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
