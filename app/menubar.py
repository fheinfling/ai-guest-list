"""'ai guest list' menubar app — a thin pyobjc shell over the engine.

NSStatusItem (the bar dot) + an NSPopover hosting a WKWebView that loads app/web/index.html.
All real logic is in acctsw.bridge.handle (pure, tested); this file only does AppKit plumbing:
forward JS messages to the bridge, push state back into the web view, update the dot glyph, fire
notifications, and run the official login flows in Terminal for "add a seat".

Run (dev):  PYTHONPATH=. .venv/bin/python -m app.menubar
"""
from __future__ import annotations

import json
import sys
import threading
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

from acctsw import appalive, bridge
from acctsw.context import Context

WEB_DIR = Path(__file__).resolve().parent / "web"
USAGE_POLL_SECONDS = 180.0
# The menu-bar mark is the door (icon handoff): open onto the disco when a model's free, shut when
# every seat is resting. SF Symbols give a native, template (auto light/dark) glyph; emoji is the
# fallback on older macOS where the symbol is missing (🪩 disco = open, 🚪 = shut).
DOOR_SYMBOL = {"open": "door.left.hand.open", "shut": "door.left.hand.closed"}
DOOR_EMOJI = {"open": "🪩", "shut": "🚪"}
NS_TERMINATE_NOW = 1      # NSApplicationTerminateReply.terminateNow
NS_TERMINATE_LATER = 2    # NSApplicationTerminateReply.terminateLater (we reply when teardown done)
HR_MAX_RESTARTS = 3       # proxy auto-restarts allowed per rolling window before the breaker trips
HR_RESTART_WINDOW = 1800.0  # seconds


