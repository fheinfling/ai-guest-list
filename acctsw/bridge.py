"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

from typing import Any

from . import __version__, build_number
from . import accounts as acct
from . import identity as identity_mod
from . import paths as P
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError, CannotIdentify, NoLiveCreds
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


def _codex_live_unregistered(ctx: Context, state) -> dict[str, Any] | None:
    """The codex account currently signed in on this Mac (``~/.codex/auth.json``) that ISN'T a seat yet.

    Surfaced so the add-seat view can offer a one-tap "use the login you already have" import — the
    easiest add path, no auth.json paste. None when nothing is live or the live account is already a
    seat. Best-effort + defensive: a broken/unreadable auth.json must never break the whole snapshot."""
    try:
        live = ctx.cred["codex"].get_live()
        if not live:
            return None
        email = ctx.cred["codex"].email_of(live)
        if not email or email in state.accounts("codex"):
            return None
        return {"email": email}
    except Exception:
        return None


def snapshot_state(ctx: Context) -> dict[str, Any]:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    data["moved_note"] = state.data.get("moved_note")
    last = parse_iso(state.data.get("last_switch_at"))
    data["recently_switched"] = bool(last and (now() - last).total_seconds() < SWITCH_FRESH_SECONDS)
    data["dot"] = dot_for(data)  # single source of truth for the dot (JS + native both read this)
    data["door"] = door_for(data)  # shut/open door icon — same state feeds native glyph + web header
    data["app"] = {"version": __version__, "build": build_number()}  # shown in the settings sheet
    # the signed-in-but-not-added codex account (if any) → drives the one-tap import affordance
    data["codex_live_unregistered"] = _codex_live_unregistered(ctx, state)
    data["rev"] = int(state.data.get("rev", 0))  # monotonic; the UI drops a snapshot older than one it applied
    return data


# The official browser sign-in sub-command per tool (the binary is prepended by the launcher with its
# ABSOLUTE path — see terminal.resolve_login_command — so a GUI app's minimal PATH can't hide the CLI).
# Single source of truth for "how each tool logs in", shared by login_command (logical string) and the
# terminal launcher (absolute argv).
LOGIN_SUBCMD = {"codex": ["login"], "claude": ["auth", "login"]}


