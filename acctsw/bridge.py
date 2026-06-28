"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

import shutil
from typing import Any

from . import accounts as acct
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError
from .switch import switch as do_switch


def headroom_available() -> bool:
    return shutil.which("headroom") is not None


def snapshot_state(ctx: Context) -> dict[str, Any]:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    data["headroom_available"] = headroom_available()
    return data


def login_plan(tool: str) -> dict[str, Any]:
    """Describe how to add a seat (the native side runs the chosen flow in Terminal)."""
    if tool == "codex":
        return {"tool": "codex", "title": "who's joining the list?",
                "methods": [
                    {"id": "browser", "label": "ChatGPT sign-in", "command": "codex login"},
                    {"id": "paste", "label": "paste auth.json", "command": None},
                ]}
    return {"tool": "claude", "title": "who's joining the list?",
            "methods": [
                {"id": "browser", "label": "Claude.ai sign-in", "command": "claude auth login"},
                {"id": "token", "label": "setup-token", "command": "claude setup-token"},
            ]}


def handle(ctx: Context, message: dict) -> dict[str, Any]:
    action = (message or {}).get("action")
    try:
        if action in ("ready", "status", "dot"):
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "toggle":
            state = ctx.load_state()
            state.set_setting(str(message["key"]), bool(message["value"]))
            state.save()
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "switch":
            state = ctx.load_state()
            do_switch(ctx, state, message["tool"], message["email"])
            return {"ok": True, "celebrate": True, "state": snapshot_state(ctx)}

        if action == "remove":
            state = ctx.load_state()
            acct.remove(ctx, state, message["tool"], message["email"])
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "usage":
            state = ctx.load_state()
            usage_mod.refresh(ctx, state, message.get("tool"))
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "add":
            return {"ok": True, "login": login_plan(message["tool"])}

        if action == "headroom_install":
            from . import headroom
            return {"ok": True, "command": headroom.INSTALL_COMMAND,
                    "available": headroom.available()}

        if action == "snapshot":
            # called after the user completed the official login in Terminal
            state = ctx.load_state()
            seat = acct.add(ctx, state, message["tool"],
                            name=message.get("name"), email=message.get("email"))
            return {"ok": True, "celebrate": True, "added": seat["email"],
                    "state": snapshot_state(ctx)}

        if action in ("settings", "quit"):
            return {"ok": True}  # handled natively by the shell

        return {"ok": False, "error": f"unknown action: {action}"}
    except AcctswError as e:
        return {"ok": False, "error": str(e)}
    except KeyError as e:
        return {"ok": False, "error": f"missing field: {e}"}
