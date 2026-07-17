"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

from typing import Any

from . import __version__, build_number
from . import accounts as acct
from . import usage as usage_mod
from .claudetoken import claude_identity, looks_like_setup_token
from .context import Context
from .errors import AcctswError
from .switch import sync_back
from .switch import switch as do_switch
from .util import now, iso, parse_iso
from .web_dot import dot_for, door_for

# Settings the UI may toggle (boolean only) — a whitelist so a stray key can't clobber e.g. theme.
TOGGLE_KEYS = {"auto_switch", "notify", "restart_app", "celebrations", "same_tool_only"}

# Actions handled entirely by the native shell (app quit / run the chosen login in Terminal).
# Everything else goes through the bridge; the shell then acts on result fields (login/command).
NATIVE_ACTIONS = {"quit", "login", "settings"}

SWITCH_FRESH_SECONDS = 8  # how long the "just switched you" dot lingers


def is_native(action: str | None) -> bool:
    return action in NATIVE_ACTIONS


def snapshot_state(ctx: Context) -> dict[str, Any]:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    data["moved_note"] = state.data.get("moved_note")
    last = parse_iso(state.data.get("last_switch_at"))
    data["recently_switched"] = bool(last and (now() - last).total_seconds() < SWITCH_FRESH_SECONDS)
    data["dot"] = dot_for(data)  # single source of truth for the dot (JS + native both read this)
    data["door"] = door_for(data)  # shut/open door icon — same state feeds native glyph + web header
    data["app"] = {"version": __version__, "build": build_number()}  # shown in the settings sheet
    return data


def login_command(tool: str, method: str = "browser") -> str:
    """The Terminal command for an official sign-in. Kept in the bridge (not the UI) so the engine
    stays the source of truth for how each tool logs in. The new add-seat sub-view sends
    ``{tool, method}`` and the native side resolves the command here; ``method`` only affects Claude
    (browser sign-in vs. the long-lived ``setup-token``)."""
    if tool == "codex":
        return "codex login"
    if method == "token":
        return "claude setup-token"
    return "claude auth login"


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

        if action == "paste":
            tool = message["tool"]
            if tool == "claude":
                # Claude no-browser path: install a pasted setup-token. NOTE `claude auth status` is
                # useless as a validator here — it reads identity from ~/.claude.json, not the
                # keychain, and says loggedIn:true for garbage. The OAuth profile endpoint validates
                # the token itself and names ITS account, so it is both gate and identity source.
                token = looks_like_setup_token(message["blob"])
                if not token:
                    return {"ok": False,
                            "error": "that doesn't look like a setup-token (sk-ant-oat…)"}
                ident = claude_identity(token, user_agent=usage_mod.claude_user_agent(ctx.claude_bin))
                if ident is None:               # 401 / network / no email → reject; NOTHING WRITTEN
                    return {"ok": False,
                            "error": "Claude didn't accept that token — nothing was changed"}
                email, plan_raw = ident
                with ctx.locked():
                    state = ctx.load_state()
                    sync_back(ctx, state, "claude")          # INVARIANT: outgoing seat first
                    original = ctx.cred["claude"].get_live()  # back up AFTER sync_back, BEFORE write
                    candidate = ctx.cred["claude"].merge_token(original, token, plan_raw)
                    try:
                        ctx.cred["claude"].set_live(candidate)
                        seat = acct.add(ctx, state, "claude", name=message.get("name"), email=email)
                    except Exception:
                        # HARD SAFETY: restore exactly what was there — stock claude keeps working.
                        if original is not None:
                            ctx.cred["claude"].set_live(original)
                        else:
                            ctx.cred["claude"].clear_live()
                        raise
                return {"ok": True, "celebrate": True, "added": seat["email"],
                        "state": snapshot_state(ctx)}
            # codex no-browser path: install a pasted auth.json, then register it as a seat.
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
