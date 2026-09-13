"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

import hashlib
from typing import Any

from . import __version__, build_number
from . import accounts as acct
from . import identity as identity_mod
from . import install as install_mod
from . import paths as P
from . import session as session_mod
from . import usage as usage_mod
from .context import Context
from .errors import AcctswError, CannotIdentify, NoLiveCreds
from .selection import choose
from .switch import sync_back
from .switch import switch as do_switch
from .util import now, iso, parse_iso
from .web_dot import dot_for, door_for

# Settings the UI may toggle (boolean only) — a whitelist so a stray key can't clobber e.g. theme.
TOGGLE_KEYS = {
    "auto_switch", "notify", "restart_app", "celebrations", "same_tool_only", "supervise_shell",
}

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
    data["supervision"] = install_mod.supervision_status()
    # the signed-in-but-not-added codex account (if any) → drives the one-tap import affordance
    data["codex_live_unregistered"] = _codex_live_unregistered(ctx, state)
    data["rev"] = int(state.data.get("rev", 0))  # monotonic; the UI drops a snapshot older than one it applied
    return data


def _stored_usage_is_fresh_and_healthy(seat: dict | None) -> bool:
    """Whether a just-refreshed parked seat is safe to receive an automatic handoff.

    A successful endpoint status alone is insufficient: an older response can have been superseded,
    and a provider can return an OK envelope that still says the account is out.  Require fresh,
    non-stale numeric windows below the actual hard limit and no authoritative out marker.
    """
    usage = (seat or {}).get("usage") or {}
    fetched_at = parse_iso(usage.get("fetched_at"))
    if (usage.get("ok") is not True or usage.get("error") or usage.get("stale")
            or fetched_at is None
            or (now() - fetched_at).total_seconds() > usage_mod.MAX_TRUSTED_AGE_S
            or usage_mod.snapshot_says_out(usage)):
        return False
    windows = usage.get("windows") or {}
    pcts = [w.get("used_pct") for w in windows.values()
            if isinstance(w, dict) and isinstance(w.get("used_pct"), (int, float))]
    return bool(pcts) and all(pct < usage_mod.LIMIT_PCT for pct in pcts)


def _blob_digest(blob: str | None) -> str | None:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest() if blob else None


