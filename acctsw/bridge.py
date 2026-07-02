"""Bridge between the WKWebView UI (JS) and the engine.

``handle(ctx, message)`` is a PURE dispatch function (no pyobjc, no I/O beyond the engine) so the
entire UI action surface is unit-testable. The native menubar shell (app/menubar.py) just forwards
WKScriptMessage dicts here and pushes the returned ``state`` back into the web view.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Any

from . import __version__, build_number
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


def _run_async(fn) -> None:
    """Fire-and-forget on a daemon thread. Overridden in tests to run inline for determinism."""
    threading.Thread(target=fn, daemon=True).start()


def _restart_proxy_quietly(headroom, ctx, prev_level=None) -> None:
    """Background proxy cycle after a savings-level change. headroom.restart_proxy self-guards under
    op_lock: it re-applies the new env to a running proxy (a live routed one OR a retained graceful-OFF
    one kept for open sessions) and no-ops (returns None) when no proxy runs. On a hard FAILURE
    (returns False) restart_proxy has already unrouted to avoid a dead port.

    A merely-different tier must not silently kill save-credit, so on failure we first try to ROLL BACK
    to the previously-working level and bring routing back up at it (global_enable). Only if that also
    fails do we mark the headroom setting OFF to match reality — the poll health-check only cleans up
    (it never re-enables), so leaving it on would show "reconnecting" forever. The user can re-toggle."""
    try:
        if headroom.restart_proxy(ctx.data_dir) is not False:
            return
        with ctx.locked():
            was_on = bool(ctx.load_state().settings().get("headroom"))
        # Only recover routing the user actually wanted on. (A retained graceful-OFF proxy has the
        # setting already off; re-routing it would resurrect save-credit against the user's intent.)
        if was_on and prev_level is not None:
            with ctx.locked():
                s = ctx.load_state()
                if s.settings().get("savings_level") != prev_level:
                    s.set_setting("savings_level", prev_level)
                    s.save()
            if headroom.global_enable(ctx.data_dir)[0]:
                return                                   # recovered at the prior level; save-credit stays on
        if was_on:
            with ctx.locked():
                s = ctx.load_state()
                if s.settings().get("headroom"):
                    s.set_setting("headroom", False)
                    # Silent background thread — the persistent banner is the only way the user
                    # learns save-credit gave up.
                    headroom.record_event(s, "the proxy wouldn't restart at the new savings level")
                    s.save()
    except Exception:
        pass


# `output-savings` (subprocess) and `/stats` (loopback HTTP) each take up to a few seconds, and
# snapshot_state runs on EVERY UI action — so we never fetch them inline. Instead we cache the last
# result and refresh it on a background thread when stale; snapshot returns the last-known values
# instantly. Both figures change slowly, so a short TTL is plenty.
_HR_TTL = 30.0
_HR_LOCK = threading.Lock()
_HR_CACHE: dict[str, Any] = {"at": None, "savings": (None, False), "stats": None, "refreshing": False}


def _refresh_headroom_report() -> None:
    savings, stats = (None, False), None
    # Swallow any fetch error (this runs fire-and-forget on a daemon thread) and ALWAYS stamp `at` +
    # clear `refreshing` — otherwise one bad response would leave refreshing stuck True and no later
    # snapshot would ever retry. Stamping `at` also backs the retry off for a full TTL rather than
    # hammering the endpoint on every snapshot.
    # Fetch independently: a failure in one must NOT discard the other's result. (A single
    # `savings, stats = f(), g()` evaluates the whole RHS before binding, so if g() raises, a
    # successfully-computed savings figure is thrown away and the cache is stamped (None, False).)
    try:
        savings = headroom_savings()
    except Exception:
        pass
    try:
        stats = headroom_stats()
    except Exception:
        pass
    with _HR_LOCK:
        _HR_CACHE.update(at=time.monotonic(), savings=savings, stats=stats, refreshing=False)


def _headroom_report() -> tuple[tuple[int | None, bool], dict[str, Any] | None]:
    """Last-known (savings, stats), returned WITHOUT blocking; kicks a background refresh when the
    cache is empty or older than _HR_TTL. First call returns the empty default and triggers a fetch;
    the UI polls status periodically, so a fresh value lands on a later snapshot."""
    now_mono = time.monotonic()
    with _HR_LOCK:
        at = _HR_CACHE["at"]
        stale = at is None or (now_mono - at) >= _HR_TTL
        kick = stale and not _HR_CACHE["refreshing"]
        if kick:
            _HR_CACHE["refreshing"] = True
        savings, stats = _HR_CACHE["savings"], _HR_CACHE["stats"]
    if kick:
        _run_async(_refresh_headroom_report)
    return savings, stats


def _parse_output_savings(text: str) -> tuple[int | None, bool]:
    """Parse `headroom output-savings` stdout → (reduction_pct, measured). `measured` is True only
    when the report is a real holdout A/B (Method: MEASURED), not the synthetic-control ESTIMATE — so
    the UI can label an unverified number honestly."""
    import re
    txt = text or ""
    # Match ONLY the labelled "Reduction: N%" line. The report ALSO prints a holdout fraction
    # ("Holdout: 10%") and 95% CI figures, so a blind first-"N%" fallback could surface one of THOSE as
    # the savings number (e.g. a bogus "~95% fewer tokens"). Per spec §8 (don't fake a number), if
    # there's no reduction line we report None rather than guess. Accept a decimal ("42.3%") — Headroom
    # prints fractional reductions — and round to a whole percent.
    m = re.search(r"[Rr]eduction:?\s*(\d+(?:\.\d+)?)\s*%", txt)
    pct = round(float(m.group(1))) if m else None
    measured = bool(re.search(r"Method:\s*MEASURED", txt, re.IGNORECASE))
    return pct, measured


def _num(v: Any) -> float | int | None:
    """Accept a real JSON number only. /stats is an UNTRUSTED loopback response (a foreign process
    could be squatting the port), and these values are rendered into the WebView — so a non-number
    (string, bool, list) must never pass through to innerHTML. Returns None for anything non-numeric."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse_stats(data: dict) -> dict[str, Any] | None:
    """Pull lifetime totals from the proxy /stats JSON → {tokens_saved, usd_saved}, or None if the
    shape is missing (proxy down / older build) or the values aren't numeric (untrusted response)."""
    # isinstance-guard every level: /stats is untrusted, and valid-but-wrong-shaped JSON (e.g.
    # {"summary": "ok"}) would make a bare .get raise AttributeError — which must read as "no stats".
    def _obj(v: Any) -> dict:
        return v if isinstance(v, dict) else {}
    summary = _obj(_obj(data).get("summary"))  # guard `data` itself — /stats could return a list/string
    comp = _obj(summary.get("compression"))
    cost = _obj(summary.get("cost"))
    tokens = _num(comp.get("total_tokens_saved_with_rtk"))
    usd = _num(cost.get("total_saved_usd"))
    if tokens is None and usd is None:
        return None
    return {"tokens_saved": tokens, "usd_saved": usd}


