"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

from . import accounts as acct
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError
from .switch import sync_back
from .switch import switch as do_switch
from .util import now, iso, parse_iso
from .web_dot import dot_for, door_for

# Settings the UI may toggle (boolean only) — a whitelist so a stray key can't clobber e.g. theme.
TOGGLE_KEYS = {"auto_switch", "headroom", "notify", "restart_app", "celebrations", "same_tool_only"}

# Actions handled entirely by the native shell (app quit / run the chosen login in Terminal).
# Everything else goes through the bridge; the shell then acts on result fields (login/command).
NATIVE_ACTIONS = {"quit", "login", "settings"}

SWITCH_FRESH_SECONDS = 8  # how long the "just switched you" dot lingers


def is_native(action: str | None) -> bool:
    return action in NATIVE_ACTIONS


def headroom_available() -> bool:
    from . import headroom
    return headroom.available()  # checks PATH and this app's venv bin


def headroom_savings() -> int | None:
    """Real compression figure from `headroom output-savings` (spec §8: don't fake a number)."""
    from . import headroom
    exe = headroom.headroom_path()
    if not exe:
        return None
    try:
        import re
        out = subprocess.run([exe, "output-savings"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)\s*%", out.stdout or "")
        return int(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def snapshot_state(ctx: Context) -> dict[str, Any]:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    data["headroom_available"] = headroom_available()
    data["headroom_savings"] = headroom_savings() if data["headroom_available"] else None
    data["moved_note"] = state.data.get("moved_note")
    last = parse_iso(state.data.get("last_switch_at"))
    data["recently_switched"] = bool(last and (now() - last).total_seconds() < SWITCH_FRESH_SECONDS)
    data["dot"] = dot_for(data)  # single source of truth for the dot (JS + native both read this)
    data["door"] = door_for(data)  # shut/open door icon — same state feeds native glyph + web header
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
            key = str(message["key"])
            if key not in TOGGLE_KEYS:
                return {"ok": False, "error": f"not a toggle: {key}"}
            val = bool(message["value"])
            # The Headroom toggle drives global app-managed routing (apply/remove + proxy). Run the
            # op FIRST, then persist the setting to match what actually happened — so the setting is
            # never out of step with reality. In particular a FAILED disable keeps the setting ON, so
            # the launch/health-check recovery paths keep trying instead of abandoning a still-routed
            # config pointed at a dying proxy.
            if key == "headroom":
                from . import headroom
                try:
                    if val:
                        ok, msg = headroom.global_enable(ctx.data_dir)
                        effective = bool(ok)           # enable failed → leave it OFF
                    else:
                        ok, msg = headroom.global_disable(ctx.data_dir)
                        effective = not ok             # disable failed → leave it ON (keep recovering)
                except Exception as e:                 # never let the toggle hang with no result
                    ok, msg, effective = False, f"{type(e).__name__}: {e}", (not val)
                with ctx.locked():
                    s = ctx.load_state(); s.set_setting("headroom", effective); s.save()
                if not ok:
                    err = f"couldn't enable Headroom: {msg}" if val else msg
                    return {"ok": False, "error": err, "state": snapshot_state(ctx)}
                return {"ok": True, "state": snapshot_state(ctx)}
            with ctx.locked():
                state = ctx.load_state()
                state.set_setting(key, val)
                state.save()
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "set_theme":
            val = message.get("value")
            if val not in ("light", "dark"):
                return {"ok": False, "error": f"bad theme: {val}"}
            with ctx.locked():
                state = ctx.load_state()
                state.set_setting("theme", val)
                state.save()
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "set_strategy":
            val = message.get("value")
            if val not in ("soonest_back", "most_headroom"):
                return {"ok": False, "error": f"bad strategy: {val}"}
            with ctx.locked():
                state = ctx.load_state()
                state.set_setting("strategy", val)
                state.save()
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "switch":
            with ctx.locked():
                state = ctx.load_state()
                do_switch(ctx, state, message["tool"], message["email"])
                state.data["last_switch_at"] = iso(now())
                state.save()
            return {"ok": True, "celebrate": True, "state": snapshot_state(ctx)}

        if action == "remove":
            with ctx.locked():
                state = ctx.load_state()
                acct.remove(ctx, state, message["tool"], message["email"])
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "usage":
            with ctx.locked():
                state = ctx.load_state()
                acct.reconcile_codex(ctx, state)   # capture a fresh/out-of-band ~/.codex into its home
                usage_mod.refresh(ctx, state, message.get("tool"))
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "add":
            return {"ok": True, "login": login_plan(message["tool"])}

        if action == "headroom_install":
            from . import headroom
            installed = headroom.ensure_installed()  # pip-install into this venv, in-process
            return {"ok": installed, "installed": installed,
                    "error": None if installed else "couldn't install headroom",
                    "state": snapshot_state(ctx)}

        if action == "paste":
            # codex no-browser path: install a pasted auth.json, then register it as a seat.
            tool = message["tool"]
            blob = message["blob"]
            # VALIDATE BEFORE WRITING: never overwrite the canonical auth.json with an unparseable
            # paste — that would break stock `codex` (violates "stock keeps working").
            email = ctx.cred[tool].email_of(blob)
            if not email:
                return {"ok": False, "error": "that doesn't look like a valid auth.json"}
            with ctx.locked():
                state = ctx.load_state()
                sync_back(ctx, state, tool)         # preserve the outgoing seat's rotated token
                ctx.cred[tool].set_live(blob)
                seat = acct.add(ctx, state, tool, name=message.get("name"), email=email)
            return {"ok": True, "celebrate": True, "added": seat["email"],
                    "state": snapshot_state(ctx)}

        if action == "snapshot":
            # called after the user completed the official login in Terminal
            with ctx.locked():
                state = ctx.load_state()
                seat = acct.add(ctx, state, message["tool"],
                                name=message.get("name"), email=message.get("email"))
            return {"ok": True, "celebrate": True, "added": seat["email"],
                    "state": snapshot_state(ctx)}

        return {"ok": False, "error": f"unknown action: {action}"}
    except AcctswError as e:
        return {"ok": False, "error": str(e)}
    except KeyError as e:
        return {"ok": False, "error": f"missing field: {e}"}