def _auto_switch_after_usage(ctx: Context, summary: dict[str, Any], *,
                             excluded_by_tool: dict[str, set[str]] | None = None) -> dict[str, Any] | None:
    """Make one app-owned, same-tool handoff after a confirmed active-seat limit.

    A supervised launcher owns an in-progress terminal session, so the menubar merely leaves its
    usage rest in state for that launcher to observe.  With no session, this revalidates a distinct
    candidate against a fresh provider response outside the state flock, then checks every decision
    input again under the flock immediately before the existing safe switch primitive runs.
    """
    excluded_by_tool = excluded_by_tool or {}
    for tool, tool_summary in summary.items():
        if not isinstance(tool_summary, dict):
            continue
        with ctx.locked():
            state = ctx.load_state()
            active = state.active(tool)
            active_seat = state.get_seat(tool, active) if active else None
            active_ok = active and tool_summary.get(active) == "ok"
            active_until = parse_iso((active_seat or {}).get("limited_until"))
            active_limited = (active_seat and active_seat.get("limit_source") in ("usage", "hard")
                              and active_until is not None and active_until > now())
            if (not state.settings().get("auto_switch", True) or not active_ok
                    or not active_limited or session_mod.active_session(ctx.data_dir, tool) is not None):
                continue
            excluded = excluded_by_tool.setdefault(tool, {active})
            # `choose` intentionally has a simple deterministic ordering.  Walk that ordering for
            # this one handoff: a same-person alias or an endpoint-backoff seat must not hide a
            # later, independent healthy seat.
            candidate = None
            while True:
                selection = choose(state, tool, exclude=excluded)
                proposed = selection.email if selection.available else None
                if not proposed:
                    break
                proposed_seat = state.get_seat(tool, proposed)
                if (proposed_seat and active_seat.get("account_id")
                        and proposed_seat.get("account_id") == active_seat["account_id"]):
                    excluded.add(proposed)
                    continue
                proposed_usage = (proposed_seat or {}).get("usage") or {}
                if proposed_usage.get("error") and not usage_mod._due(
                        proposed_usage, now(), usage_mod.USAGE_MIN_REFRESH_SECONDS, active=False):
                    excluded.add(proposed)
                    continue
                candidate = proposed
                break
            candidate_seat = state.get_seat(tool, candidate) if candidate else None
            if not candidate or candidate_seat is None:
                continue
            # A known equal fingerprint means one person/subscription quota.  Claude currently has
            # no such fingerprint, so unknown identities remain eligible rather than disabling every
            # Claude handoff.
            if (active_seat.get("account_id") and candidate_seat.get("account_id")
                    and active_seat["account_id"] == candidate_seat["account_id"]):
                continue
            candidate_added_at = candidate_seat.get("added_at")
            active_attempt = ((active_seat.get("usage") or {}).get("last_attempted_at"))

        # Keychain access is a subprocess on macOS.  Pin both snapshots with no state flock held;
        # the receipt revision below makes a state writer invalidate this decision before commit.
        outgoing_blob = ctx.cred[tool].get_live()
        outgoing_digest = _blob_digest(outgoing_blob)
        active_snapshot_digest = _blob_digest(ctx.snapshot_get(tool, active))
        candidate_digest_before = _blob_digest(ctx.snapshot_get(tool, candidate))
        if not outgoing_digest or not candidate_digest_before:
            continue
        if (active_seat.get("usage") or {}).get("credential_digest") != outgoing_digest:
            continue
        if tool == "codex" and ctx.cred[tool].email_of(outgoing_blob) != active:
            continue
        if tool == "claude" and outgoing_digest != active_snapshot_digest:
            continue

        # `refresh_live` has its own stale-result guards and does no network work under the lock.
        candidate_summary = usage_mod.refresh_live(ctx, tool, only=candidate, force=True)
        if candidate_summary.get(tool, {}).get(candidate) != "ok":
            excluded_by_tool.setdefault(tool, {active}).add(candidate)
            return _auto_switch_after_usage(ctx, summary, excluded_by_tool=excluded_by_tool)

        candidate_digest_after = _blob_digest(ctx.snapshot_get(tool, candidate))
        receipt_state = ctx.load_state()
        receipt_rev = receipt_state.data.get("rev")
        if candidate_digest_after is None or candidate_digest_after != candidate_digest_before:
            excluded_by_tool.setdefault(tool, {active}).add(candidate)
            return _auto_switch_after_usage(ctx, summary, excluded_by_tool=excluded_by_tool)

        # Claude's identity query may block; resolve before taking the handoff lock.  `do_switch`
        # verifies this exact blob again before it writes, so a login racing this probe is harmless.
        live_identity = identity_mod.claude_live_identity(ctx) if tool == "claude" else None
        outgoing_after = ctx.cred[tool].get_live()
        candidate_after = ctx.snapshot_get(tool, candidate)
        outgoing_unchanged = _blob_digest(outgoing_after) == outgoing_digest
        candidate_unchanged = _blob_digest(candidate_after) == candidate_digest_before
        with ctx.locked():
            state = ctx.load_state()
            current_active = state.active(tool)
            current_active_seat = state.get_seat(tool, current_active) if current_active else None
            current_candidate = state.get_seat(tool, candidate)
            current_selection = choose(state, tool, exclude=excluded)
            current_until = parse_iso((current_active_seat or {}).get("limited_until"))
            still_limited = (current_active_seat
                             and current_active_seat.get("limit_source") in ("usage", "hard")
                             and current_until is not None and current_until > now())
            active_receipt_matches = ((current_active_seat or {}).get("usage") or {}).get(
                "last_attempted_at"
            ) == active_attempt
            distinct = not (current_active_seat and current_candidate
                            and current_active_seat.get("account_id")
                            and current_candidate.get("account_id")
                            and current_active_seat["account_id"] == current_candidate["account_id"])
            if (not state.settings().get("auto_switch", True) or current_active != active
                    or state.data.get("rev") != receipt_rev
                    or session_mod.active_session(ctx.data_dir, tool) is not None
                    or not still_limited or not active_receipt_matches or current_candidate is None
                    or current_candidate.get("added_at") != candidate_added_at
                    or not outgoing_unchanged or not candidate_unchanged
                    or (current_candidate.get("usage") or {}).get("credential_digest") != candidate_digest_before
                    or not distinct or not current_selection.available
                    or current_selection.email != candidate
                    or not _stored_usage_is_fresh_and_healthy(current_candidate)):
                continue
            if tool == "codex" and ctx.cred[tool].email_of(outgoing_after) != active:
                continue
            if tool == "claude" and (live_identity is None or live_identity.blob != outgoing_after
                                      or live_identity.email != active):
                continue
            try:
                do_switch(ctx, state, tool, candidate, live_identity=live_identity)
                state.data["last_switch_at"] = iso(now())
                state.save()
            except (AcctswError, OSError) as exc:
                return {"status": "failed", "tool": tool, "from": active, "to": candidate,
                        "reason": "usage_limit", "error": str(exc)}
            return {"status": "switched", "tool": tool, "from": active, "to": candidate,
                    "reason": "usage_limit"}
    return None


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
                previous = bool(state.settings().get(key, True))
                if key == "supervise_shell":
                    try:
                        if val:
                            # The popover's repair affordance uses this same toggle. Restore missing
                            # wrappers too, while keeping the required rc operation explicit below.
                            install_mod.ensure_launchers(wire_rc=False)
                            install_mod.ensure_shell_setup()
                        else:
                            install_mod.remove_shell_setup()
                    except Exception as exc:
                        direction = "on" if val else "off"
                        detail = str(exc).strip() or exc.__class__.__name__
                        result = {
                            "ok": False,
                            "error": f"couldn't turn terminal supervision {direction}: {detail}",
                        }
                        try:
                            result["state"] = snapshot_state(ctx)
                        except Exception as status_exc:
                            status_detail = str(status_exc).strip() or status_exc.__class__.__name__
                            result["error"] += f"; couldn't refresh status: {status_detail}"
                        return result
                state.set_setting(key, val)
                try:
                    state.save()
                except Exception as exc:
                    rollback_error = ""
                    if key == "supervise_shell":
                        # The state write failed after the rc edit. Restore the shell to the persisted
                        # setting so the two user-facing sources of truth cannot silently disagree.
                        try:
                            if previous:
                                install_mod.ensure_shell_setup()
                            else:
                                install_mod.remove_shell_setup()
                        except Exception as rollback_exc:
                            rollback_detail = str(rollback_exc).strip() or rollback_exc.__class__.__name__
                            rollback_error = f"; shell rollback also failed: {rollback_detail}"
                    detail = str(exc).strip() or exc.__class__.__name__
                    result = {
                        "ok": False,
                        "error": f"couldn't save that setting: {detail}{rollback_error}",
                    }
                    try:
                        result["state"] = snapshot_state(ctx)
                    except Exception as status_exc:
                        status_detail = str(status_exc).strip() or status_exc.__class__.__name__
                        result["error"] += f"; couldn't refresh status: {status_detail}"
                    return result
            result = {"ok": True, "state": snapshot_state(ctx)}
            if key == "supervise_shell":
                result["message"] = f"terminal supervision is {'on' if val else 'off'}"
            return result

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
            scope = message.get("scope", "all")
            if scope not in ("active", "all"):
                return {"ok": False, "error": f"bad usage scope: {scope}"}
            # Claude auth status is needed only when the live Keychain bytes differ from the named
            # active snapshot.  Normal 30-second card refreshes avoid that potentially 30s process.
            # All credential/identity probes happen before the flock.
            before = ctx.load_state()
            requested_tool = message.get("tool")
            want_codex = requested_tool in (None, "codex")
            want_claude = requested_tool in (None, "claude")
            codex_session_sig = (session_mod.session_mtime_ns(ctx.data_dir, "codex")
                                  if want_codex else 0)
            codex_session_live = (session_mod.active_session(ctx.data_dir, "codex")
                                   if want_codex else None)
            reconcile_claude = False
            live_identity = None
            if want_claude and before.accounts("claude"):
                active_claude = before.active("claude")
                live_claude = ctx.cred["claude"].get_live()
                saved_claude = (ctx.snapshot_get("claude", active_claude)
                                if active_claude else None)
                if live_claude != saved_claude:
                    live_identity = identity_mod.claude_live_identity(ctx)
                    reconcile_claude = True
                claude_ua = usage_mod.claude_user_agent(ctx.claude_bin)
            else:
                claude_ua = P.CLAUDE_USER_AGENT_FALLBACK
            with ctx.locked():
                state = ctx.load_state()
                # A supervised Codex session owns its per-account home and rotates tokens there.
                # Copying the shared mirror over it from an observer poll could restore stale bytes.
                # Recheck the cheap heartbeat signature under the lock.  If a launcher started after
                # our slow PID check, leave its private home alone and let the next poll reconcile.
                if (want_codex and codex_session_live is None
                        and session_mod.session_mtime_ns(ctx.data_dir, "codex") == codex_session_sig):
                    acct.reconcile_codex(ctx, state)
                if reconcile_claude:
                    # The slow identity answer was resolved above and remains a no-op if its exact
                    # live bytes changed before this short reconciliation write.
                    acct.reconcile_claude(ctx, state, live_identity=live_identity)
            summary = usage_mod.refresh_live(
                ctx, requested_tool, only=message.get("only"),
                active_only=(scope == "active"), force=bool(message.get("force", False)),
                user_agent=claude_ua,
            )
            handoff = _auto_switch_after_usage(ctx, summary)
            return {"ok": True, "refresh": summary, "auto_switch": handoff,
                    "state": snapshot_state(ctx)}

        if action == "paste":
            # Codex no-browser path only: install a pasted auth.json, then register it as a seat.
            # (Claude has no paste path AND no no-browser add path at all — a `claude setup-token` is
            # an env-var inference credential that 403s on the OAuth endpoints and doesn't write the
            # Keychain login this app snapshots. Claude seats are added via browser sign-in only.)
            tool = message["tool"]
            # Reject Claude explicitly before the lock: sync_back would otherwise need its slow
            # identity subprocess, and the paste action has no supported Claude credential format.
            if tool != "codex":
                return {"ok": False, "error": "pasting credentials is codex-only"}
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