def headroom_savings() -> tuple[int | None, bool]:
    """Real compression figure from `headroom output-savings` (spec §8: don't fake a number).
    Returns (reduction_pct, measured)."""
    from . import headroom
    exe = headroom.headroom_path()
    if not exe:
        return None, False
    try:
        out = subprocess.run([exe, "output-savings"], capture_output=True, text=True, timeout=10)
        return _parse_output_savings(out.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return None, False


def headroom_stats(port: int | None = None) -> dict[str, Any] | None:
    """Lifetime tokens/$ saved from the running proxy's /stats endpoint (loopback, best-effort)."""
    import json as _json
    import urllib.request
    from . import headroom
    if port is None:
        port = headroom.PROXY_PORT  # single source of truth for the port we start the proxy on
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=2) as resp:
            return _parse_stats(_json.loads(resp.read() or b"{}"))
    except (OSError, ValueError):
        return None


def snapshot_state(ctx: Context) -> dict[str, Any]:
    state = ctx.load_state()
    data = acct.status(ctx, state)
    data["headroom_available"] = headroom_available()
    if data["headroom_available"]:
        (pct, measured), stats = _headroom_report()
        data["headroom_savings"] = pct
        data["headroom_savings_measured"] = measured
        data["headroom_stats"] = stats
    else:
        data["headroom_savings"] = None
        data["headroom_savings_measured"] = False
        data["headroom_stats"] = None
    # Honest health signal: Headroom is toggled on but its proxy isn't actually running (e.g. a
    # background restart failed). Cheap, subprocess/network-free pidfile+identity check — lets the UI
    # show "save-credit paused" instead of falsely reporting it active while recovery catches up.
    from . import headroom
    data["headroom_proxy_down"] = bool(
        state.settings().get("headroom") and not headroom.proxy_maybe_running(ctx.data_dir))
    data["moved_note"] = state.data.get("moved_note")
    # Last auto-off ({at, reason}): the popover shows a persistent banner until the user re-enables
    # save-credit or dismisses it — a transient notification alone is missable.
    data["headroom_event"] = state.data.get("headroom_event")
    last = parse_iso(state.data.get("last_switch_at"))
    data["recently_switched"] = bool(last and (now() - last).total_seconds() < SWITCH_FRESH_SECONDS)
    data["dot"] = dot_for(data)  # single source of truth for the dot (JS + native both read this)
    data["door"] = door_for(data)  # shut/open door icon — same state feeds native glyph + web header
    data["app"] = {"version": __version__, "build": build_number()}  # shown in the settings sheet
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
                        # graceful OFF: unroute new sessions but KEEP the proxy alive so an already-
                        # open Claude/Codex session pinned to it doesn't drop. The proxy is reaped on
                        # quit/health-fail, not by the user toggle.
                        ok, msg = headroom.global_disable(ctx.data_dir, reap_proxy=False)
                        effective = not ok             # disable failed → leave it ON (keep recovering)
                except Exception as e:                 # never let the toggle hang with no result
                    ok, msg, effective = False, f"{type(e).__name__}: {e}", (not val)
                with ctx.locked():
                    s = ctx.load_state(); s.set_setting("headroom", effective)
                    if effective:                      # back on → the auto-off banner is stale
                        s.data.pop("headroom_event", None)
                    s.save()
                if not ok:
                    err = f"couldn't enable Headroom: {msg}" if val else msg
                    return {"ok": False, "error": err, "state": snapshot_state(ctx)}
                return {"ok": True, "state": snapshot_state(ctx)}
            with ctx.locked():
                state = ctx.load_state()
                state.set_setting(key, val)
                state.save()
            return {"ok": True, "state": snapshot_state(ctx)}

        if action == "headroom_event_dismiss":
            with ctx.locked():
                state = ctx.load_state()
                state.data.pop("headroom_event", None)
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

        if action == "set_savings_level":
            from . import headroom
            val = message.get("value")
            if val not in headroom.SAVINGS_PROFILES:
                return {"ok": False, "error": f"bad savings level: {val}"}
            with ctx.locked():
                state = ctx.load_state()
                prev = state.settings().get("savings_level")   # remember it so a failed switch can roll back
                state.set_setting("savings_level", val)
                state.save()
            # The proxy reads its compression env only at boot, so a live change takes effect on a
            # restart. Always kick it: restart_proxy self-guards on whether a proxy is actually running
            # (including a retained graceful-OFF proxy that would otherwise be adopted with the stale
            # level on the next enable) and no-ops otherwise. Off the UI thread — start_proxy blocks
            # polling /readyz (~30s), which would freeze the popover. The setting is already persisted,
            # so return the new state now and let the proxy cycle in the background (op_lock-serialized).
            _run_async(lambda: _restart_proxy_quietly(headroom, ctx, prev_level=prev))
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
