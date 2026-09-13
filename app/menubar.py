"""'ai guest list' menubar app — a thin pyobjc shell over the engine.

NSStatusItem (the bar dot) + an NSPopover hosting a WKWebView that loads app/web/index.html.
All real logic is in acctsw.bridge.handle (pure, tested); this file only does AppKit plumbing:
forward JS messages to the bridge, push state back into the web view, update the dot glyph, fire
notifications, and run the official login flows in Terminal for "add a seat".

Run (dev):  PYTHONPATH=. .venv/bin/python -m app.menubar
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

try:
    import objc
    from AppKit import (NSApplication, NSStatusBar, NSPopover, NSViewController,
                        NSVariableStatusItemLength, NSApplicationActivationPolicyAccessory,
                        NSUserNotification, NSUserNotificationCenter, NSImage, NSWorkspace)
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject, NSURL, NSTimer, NSMakeRect, NSMakeSize
except ImportError:  # allows importing this module's pure helpers without pyobjc installed
    objc = None

from acctsw import TOOLS, appalive, bridge, session
from acctsw.context import Context, hydrate_path

WEB_DIR = Path(__file__).resolve().parent / "web"
# The open popover is the one place where a person is actively making a decision from the
# numbers, so keep its cache moving promptly.  Hidden polling stays deliberately slower: both
# usage endpoints throttle hard and the engine applies its own per-seat backoff as a second guard.
USAGE_POLL_OPEN_SECONDS = 30.0
USAGE_POLL_HIDDEN_SECONDS = 180.0
STATE_POLL_SECONDS = 3.0
# The menu-bar mark is the door (icon handoff): open onto the disco when a model's free, shut when
# every seat is resting. SF Symbols give a native, template (auto light/dark) glyph; emoji is the
# fallback on older macOS where the symbol is missing (🪩 disco = open, 🚪 = shut).
DOOR_SYMBOL = {"open": "door.left.hand.open", "shut": "door.left.hand.closed"}
DOOR_EMOJI = {"open": "🪩", "shut": "🚪"}
NS_TERMINATE_NOW = 1      # NSApplicationTerminateReply.terminateNow


def usage_poll_interval(popover_visible: bool) -> float:
    """The native scheduler cadence; kept pure so it is testable without AppKit."""
    return USAGE_POLL_OPEN_SECONDS if popover_visible else USAGE_POLL_HIDDEN_SECONDS


def usage_poll_scope(popover_visible: bool, last_all_at: float, *, at: float) -> str:
    """Keep active cards quick while guaranteeing a full-seat refresh at least every hidden cadence."""
    if not popover_visible or at - last_all_at >= USAGE_POLL_HIDDEN_SECONDS:
        return "all"
    return "active"


def _codex_desktop_app(app) -> bool:
    """Whether an NSRunningApplication is the Codex desktop app (testable without AppKit)."""
    try:
        bundle = app.bundleIdentifier() or ""
        name = app.localizedName() or ""
    except Exception:
        return False
    return bundle == "com.openai.codex" if bundle else name == "Codex"


def request_codex_restart(running_apps) -> tuple[bool, str | None]:
    """Ask a running Codex app to quit normally; never force-terminate it.

    The caller schedules the relaunch after this returns.  Separating the request from launching
    makes it impossible for a failed graceful quit to be masked by an `open -a Codex` activation.
    """
    matches = [app for app in running_apps if _codex_desktop_app(app)]
    if not matches:
        return False, None
    for app in matches:
        try:
            if not app.terminate():
                return True, "Codex did not accept the restart request"
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return True, f"couldn't ask Codex to quit: {detail}"
    return True, None


def launch_codex_desktop(*, run=subprocess.run) -> str | None:
    """Open Codex after its graceful quit and verify that macOS accepted the request."""
    try:
        from acctsw.procenv import harden_env
        completed = run(["open", "-b", "com.openai.codex"], env=harden_env(), capture_output=True,
                        text=True, timeout=10)
        if completed.returncode != 0:
            return completed.stderr.strip() or f"open exited {completed.returncode}"
    except Exception as exc:
        return str(exc).strip() or exc.__class__.__name__
    return None


def _bootstrap_notice(notify, title: str, text: str) -> None:
    """Best-effort user notification with a stderr fallback for non-AppKit/test callers."""
    try:
        notify(title, text)
    except Exception as exc:
        print(f"{title}: {text} (notification failed: {exc})", file=sys.stderr)


def bootstrap_supervision(ctx: Context, notify) -> dict:
    """Heal terminal supervision without letting a background bootstrap failure crash the app.

    Wrapper validation runs on every launch.  The rc block is repaired only while the explicit
    ``supervise_shell`` setting is enabled; the legacy sentinel now controls only the one-time
    welcome notification.
    """
    from acctsw import install

    sentinel = ctx.data_dir / ".cli-bootstrapped"
    first_run = not sentinel.exists()
    try:
        enabled = bool(ctx.load_state().settings().get("supervise_shell", True))
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        error = f"couldn't read the terminal supervision setting: {detail}"
        _bootstrap_notice(notify, "terminal supervision needs attention", error)
        return {"ok": False, "error": error}

    try:
        status = install.supervision_status()
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        error = f"couldn't inspect terminal supervision: {detail}"
        _bootstrap_notice(notify, "terminal supervision needs attention", error)
        return {"ok": False, "error": error}
    wire_rc = enabled and not status["block"]
    try:
        changed, messages = install.ensure_launchers(wire_rc=wire_rc)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        error = f"couldn't wire codex/claude supervision: {detail}"
        _bootstrap_notice(notify, "terminal supervision needs attention", error)
        return {"ok": False, "error": error, "status": status}

    try:
        final_status = install.supervision_status()
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        error = f"terminal supervision was wired, but couldn't be verified: {detail}"
        _bootstrap_notice(notify, "terminal supervision needs attention", error)
        return {"ok": False, "error": error}

    if first_run:
        try:
            sentinel.write_text("")
        except OSError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            _bootstrap_notice(
                notify,
                "ai guest list setup needs attention",
                f"terminal supervision is ready, but setup couldn't be recorded: {detail}",
            )
        if changed and enabled and final_status["active"]:
            _bootstrap_notice(
                notify,
                "ai guest list is ready",
                "wired up codex/claude — open a new terminal to use them",
            )
    return {
        "ok": True,
        "changed": changed,
        "messages": messages,
        "status": final_status,
    }


if objc is not None:

    class AGLDelegate(NSObject):
        def initWithContext_(self, ctx):
            self = objc.super(AGLDelegate, self).init()
            if self is None:
                return None
            self.ctx = ctx
            self.statusItem = None
            self.popover = None
            self.webview = None
            self._state_timer = None
            self._usage_timer = None
            self._usage_interval = None
            self._usage_poll_inflight = False
            self._pending_usage_request = None
            self._last_all_usage_poll = 0.0
            self._codex_restart_pending = False
            self._codex_restart_timer = None
            self._codex_restart_attempts = 0
            self._auto_handoff_warnings = set()
            self._last_state_sig = None          # (state rev, session heartbeat mtimes)
            self._acctWarned = set()              # shared-account warnings already toasted this session
            self._login_baseline = {}             # tool → (op, digest of live creds at that login launch)
            self._login_seq = 0                   # monotonic login op id (see the login handler)
            return self

        # --- lifecycle ----------------------------------------------------------------------
        def applicationDidFinishLaunching_(self, _notif):
            bar = NSStatusBar.systemStatusBar()
            self.statusItem = bar.statusItemWithLength_(NSVariableStatusItemLength)
            self._setBarDoor("open")  # welcoming default until the first state push (fresh install = open)
            self.statusItem.button().setTarget_(self)
            self.statusItem.button().setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
            # The app is the master switch: while it's alive, terminal codex/claude (cx/cl) supervise
            # + auto-switch; when it's closed they run stock. Heartbeat is refreshed each usage poll.
            appalive.mark_alive(self.ctx.data_dir)
            self._buildPopover()
            self._startUsageTimer()
            # One-time cleanup of the retired "save credit" Headroom feature — off the main thread so a
            # config restore never blocks the menubar on launch. No-op once nothing remains.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.recoverBg_, signature=b"v@:@"), None)
            # Make cx/cl work with no manual steps — having the app installed IS the install. Write the
            # wrappers + wire the shell rc (PATH + codex/claude aliases) on launch (idempotent), so
            # autoswitch works out of the box and a deleted rc block self-heals.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.bootstrapBg_, signature=b"v@:@"), None)

        def bootstrapBg_(self, _arg):
            # Two responsibilities, two lifetimes:
            #  - bin wrappers: validate/heal EVERY launch (idempotent) so a wrapper an older build
            #    baked with a broken interpreter — e.g. system python3 + the frozen 3.11 zip, which
            #    crashed `claude auth login` with "can't find module 'encodings'" — gets corrected.
            #  - shell rc block: repair it whenever it is missing, unless the explicit setting says
            #    the user opted out. The sentinel is only the one-time welcome-notification marker.
            bootstrap_supervision(self.ctx, self._notify)
            # Refresh the popover after the background repair. A ready/usage reply may have raced and
            # rendered the pre-heal status; pushing a fresh snapshot makes the banner self-heal too.
            try:
                result = {"ok": True, "state": bridge.snapshot_state(self.ctx), "background": True}
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    objc.selector(self.applyResult_, signature=b"v@:@"), result, False)
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                _bootstrap_notice(
                    self._notify,
                    "terminal supervision needs attention",
                    f"setup finished, but its status couldn't be refreshed: {detail}",
                )

        def applicationShouldTerminate_(self, _sender):
            """Single quit gate for BOTH the in-app quit button (terminate_) AND OS-level quit
            (Cmd-Q / Apple menu / logout). Nothing to tear down anymore (no proxy/routing) — just mark
            the app dead so terminal codex/claude revert to stock, and quit immediately."""
            appalive.mark_dead(self.ctx.data_dir)
            return NS_TERMINATE_NOW

        def recoverBg_(self, _arg):
            """On launch (background): clean up after the retired "save credit" Headroom feature. If an
            older build left provider routing injected in ~/.codex/~/.claude (or an orphaned proxy),
            strip/restore it once so plain codex/claude run directly. Idempotent; a no-op once nothing
            remains."""
            try:
                from acctsw import headroom
                if headroom.legacy_present(self.ctx):
                    headroom.cleanup_legacy(self.ctx)
            except Exception:
                pass

        @objc.python_method
        def _buildPopover(self):
            cfg = WKWebViewConfiguration.alloc().init()
            ucc = WKUserContentController.alloc().init()
            ucc.addScriptMessageHandler_name_(self, "agl")
            cfg.setUserContentController_(ucc)
            self.webview = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 376, 600), cfg)
            self.webview.loadFileURL_allowingReadAccessToURL_(
                NSURL.fileURLWithPath_(str(WEB_DIR / "index.html")),
                NSURL.fileURLWithPath_(str(WEB_DIR)))
            vc = NSViewController.alloc().init()
            vc.setView_(self.webview)
            self.popover = NSPopover.alloc().init()
            self.popover.setDelegate_(self)
            self.popover.setContentViewController_(vc)
            self.popover.setContentSize_(NSMakeSize(376, 600))  # match the 376px popover width
            self.popover.setBehavior_(1)  # NSPopoverBehaviorTransient

        @objc.python_method
        def _setUsagePollInterval(self, interval):
            """Run one coalesced usage poll on the cadence appropriate for popover visibility."""
            if self._usage_timer is not None and self._usage_interval == interval:
                return
            if self._usage_timer is not None:
                self._usage_timer.invalidate()
                self._usage_timer = None
            self._usage_interval = interval
            self._usage_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, self, objc.selector(self.pollUsage_, signature=b"v@:@"), None, True)

        @objc.python_method
        def _startUsageTimer(self):
            self._setUsagePollInterval(usage_poll_interval(False))
            self.pollUsage_(None)  # don't wait 180s for the first usage read

        # --- actions ------------------------------------------------------------------------
        def togglePopover_(self, sender):
            if self.popover.isShown():
                self._stopStateTimer()
                self._setWebVisible(False)
                self.popover.performClose_(sender)
            else:
                btn = self.statusItem.button()
                self.popover.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, 1)
                self._setWebVisible(True)
                self._startStateTimer()
                self._setUsagePollInterval(usage_poll_interval(True))
                self.pollUsage_(None)  # refresh usage each time the popover opens (cache-guarded)

        def popoverDidClose_(self, _notification):
            # Transient popovers also close when the user clicks elsewhere, bypassing togglePopover_.
            self._stopStateTimer()
            self._setWebVisible(False)
            self._setUsagePollInterval(usage_poll_interval(False))

        @objc.python_method
        def _startStateTimer(self):
            if self._state_timer is not None:
                return
            self._state_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                STATE_POLL_SECONDS, self, objc.selector(self.pollState_, signature=b"v@:@"), None, True)
            self.pollState_(None)

        @objc.python_method
        def _stopStateTimer(self):
            if self._state_timer is not None:
                self._state_timer.invalidate()
                self._state_timer = None

        @objc.python_method
        def _stateSignature(self):
            """What "the state changed" means for the popover, cheaply (no network, no subprocess).

            state.json's monotonic ``rev`` covers seat/usage/limit writes — but NOT a session
            starting or ending: mark_session/clear_session write their own heartbeat file and a
            launch on the already-active seat saves no state at all. Without the heartbeat mtimes
            here, the live-session dot would lag by up to a full usage poll (180s), which is the
            exact staleness this timer exists to remove.
            """
            try:
                rev = int(self.ctx.load_state().data.get("rev", 0))
            except Exception:
                return None
            beats = []
            for tool in TOOLS:
                try:
                    beats.append(session.session_mtime_ns(self.ctx.data_dir, tool))
                except Exception:
                    beats.append(0)
            return (rev, tuple(beats))

        def pollState_(self, _timer):
            """While open, notice state.json / session writes from supervised cx/cl sessions.

            Deliberately network-free: the "status" action only re-reads local state, so the
            engine's per-seat usage cache still governs every endpoint call."""
            sig = self._stateSignature()
            if sig is None or sig == self._last_state_sig:
                return
            self._last_state_sig = sig
            result = dict(bridge.handle(self.ctx, {"action": "status"}))
            result["background"] = True
            self.applyResult_(result)

        def pollUsage_(self, _timer):
            # Run the network refresh OFF the main thread so the menubar UI never freezes.  A slow
            # Claude auth-status/usage request can overlap an open-popover 30s tick; one in-flight
            # poll is enough because bridge/usage refreshes every provider together.
            if self._usage_poll_inflight:
                # A just-switched active seat needs its own forced read; retain that request after
                # the ordinary in-flight poll finishes instead of silently dropping it.
                if isinstance(_timer, dict) and _timer.get("force"):
                    self._pending_usage_request = dict(_timer)
                return
            self._usage_poll_inflight = True
            self.performSelectorInBackground_withObject_(
                objc.selector(self.pollBg_, signature=b"v@:@"), _timer)

        def pollBg_(self, request):
            try:
                appalive.mark_alive(self.ctx.data_dir)   # refresh heartbeat so a spurious removal self-heals within one poll
                if not isinstance(request, dict):
                    visible = self._usage_interval == USAGE_POLL_OPEN_SECONDS
                    scope = usage_poll_scope(visible, self._last_all_usage_poll, at=time.monotonic())
                    request = {"scope": scope}
                    if scope == "all":
                        self._last_all_usage_poll = time.monotonic()
                result = dict(bridge.handle(self.ctx, {"action": "usage", **request}))
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                result = {"ok": False, "error": f"couldn't refresh usage: {detail}"}
            result["background"] = True   # the JS must not toast a transient poll error over the UI
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.finishUsagePoll_, signature=b"v@:@"), result, False)

        def finishUsagePoll_(self, result):
            self._usage_poll_inflight = False
            self.applyResult_(result)
            handoff = result.get("auto_switch") or {}
            if handoff.get("status") == "switched":
                notify_on = ((result.get("state") or {}).get("settings") or {}).get("notify", True)
                if notify_on:
                    self._notify(
                        f"switched {handoff.get('tool')} ✨",
                        f"{handoff.get('from')} hit its usage limit — {handoff.get('to')} is on now",
                    )
                self._restartCodexAfterSwap_(handoff.get("tool"))
                # The new active credentials have just landed.  Prime its card rather than making
                # the person wait for the next visible/hidden cadence.
                tool, email = handoff.get("tool"), handoff.get("to")
                if tool and email:
                    self.pollUsage_({"scope": "active", "tool": tool, "only": email,
                                     "force": True})
            elif handoff.get("status") == "failed" and handoff.get("error"):
                key = (handoff.get("tool"), handoff.get("from"), handoff.get("to"), handoff["error"])
                if key not in self._auto_handoff_warnings:
                    self._auto_handoff_warnings.add(key)
                    self._notify("couldn't switch automatically", handoff["error"])
            pending = self._pending_usage_request
            self._pending_usage_request = None
            if pending is not None:
                self.pollUsage_(pending)

        @objc.python_method
        def _credDigest(self, tool):
            import hashlib
            blob = self.ctx.cred[tool].get_live()
            return hashlib.sha256(blob.encode()).hexdigest() if blob else None

        @objc.python_method
        def _revealCodexAuth(self):
            """Reveal ~/.codex/auth.json in Finder (or its folder if the file isn't there yet) as a
            helper for the paste flow. Best-effort, non-blocking; any failure is swallowed — revealing
            a file must never surface an error or beachball the app."""
            import subprocess
            from acctsw.procenv import harden_env
            try:
                path = self.ctx.cred["codex"].auth_path
                if path.exists():
                    subprocess.Popen(["open", "-R", str(path)], env=harden_env())
                elif path.parent.exists():
                    subprocess.Popen(["open", str(path.parent)], env=harden_env())
            except Exception:
                pass

        def loginBg_(self, msg):
            # sync-back-before-login (invariant) + launch the official flow in Terminal, off the main
            # thread. Everything is inside the try so ANY startup failure (the absolute py2app import,
            # command resolution, the launch) surfaces as one correlated error and drops the baseline.
            tool = msg["tool"]
            op = msg.get("_op")
            try:
                # Absolute import (not `.terminal`): under py2app the main script runs as top-level
                # __main__ with no package context, so a relative import would fail in the .app.
                # prepare_then_login resolves the CLI's ABSOLUTE path itself (raising a clear error if
                # the CLI is missing) and launches via `open` — no AppleEvents/Automation permission.
                from app.terminal import prepare_then_login
                prepare_then_login(self.ctx, tool)
            except Exception as e:
                # Launch failed. If a NEWER login for this tool has since superseded us (op identity),
                # this failure is stale — the user restarted, so stay silent: pushing it would send
                # the restarted same-tool flow back to details and toast over it. Only the current
                # attempt reports, drops its own baseline, and returns THIS flow to details.
                cur = self._login_baseline.get(tool)
                if cur is None or cur[0] != op:
                    return
                self._login_baseline.pop(tool, None)
                # Surface the specific reason (e.g. "can't find the codex command — install codex
                # first…") instead of a generic line, so a missing CLI or a failed launch is actionable.
                error = str(e).strip() or "couldn't open the sign-in — try again"
                result = {"ok": False, "add_op": True, "tool": tool, "error": error}
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    objc.selector(self.applyResult_, signature=b"v@:@"), result, False)
                return
            # Success: the connecting step is already shown optimistically; just nudge the user. The
            # notification must fire on the main thread.
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.loginNudge_, signature=b"v@:@"), None, False)

        def loginNudge_(self, _arg):
            self._notify("finish signing in", "then tap ‘save my seat’ 🎟️")

        def addBg_(self, msg):
            # `paste`/`snapshot` verify creds against the provider (Claude shells out / hits the
            # network), so run them off the main thread — a synchronous handle() would beachball the
            # app behind the add-seat "saving your seat…" spinner. ctx.locked() serialises the poll.
            # Guard: an unexpected raise here would otherwise leave the user stuck on the spinner with
            # nothing pushed back — turn it into a friendly error the connecting step can act on.
            tool = msg.get("tool")
            try:
                if msg.get("action") == "snapshot":
                    base = self._login_baseline.get(tool)   # (op, digest)
                    if base is not None and self._credDigest(tool) == base[1]:
                        # Live creds are unchanged since the login launched → the browser sign-in
                        # isn't finished. Snapshotting now would just re-add the OUTGOING seat.
                        result = {"ok": False, "add_op": True, "tool": tool,
                                  "error": "finish signing in first, then save your seat"}
                        self.performSelectorOnMainThread_withObject_waitUntilDone_(
                            objc.selector(self.applyResult_, signature=b"v@:@"), result, False)
                        return
                result = dict(bridge.handle(self.ctx, dict(msg)))
                if result.get("ok") and msg.get("action") == "snapshot":
                    self._login_baseline.pop(tool, None)   # consumed — a real add completed
            except Exception:
                result = {"ok": False, "error": "something went wrong saving that seat"}
            # Tag it as an add-op reply FOR THIS TOOL. The JS reducer uses both to attribute the reply
            # to the right flow (vs. a poll error, or a late reply from another tool's abandoned add).
            result["add_op"] = True
            result["tool"] = tool
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)

        def applyResult_(self, result):
            self._pushResult(result)
            self._updateDot(result.get("state"))
            self._notifyAccountWarnings(result)
            # A backgrounded add still deserves the "seat added" nudge the main path fires. The notify
            # flag is already in the result's own snapshot — no need to re-read state from disk.
            notify_on = ((result.get("state") or {}).get("settings") or {}).get("notify", True)
            if result.get("add_op") and result.get("added") and result.get("ok") and notify_on:
                self._notify("seat saved ✨", "your seat's on the floor")

        @objc.python_method
        def _restartCodexAfterSwap_(self, tool):
            """Gracefully restart Codex after a successful Codex handoff when requested.

            Claude reads Keychain credentials live, so it is intentionally excluded.  This never
            force-terminates Codex: an unsuccessful normal quit is surfaced and no launch is tried
            against the still-running process.  The pending flag coalesces duplicate poll replies.
            """
            if tool != "codex" or self._codex_restart_pending:
                return
            try:
                enabled = bool(self.ctx.load_state().settings().get("restart_app", False))
            except Exception:
                enabled = False
            if not enabled:
                return
            try:
                was_running, error = request_codex_restart(
                    NSWorkspace.sharedWorkspace().runningApplications()
                )
            except Exception as exc:
                was_running = False
                error = str(exc).strip() or exc.__class__.__name__
            if error:
                self._notify("couldn't restart Codex", error)
                return
            if not was_running:
                # This is a restart option, not a request to start a desktop app the user left
                # closed.  The CLI/menubar credentials were still switched successfully.
                return
            self._codex_restart_pending = True
            # `terminate()` is asynchronous. Poll the running-app list until it has actually
            # exited; `open -a` while the old process still exists merely activates it, then
            # leaves no replacement when it later finishes quitting.
            self._codex_restart_attempts = 0
            self._codex_restart_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.25, self, objc.selector(self.waitForCodexExit_, signature=b"v@:@"), None, True)

        def waitForCodexExit_(self, timer):
            self._codex_restart_attempts += 1
            try:
                still_running = any(
                    _codex_desktop_app(app)
                    for app in NSWorkspace.sharedWorkspace().runningApplications()
                )
            except Exception as exc:
                timer.invalidate()
                self._codex_restart_timer = None
                self._codex_restart_pending = False
                detail = str(exc).strip() or exc.__class__.__name__
                self._notify("couldn't restart Codex", f"couldn't confirm it exited: {detail}")
                return
            if still_running and self._codex_restart_attempts < 40:  # ten seconds, never force-kill
                return
            timer.invalidate()
            self._codex_restart_timer = None
            if still_running:
                self._codex_restart_pending = False
                self._notify("couldn't restart Codex", "Codex did not quit within 10 seconds")
                return
            self.launchCodexAfterSwap_(None)

        def launchCodexAfterSwap_(self, _timer):
            # `open` is a short subprocess but still belongs off AppKit's main thread.  Its result
            # is returned to the main thread so an unavailable/misnamed app is never silent.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.launchCodexBg_, signature=b"v@:@"), None)

        def launchCodexBg_(self, _arg):
            error = launch_codex_desktop()
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.finishCodexLaunch_, signature=b"v@:@"), error or "", False)

        def finishCodexLaunch_(self, error):
            self._codex_restart_pending = False
            if error:
                self._notify("couldn't restart Codex", error)

        @objc.python_method
        def _notifyAccountWarnings(self, result):
            """Toast each shared-account warning ONCE per session — the user needs to know two seats
            are secretly the same account (no real headroom). Keyed on the exact message so a new or
            changed warning re-notifies. The warning also rides `status --json`; the toast is the
            in-app surface today (a persistent popover banner is a follow-up). Warnings live under the
            nested state payload (bridge returns {ok, state}), same level as the dot."""
            for w in ((result.get("state") or {}).get("warnings") or []):
                if w not in self._acctWarned:
                    self._acctWarned.add(w)
                    self._notify("heads up — seats share one account", w)

        # --- JS → Python (WKScriptMessageHandler) -------------------------------------------
        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            try:
                msg = dict(message.body())
            except Exception:
                return
            action = msg.get("action")
            if action == "quit":
                NSApplication.sharedApplication().terminate_(self)
                return
            if action == "settings":
                return  # reserved
            if action == "login":
                # Give each login an op id so a late failure from an abandoned attempt can only drop
                # ITS OWN baseline, not a newer one for the same tool.
                self._login_seq += 1
                op = self._login_seq
                # Baseline the OUTGOING live creds NOW — before anything can rewrite them — so a "save
                # my seat" tapped before sign-in completes (creds unchanged) is rejected instead of
                # silently re-adding the old seat. (sync_back doesn't change live creds, so capturing
                # here == capturing just before launch.) Reading creds is a quick, lock-free read.
                self._login_baseline[msg["tool"]] = (op, self._credDigest(msg["tool"]))
                # Off the main thread: prepare_then_login holds ctx.locked() and spawns `open` to launch
                # the sign-in terminal — on the main thread it would beachball behind a usage poll
                # holding the same lock.
                m = dict(msg); m["_op"] = op
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.loginBg_, signature=b"v@:@"), m)
                return
            if action == "reveal":
                # Reveal ~/.codex/auth.json in Finder (helper for the paste flow). OS-level, so it's
                # handled natively rather than via the bridge. Fire-and-forget so it can't block the UI.
                self._revealCodexAuth()
                return
            if action in ("paste", "snapshot", "import_current"):
                # off the main thread (see addBg_) — paste/snapshot verify creds against the provider
                # (Claude shells out / hits the network); import_current does a keychain snapshot. All
                # run behind the connecting spinner and would otherwise beachball the app.
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.addBg_, signature=b"v@:@"), msg)
                return

            if action == "switch":
                # Claude identity can take seconds; keep paints and usage updates responsive.
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.switchBg_, signature=b"v@:@"), msg)
                return
            result = bridge.handle(self.ctx, msg)
            if action == "dot":
                self._updateDot(result.get("state"))
                return
            self._pushResult(result)
            self._updateDot(result.get("state"))

        def switchBg_(self, msg):
            try:
                result = dict(bridge.handle(self.ctx, msg))
            except Exception as exc:
                result = {"ok": False, "error": str(exc).strip() or exc.__class__.__name__}
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.finishSwitch_, signature=b"v@:@"), [msg, result], False)

        def finishSwitch_(self, response):
            msg, result = response
            self.applyResult_(result)
            if result.get("ok") and self.ctx.load_state().settings().get("notify", True):
                self._notify("just switched you ✨", "your seat's on the floor")
            if result.get("ok"):
                self._restartCodexAfterSwap_(msg.get("tool"))
                # A manual switch is equally entitled to a current card; if a background sweep is
                # still running this is retained as the one coalesced follow-up request.
                self.pollUsage_({"scope": "active", "tool": msg.get("tool"),
                                 "only": msg.get("email"), "force": True})

        # --- helpers ------------------------------------------------------------------------
        @objc.python_method
        def _pushResult(self, result):
            # Deliberately does NOT stamp _last_state_sig: this runs for pushes from other paths
            # (the usage poll, a user action) whose state may already be older than what is on disk.
            # Stamping here could swallow a change; the state timer records its OWN signature before
            # it pushes, so the worst case is one redundant, network-free re-render.
            if self.webview:
                self.webview.evaluateJavaScript_completionHandler_(
                    f"window.AGL.result({json.dumps(result)});", None)

        @objc.python_method
        def _setWebVisible(self, visible):
            if self.webview:
                flag = "true" if visible else "false"
                self.webview.evaluateJavaScript_completionHandler_(
                    f"window.AGL && window.AGL.setVisible({flag});", None)

        @objc.python_method
        def _updateDot(self, state):
            if not state:
                return
            self._setBarDoor(state.get("door", "open"))

        @objc.python_method
        def _setBarDoor(self, door):
            """Set the bar mark to the open/shut door — SF Symbol (template) when available, emoji
            fallback otherwise. Swaps live with availability."""
            btn = self.statusItem.button()
            img = None
            name = DOOR_SYMBOL.get(door)
            if name and hasattr(NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    name, "a model's free" if door == "open" else "every seat is resting")
            if img is not None:
                img.setTemplate_(True)
                btn.setTitle_("")
                btn.setImage_(img)
            else:
                btn.setImage_(None)
                btn.setTitle_(DOOR_EMOJI.get(door, "🎟️"))

        @objc.python_method
        def _notify(self, title, text):
            # Always deliver on the main thread — _notify is called from background poll threads too,
            # and AppKit/NSUserNotification UI off-main can silently drop or assert.
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.notifyMain_, signature=b"v@:@"), [title, text], False)

        def notifyMain_(self, pair):
            n = NSUserNotification.alloc().init()
            n.setTitle_(pair[0])
            n.setInformativeText_(pair[1])
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)


def main() -> int:
    if objc is None:
        print("pyobjc not installed; run `pip install '.[app]'`", file=sys.stderr)
        return 1
    # A GUI launch gives us only launchd's minimal PATH; add the dirs where claude/codex/node live
    # BEFORE resolving them, or the app can't run the CLIs (identify a login → seat, poll usage).
    hydrate_path()
    ctx = Context.default()
    ctx.ensure_dirs()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menubar only, no dock icon
    delegate = AGLDelegate.alloc().initWithContext_(ctx)
    app.setDelegate_(delegate)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
