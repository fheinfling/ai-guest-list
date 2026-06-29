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
from pathlib import Path

try:
    import objc
    from AppKit import (NSApplication, NSStatusBar, NSPopover, NSViewController,
                        NSVariableStatusItemLength, NSApplicationActivationPolicyAccessory,
                        NSUserNotification, NSUserNotificationCenter, NSImage)
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject, NSURL, NSTimer, NSMakeRect, NSMakeSize
except ImportError:  # allows importing this module's pure helpers without pyobjc installed
    objc = None

from acctsw import bridge
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
            return self

        # --- lifecycle ----------------------------------------------------------------------
        def applicationDidFinishLaunching_(self, _notif):
            bar = NSStatusBar.systemStatusBar()
            self.statusItem = bar.statusItemWithLength_(NSVariableStatusItemLength)
            self._setBarDoor("open")  # welcoming default until the first state push (fresh install = open)
            self.statusItem.button().setTarget_(self)
            self.statusItem.button().setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
            self._teardownDone = False
            self._hrRecovered = False     # gate: poll health-check waits until launch recovery is done
            self._buildPopover()
            self._startUsageTimer()
            # Reconcile a prior run's routing OFF the main thread — global_enable starts the proxy and
            # waits on /readyz (slow); doing it inline would freeze the menubar on launch.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.recoverBg_, signature=b"v@:@"), None)

        def applicationShouldTerminate_(self, _sender):
            """Single quit gate for BOTH the in-app quit button (terminate_) AND OS-level quit
            (Cmd-Q / Apple menu / logout). If there's routing to tear down, do it on a BACKGROUND
            thread (serialized via op_lock, exact restore) and tell AppKit to wait (terminateLater),
            replying once done — so quit never blocks the main thread (no beachball) and never races a
            relaunch the way a detached remove did."""
            try:
                from acctsw import headroom
                if not self._teardownDone and headroom.needs_reconcile(self.ctx):
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
                            with self.ctx.locked():
                                s = self.ctx.load_state(); s.set_setting("headroom", False); s.save()
                else:
                    headroom.heal(self.ctx.data_dir)   # clean an orphaned injection, if any
            except Exception:
                pass
            finally:
                self._hrRecovered = True

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
              multi-poll streak — that left up to a 180s dead-proxy window). One transient blip is
              tolerated; a genuinely dead proxy is healed within seconds. reconcile()/heal() re-check
              `global_running` under the op_lock, so a recovery in between is still honored."""
            if not getattr(self, "_hrRecovered", False):
                return                     # launch recovery (recoverBg_) owns the first reconcile
            if getattr(self, "_healthInFlight", False):
                return                     # single-flight: rapid popover-opens can't stack probes/threads
            self._healthInFlight = True
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
                            with self.ctx.locked():
                                s = self.ctx.load_state(); s.set_setting("headroom", False); s.save()
                            self._notify("Headroom turned off", "its helper binary changed unexpectedly")
                        else:             # couldn't tear down → warn; routing may still use a bad rtk
                            self._notify("Headroom may be unsafe",
                                         "its helper binary changed and I couldn't turn routing off — "
                                         "quit the app or run save-credit off")
                    return
                # proxy reads down — grace re-check to ride out a transient blip (proxy restart,
                # wake-from-sleep) before the destructive teardown, then heal fast. (8s is a balance
                # between a too-eager teardown and a too-long dead-proxy window; tune at M8.)
                time.sleep(8)
                if headroom.global_running():
                    return
                healed, _ = headroom.reconcile(self.ctx, blocking=False)
                if healed:
                    self._notify("Headroom turned off", "the proxy stopped — restored your setup")
            except Exception:
                pass
            finally:
                self._healthInFlight = False

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
            routing + restore config via global_disable, which is SERIALIZED through op_lock (waits
            out any in-flight enable/heal) and does an exact restore. The `headroom` SETTING is kept
            so it re-applies next launch. The proxy's lifecycle is the APP's, so quit also reaps a
            graceful-OFF proxy that was left running for open sessions (needs_reconcile is false then,
            since routing is already direct). Guarded against a double run."""
            if getattr(self, "_teardownDone", False):
                return
            self._teardownDone = True
            try:
                from acctsw import headroom
                if headroom.needs_reconcile(self.ctx):
                    headroom.global_disable(self.ctx.data_dir)   # blocking + op_lock-serialized (unroute + reap)
                elif headroom.proxy_ready():
                    headroom.stop_proxy(self.ctx.data_dir)       # reap a graceful-OFF proxy kept alive for open sessions
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