def breaker_allows(times: list, now: float, *, max_n: int = HR_MAX_RESTARTS,
                   window: float = HR_RESTART_WINDOW) -> bool:
    """Crash-loop breaker for proxy auto-restarts: prune entries older than `window` from `times`
    (mutated in place), then allow — and record — one more attempt unless `max_n` already happened
    inside the window. Pure (caller supplies the clock) so the policy is trivially testable; the
    delegate owns the in-memory `times` list, so an app relaunch resets the breaker — fine, launch
    recovery makes its own single attempt."""
    times[:] = [t for t in times if now - t < window]
    if len(times) >= max_n:
        return False
    times.append(now)
    return True


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
            self._healthInFlight = False
            self._healthLock = threading.Lock()   # makes the single-flight check-then-set atomic
            self._hrRestartTimes = []             # breaker_allows window (auto-restart timestamps)
            return self

        @objc.python_method
        def _hrAutoOff(self, reason):
            """LAST resort, after restarts failed or the breaker tripped: flip save-credit OFF to
            match reality, persist the auto-off event (the popover's banner — a transient
            notification alone is missable), strip the dead routing, and notify. Every automatic
            disable funnels through here so the user always learns what happened and why."""
            from acctsw import headroom
            try:
                with self.ctx.locked():
                    s = self.ctx.load_state()
                    s.set_setting("headroom", False)
                    headroom.record_event(s, reason)
                    s.save()
                headroom.reconcile(self.ctx, blocking=False)
            except Exception:
                pass
            self._notify("save-credit turned itself off", reason)

        @objc.python_method
        def _claimHealthSlot(self):
            """Atomically claim the single health/recovery slot shared by the periodic poll and the
            wake handler (else both threads can pass the check before either sets the flag). Returns
            True if claimed; caller must release via _releaseHealthSlot."""
            with self._healthLock:
                if self._healthInFlight:
                    return False
                self._healthInFlight = True
                return True

        @objc.python_method
        def _releaseHealthSlot(self):
            with self._healthLock:
                self._healthInFlight = False

        # --- lifecycle ----------------------------------------------------------------------
        def applicationDidFinishLaunching_(self, _notif):
            bar = NSStatusBar.systemStatusBar()
            self.statusItem = bar.statusItemWithLength_(NSVariableStatusItemLength)
            self._setBarDoor("open")  # welcoming default until the first state push (fresh install = open)
            self.statusItem.button().setTarget_(self)
            self.statusItem.button().setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
            self._teardownDone = False
            self._hrRecovered = False     # gate: poll health-check waits until launch recovery is done
            # The app is the master switch: while it's alive, terminal codex/claude (cx/cl) supervise
            # + auto-switch; when it's closed they run stock. Heartbeat is refreshed each usage poll.
            appalive.mark_alive(self.ctx.data_dir)
            self._buildPopover()
            self._startUsageTimer()
            # Reconcile a prior run's routing OFF the main thread — global_enable starts the proxy and
            # waits on /readyz (slow); doing it inline would freeze the menubar on launch.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.recoverBg_, signature=b"v@:@"), None)
            # The proxy is a plain detached subprocess (no KeepAlive), so it does NOT reliably survive
            # sleep. On wake, re-assert it IMMEDIATELY rather than waiting up to one 180s poll — that
            # window is exactly when an already-open session pinned to 127.0.0.1:PORT fails with
            # ConnectionRefused. Register on NSWorkspace's OWN notification center (not the default one).
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
                self, objc.selector(self.workspaceDidWake_, signature=b"v@:@"),
                "NSWorkspaceDidWakeNotification", None)
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
            #  - shell rc block: wire ONCE (first launch), gated by a sentinel, so we don't fight a
            #    user who later removes our rc block / aliases by re-adding them.
            try:
                from acctsw import install
                sentinel = self.ctx.data_dir / ".cli-bootstrapped"
                first_run = not sentinel.exists()
                changed, _ = install.ensure_launchers(wire_rc=first_run)
                if first_run:
                    sentinel.write_text("")   # mark rc wired even if unchanged, so we never re-edit it
                    if changed:
                        self._notify("ai guest list is ready",
                                     "wired up codex/claude — open a new terminal to use them")
            except Exception:
                pass

        def applicationShouldTerminate_(self, _sender):
            """Single quit gate for BOTH the in-app quit button (terminate_) AND OS-level quit
            (Cmd-Q / Apple menu / logout). If there's routing to tear down, do it on a BACKGROUND
            thread (serialized via op_lock, exact restore) and tell AppKit to wait (terminateLater),
            replying once done — so quit never blocks the main thread (no beachball) and never races a
            relaunch the way a detached remove did."""
            appalive.mark_dead(self.ctx.data_dir)   # app is going away → terminal codex/claude revert to stock
            try:
                from acctsw import headroom
                # Run the teardown when there's routing/backup to undo OR a proxy still alive. The
                # proxy_maybe_running check matters for the graceful-OFF state (routing+backup already
                # gone, needs_reconcile False) — without it _headroomTeardown's reap branch is
                # unreachable and the proxy outlives the app until a later cx/cl cleans it up.
                if not self._teardownDone and (headroom.needs_reconcile(self.ctx)
                                               or headroom.proxy_maybe_running(self.ctx.data_dir)):
                    self.performSelectorInBackground_withObject_(
                        objc.selector(self.quitBg_, signature=b"v@:@"), None)
                    return NS_TERMINATE_LATER
            except Exception:
                pass
            return NS_TERMINATE_NOW

        def quitBg_(self, _arg):
            self._headroomTeardown()   # blocking + serialized, but off the main thread
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.replyTerminate_, signature=b"v@:@"), None, False)

        def replyTerminate_(self, _arg):
            NSApplication.sharedApplication().replyToApplicationShouldTerminate_(True)

        def recoverBg_(self, _arg):
            """On launch (background): the save-credit toggle persists across restarts. If it's on but
            routing isn't actually running (torn down on last quit, or crashed), RE-APPLY it. If
            re-apply fails, clear the setting so state matches reality. If it's OFF, heal() still
            strips any injection a prior crash/force-quit may have left dangling. This is the SOLE
            launch-time reconciler; the poll's health-check stays gated (self._hrRecovered) until this
            finishes, so the two can't race opposite policies on the same (setting-on, proxy-down)
            state."""
            try:
                from acctsw import headroom
                if self.ctx.load_state().settings().get("headroom"):
                    if not headroom.global_running():
                        ok, _ = headroom.global_enable(self.ctx.data_dir)
                        if not ok:
                            self._hrAutoOff("couldn't start the proxy at launch")
                else:
                    headroom.heal(self.ctx.data_dir)   # clean an orphaned injection, if any
            except Exception:
                pass
            finally:
                self._hrRecovered = True

        def workspaceDidWake_(self, _notif):
            self.performSelectorInBackground_withObject_(
                objc.selector(self.wakeBg_, signature=b"v@:@"), None)

        def wakeBg_(self, _arg):
            """Wake-from-sleep recovery (background thread). Sleep can kill the detached proxy; if
            save-credit is ON, RESTART it on the same port so already-open sessions reconnect on their
            next retry (instead of the poll's policy of tearing routing down — a sleep-induced death is
            benign, so we heal it back UP, not off). If the restart fails, fall back to stripping the
            dead routing so new sessions at least run direct. Gated like the poll: wait out launch
            recovery and never stack with an in-flight health-check."""
            if not getattr(self, "_hrRecovered", False):
                return                     # launch recovery (recoverBg_) owns the first reconcile
            if not self._claimHealthSlot():
                return
            try:
                from acctsw import headroom
                if self.ctx.load_state().settings().get("headroom"):
                    if not headroom.global_running():
                        ok, _ = headroom.global_enable(self.ctx.data_dir)
                        if ok:
                            self._notify("Headroom", "restarted the proxy after sleep")
                        else:
                            # Restart failed and routing rolled back to clean → the setting would sit
                            # ON with no proxy/routing behind it. Match state to reality (setting off,
                            # persistent banner, notify) like every other auto-off.
                            self._hrAutoOff("the proxy didn't survive sleep and wouldn't restart")
                elif headroom.needs_reconcile(self.ctx):
                    headroom.reconcile(self.ctx, blocking=False)   # strip any orphaned injection
            except Exception:
                pass
            finally:
                self._releaseHealthSlot()

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
            self.popover.setContentViewController_(vc)
            self.popover.setContentSize_(NSMakeSize(376, 600))  # match the 376px popover width
            self.popover.setBehavior_(1)  # NSPopoverBehaviorTransient

        @objc.python_method
        def _startUsageTimer(self):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                USAGE_POLL_SECONDS, self, objc.selector(self.pollUsage_, signature=b"v@:@"), None, True)
            self.pollUsage_(None)  # don't wait 180s for the first usage read

        # --- actions ------------------------------------------------------------------------
        def togglePopover_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
            else:
                btn = self.statusItem.button()
                self.popover.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, 1)
                self.pollUsage_(None)  # refresh usage each time the popover opens (cache-guarded)

        def pollUsage_(self, _timer):
            # Run the network refresh OFF the main thread so the menubar UI never freezes.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.pollBg_, signature=b"v@:@"), None)

        def pollBg_(self, _arg):
            appalive.mark_alive(self.ctx.data_dir)   # refresh heartbeat so a spurious removal self-heals within one poll
            # Refresh usage + push the dot FIRST, so the (possibly slow: grace sleep + status probe)
            # health-check never delays the usage/dot update.
            result = bridge.handle(self.ctx, {"action": "usage"})
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)
            self._headroomHealthCheck()   # if routing is on but the proxy died, tear it down (fail-safe)

        @objc.python_method
        def _headroomHealthCheck(self):
            """Critical fail-safe (runs on the poll's background thread).

            • Setting OFF: only reconcile if there's actually something to clean (needs_reconcile is
              a cheap, subprocess-free pre-check) — strips any orphaned injection from a prior crash.
              Non-blocking: never block the poll (and the usage refresh that follows) on a concurrent
              enable holding the lock.
            • Setting ON + proxy healthy: re-verify the rtk binary, so a swapped (tampered) rtk is
              caught between toggles, not only at enable time.
            • Setting ON + proxy reads down: DEBOUNCE with a short in-poll grace re-check (not a
              multi-poll streak — that left up to a 180s dead-proxy window), then RESTART the proxy
              (breaker-bounded) — the setting is user intent, so we heal up, not off. Auto-off is the
              last resort (enable failed / crash loop) and always leaves a persistent banner.
              global_enable runs under op_lock, so a concurrent manual toggle stays serialized; the
              setting is re-read after the grace sleep so a user's OFF is never overridden."""
            if not getattr(self, "_hrRecovered", False):
                return                     # launch recovery (recoverBg_) owns the first reconcile
            if not self._claimHealthSlot():
                return                     # single-flight: rapid popover-opens / wake can't stack probes
            try:
                import time
                from acctsw import headroom
                if not self.ctx.load_state().settings().get("headroom"):
                    if headroom.needs_reconcile(self.ctx):
                        headroom.reconcile(self.ctx, blocking=False)
                    return
                if headroom.global_running():
                    ok_rtk, _ = headroom.verify_rtk(self.ctx.data_dir)
                    if not ok_rtk:        # supply-chain tamper while routing was live → tear it down
                        if headroom.global_disable(self.ctx.data_dir)[0]:
                            self._hrAutoOff("its helper binary changed unexpectedly")
                        else:             # couldn't tear down → warn; routing may still use a bad rtk
                            self._notify("Headroom may be unsafe",
                                         "its helper binary changed and I couldn't turn routing off — "
                                         "quit the app or run save-credit off")
                    return
                # proxy reads down — grace re-check to ride out a transient blip (proxy restart,
                # wake-from-sleep). (8s is a balance between a too-eager reaction and a too-long
                # dead-proxy window; tune at M8.) Then heal UP, not off: the setting is the user's
                # INTENT, so a dead proxy gets restarted (same port — open sessions recover on their
                # own retries), exactly like the wake handler and launch recovery do. The breaker
                # stops a crash-looping proxy from restarting forever; only then flip the toggle off,
                # leaving a persistent banner so the user actually learns about it.
                time.sleep(8)
                if headroom.global_running():
                    return
                if not self.ctx.load_state().settings().get("headroom"):
                    return          # user toggled OFF during the grace sleep — honor their intent
                if breaker_allows(self._hrRestartTimes, time.monotonic()):
                    ok, _ = headroom.global_enable(self.ctx.data_dir)
                    if ok:
                        self._notify("save-credit", "the proxy stopped — restarted it")
                        return
                    reason = "the proxy stopped and wouldn't restart"
                else:
                    reason = "the proxy kept crashing (3 restarts in 30 min)"
                self._hrAutoOff(reason)
            except Exception:
                pass
            finally:
                self._releaseHealthSlot()

        def applyResult_(self, result):
            self._pushResult(result)
            self._updateDot(result.get("state"))

        def bgToggle_(self, msg):
            result = bridge.handle(self.ctx, dict(msg))   # global enable/disable runs here, off-main
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)
            # After save-credit turns ON, seed the savings baseline (slow LLM analysis) on this
            # background thread, AFTER the toggle result is already shown so the UI isn't held up.
            if msg.get("key") == "headroom" and msg.get("value") and result.get("ok"):
                try:
                    from acctsw import headroom
                    if not headroom.baseline_seeded(self.ctx.data_dir):
                        self._notify("Headroom", "learning your baseline so it can show savings…")
                        ok, _ = headroom.seed_baseline(self.ctx.data_dir)
                        if ok:
                            self.pollUsage_(None)   # refresh so the savings figure can appear
                except Exception:
                    pass

        @objc.python_method
        def _headroomTeardown(self):
            """On quit (driven by applicationShouldTerminate_ on a background thread): remove global
            routing + restore config via graceful_shutdown, which is SERIALIZED through op_lock
            (waits out any in-flight enable/heal) and does an exact restore. The `headroom` SETTING is kept
            so it re-applies next launch. The PROXY is only reaped once idle: open agents pin its
            port at launch, so quitting the app must not kill their in-flight work. We drain
            (bounded 20s), then leave a still-busy proxy alive — routing is stripped regardless, the
            next enable re-adopts it, and the next cx/cl heal reaps it once idle. A wedged proxy
            (unreadable gauge) never counts as busy. Guarded against a double run."""
            if getattr(self, "_teardownDone", False):
                return
            self._teardownDone = True
            try:
                from acctsw import headroom
                # One shutdown sequence for every teardown path (strip routing first, reap only an
                # idle proxy) — see _graceful_shutdown. drain=True: quit can afford the bounded wait
                # for in-flight responses; the cheap pre-checks keep the never-used-headroom quit
                # instant.
                if headroom.needs_reconcile(self.ctx) or headroom.proxy_maybe_running(self.ctx.data_dir):
                    headroom.graceful_shutdown(self.ctx.data_dir, drain=True)
            except Exception:
                pass

        # --- JS → Python (WKScriptMessageHandler) -------------------------------------------
        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            try:
                msg = dict(message.body())
            except Exception:
                return
            action = msg.get("action")
            if action == "quit":
                # terminate_ routes through applicationShouldTerminate_, which tears routing down on
                # a background thread (serialized) before actually quitting — one path for in-app and
                # OS-level quit alike.
                NSApplication.sharedApplication().terminate_(self)
                return
            if action == "settings":
                return  # reserved
            if action == "toggle" and msg.get("key") == "headroom":
                # global enable/disable is slow (starts/stops the proxy) → run off the main thread
                self._notify("Headroom", "turning on…" if msg.get("value") else "turning off…")
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.bgToggle_, signature=b"v@:@"), msg)
                return
            if action == "login":
                # native: sync-back-before-login (invariant) + run the chosen flow in Terminal.
                # Absolute import (not `.terminal`): under py2app the main script runs as top-level
                # __main__ with no package context, so a relative import would fail in the .app.
                from app.terminal import prepare_then_login
                prepare_then_login(self.ctx, msg["tool"], msg.get("command"))
                self._notify("finish signing in", "then tap ‘save my seat’ 🎟️")
                self._pushResult({"ok": True, "await_snapshot": True, "tool": msg["tool"]})
                return

            if action == "headroom_install":
                self._notify("installing headroom…", "this takes a moment — i'll enable it when ready")
            result = bridge.handle(self.ctx, msg)
            if action == "dot":
                self._updateDot(result.get("state"))
                return
            self._pushResult(result)
            self._updateDot(result.get("state"))
            if action in ("switch", "snapshot", "paste") and result.get("ok") \
                    and self.ctx.load_state().settings().get("notify", True):
                self._notify("just switched you ✨", "your seat's on the floor")

        # --- helpers ------------------------------------------------------------------------
        @objc.python_method
        def _pushResult(self, result):
            if self.webview:
                self.webview.evaluateJavaScript_completionHandler_(
                    f"window.AGL.result({json.dumps(result)});", None)

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