def login_command(tool: str, method: str = "browser") -> str:
    """The logical Terminal command for an official browser sign-in, e.g. ``"codex login"``. Kept in
    the bridge (not the UI) so the engine stays the source of truth for how each tool logs in.
    ``method`` is reserved but currently unused: both tools' only Terminal path is the browser sign-in
    (codex's no-browser option pastes an auth.json / imports the current login in-app; Claude has no
    working no-browser path — `claude setup-token` produces an env-var token, not the Keychain login
    this app snapshots). The actual launch resolves the CLI's absolute path via
    ``terminal.resolve_login_command``; this string is the logical form used in tests/messages."""
    return " ".join([tool, *LOGIN_SUBCMD[tool]])


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
            tool = message["tool"]
            # Claude's blob has no email, and its official identity probe can wait 30 seconds.
            # Resolve the blob/email pair before taking the cross-process flock; reconcile_claude
            # rechecks the exact blob under the lock before trusting the answer.
            live_identity = (
                identity_mod.claude_live_identity(ctx) if tool == "claude" else None
            )
            with ctx.locked():
                state = ctx.load_state()
                do_switch(ctx, state, tool, message["email"], live_identity=live_identity)
                state.data["last_switch_at"] = iso(now())
                state.save()
            return {"ok": True, "celebrate": True, "state": snapshot_state(ctx)}

        if action == "remove":
            with ctx.locked():
                state = ctx.load_state()
                acct.remove(ctx, state, message["tool"], message["email"])
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "usage":
            # Both Claude helper commands are subprocesses. Compute their answers before the flock:
            # auth status can stall for 30s and --version for 10s, neither of which may freeze the
            # menubar or another launcher. Passing the fallback when no Claude seat exists also keeps
            # a seat added in the tiny read→lock race from spawning --version under the lock.
            before = ctx.load_state()
            if before.accounts("claude"):
                live_identity = identity_mod.claude_live_identity(ctx)
                claude_ua = usage_mod.claude_user_agent(ctx.claude_bin)
            else:
                live_identity = identity_mod.ClaudeLiveIdentity(
                    blob=ctx.cred["claude"].get_live(), email=None
                )
                claude_ua = P.CLAUDE_USER_AGENT_FALLBACK
            with ctx.locked():
                state = ctx.load_state()
                acct.reconcile_codex(ctx, state)   # capture a fresh/out-of-band ~/.codex into its home
                # Claude identity comes from auth status, not its blob; the slow answer was resolved
                # above and remains a no-op if unknown or if the live bytes changed meanwhile.
                acct.reconcile_claude(ctx, state, live_identity=live_identity)
                usage_mod.refresh(ctx, state, message.get("tool"), user_agent=claude_ua)
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "paste":
            # Codex no-browser path only: install a pasted auth.json, then register it as a seat.
            # (Claude has no paste path AND no no-browser add path at all — a `claude setup-token` is
            # an env-var inference credential that 403s on the OAuth endpoints and doesn't write the
            # Keychain login this app snapshots. Claude seats are added via browser sign-in only.)
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

        if action == "import_current":
            # One-tap "use the login you already have": register the codex account currently signed in
            # on this Mac (~/.codex/auth.json) as a seat, with NO paste and NO browser dance. acct.add
            # snapshots the live creds directly (it does NOT overwrite them), so — unlike `paste` — there
            # is nothing to sync-back: the live account IS the one being added. Codex-only (Claude's live
            # creds carry no derivable email; it has no no-browser add path).
            tool = message["tool"]
            if tool != "codex":
                return {"ok": False, "error": "importing the current login is codex-only"}
            # Read the live creds, derive the email, and snapshot — all under ONE lock, passing the
            # exact blob to acct.add. That closes the TOCTOU where acct.add's own second get_live()
            # could read a DIFFERENT account (an out-of-band `codex login` mid-flow) than the email
            # was derived from, storing account C's creds under account A's label.
            with ctx.locked():
                live = ctx.cred[tool].get_live()
                if not live:
                    return {"ok": False, "error": "you're not signed in to codex on this Mac yet"}
                email = ctx.cred[tool].email_of(live)
                if not email:
                    return {"ok": False, "error": "couldn't read the codex account you're signed into"}
                state = ctx.load_state()
                if email in state.accounts(tool):
                    return {"ok": False, "error": f"{email} is already on the list"}
                seat = acct.add(ctx, state, tool, name=message.get("name"), email=email, blob=live)
            return {"ok": True, "celebrate": True, "added": seat["email"],
                    "state": snapshot_state(ctx)}

        if action == "snapshot":
            # called after the user completed the official login in Terminal
            tool = message["tool"]
            email = message.get("email")
            live = None
            if tool == "claude" and not email:
                # The Claude blob cannot identify itself. Resolve auth status before the flock and
                # pass the exact bytes into add(), closing both the 30s lock stall and a login TOCTOU.
                resolved = identity_mod.claude_live_identity(ctx)
                if not resolved.blob:
                    raise NoLiveCreds(
                        "no live claude credentials — sign in with the official tool first"
                    )
                if not resolved.email:
                    raise CannotIdentify("could not determine the account email for claude")
                live, email = resolved.blob, resolved.email
            with ctx.locked():
                state = ctx.load_state()
                seat = acct.add(ctx, state, tool, name=message.get("name"),
                                email=email, blob=live)
            return {"ok": True, "celebrate": True, "added": seat["email"],
                    "state": snapshot_state(ctx)}

        return {"ok": False, "error": f"unknown action: {action}"}
    except AcctswError as e:
        return {"ok": False, "error": str(e)}
    except KeyError as e:
        return {"ok": False, "error": f"missing field: {e}"}
