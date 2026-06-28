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
                        NSUserNotification, NSUserNotificationCenter)
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject, NSURL, NSTimer, NSMakeRect, NSMakeSize
except ImportError:  # allows importing this module's pure helpers without pyobjc installed
    objc = None

from acctsw import bridge
from acctsw.context import Context

WEB_DIR = Path(__file__).resolve().parent / "web"
USAGE_POLL_SECONDS = 180.0
DOT_GLYPH = {"green": "🟢", "amber": "🟡", "switched": "🟡", "hello": "🔴"}


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
            self.statusItem.button().setTitle_("🎟️")
            self.statusItem.button().setTarget_(self)
            self.statusItem.button().setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
            self._teardownDone = False
            self._buildPopover()
            self._startUsageTimer()
            # Reconcile a prior run's routing OFF the main thread — global_enable shells out to
            # `headroom install apply` (slow); doing it inline would freeze the menubar on launch.
            self.performSelectorInBackground_withObject_(
                objc.selector(self.recoverBg_, signature=b"v@:@"), None)

        def applicationWillTerminate_(self, _notif):
            self._headroomTeardown()  # safety net: never leave global routing dangling on exit

        def recoverBg_(self, _arg):
            """On launch (background): the save-credit toggle persists across restarts. If it's on but
            routing isn't actually running (torn down on last quit, or crashed), RE-APPLY it. If
            re-apply fails, clear the setting so state matches reality. If it's OFF, heal() still
            strips any injection a prior crash/force-quit may have left dangling."""
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
            self._headroomHealthCheck()   # if routing is on but the proxy died, tear it down (fail-safe)
            result = bridge.handle(self.ctx, {"action": "usage"})
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)

        @objc.python_method
        def _headroomHealthCheck(self):
            """Critical fail-safe (runs on the poll's background thread). reconcile() is serialized
            and keys off ACTUAL state: it removes routing + restores config + clears the setting ONLY
            if the proxy is truly down while config is injected, and no-ops if the proxy is healthy.
            Because heal() re-checks `global_running` inside the same op_lock the toggle holds, a
            health-check firing while an enable is still bringing the proxy up will NOT tear it down
            (closes the TOCTOU). While routing is up, also re-verify the rtk binary so a swapped
            (tampered) rtk is caught between toggles, not only at enable time."""
            try:
                from acctsw import headroom
                changed, _ = headroom.reconcile(self.ctx)
                if changed:
                    self._notify("Headroom turned off", "the proxy stopped — restored your setup")
                    return
                if self.ctx.load_state().settings().get("headroom"):
                    ok_rtk, _ = headroom.verify_rtk(self.ctx.data_dir)
                    if not ok_rtk:        # supply-chain tamper while routing was live → tear it down
                        headroom.global_disable(self.ctx.data_dir)
                        with self.ctx.locked():
                            s = self.ctx.load_state(); s.set_setting("headroom", False); s.save()
                        self._notify("Headroom turned off", "its helper binary changed unexpectedly")
            except Exception:
                pass

        def applyResult_(self, result):
            self._pushResult(result)
            self._updateDot(result.get("state"))

        def bgToggle_(self, msg):
            result = bridge.handle(self.ctx, dict(msg))   # global enable/disable runs here, off-main
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)

        @objc.python_method
        def _headroomTeardown(self):
            """On quit: remove global Headroom routing + restore config so codex/claude work while
            the app is closed. The `headroom` SETTING is intentionally KEPT so it re-applies on the
            next launch (the preference persists across restarts). Guarded so the quit action and
            applicationWillTerminate_ don't both run it; uses a short timeout so quit can't beachball
            for long — if remove doesn't finish, the next launch/cx/cl heal() strips the leftover."""
            if getattr(self, "_teardownDone", False):
                return
            self._teardownDone = True
            try:
                from acctsw import headroom
                if self.ctx.load_state().settings().get("headroom"):
                    # non-blocking: if a background op holds the lock, don't freeze quit — the next
                    # launch / cx / cl reconcile() strips any leftover routing.
                    headroom.global_disable(self.ctx.data_dir, timeout=15, blocking=False)
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
                self._headroomTeardown()   # auto-unwrap global routing so codex/claude stay working
                NSApplication.sharedApplication().terminate_(self)
                return
            if action == "settings":
                return  # reserved
            if action == "toggle" and msg.get("key") == "headroom":
                # global apply/remove is slow (subprocess) → run off the main thread
                self._notify("Headroom", "turning on…" if msg.get("value") else "turning off…")
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.bgToggle_, signature=b"v@:@"), msg)
                return
            if action == "login":
                # native: sync-back-before-login (invariant) + run the chosen flow in Terminal
                from .terminal import prepare_then_login
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
            self.statusItem.button().setTitle_(DOT_GLYPH.get(state.get("dot", "fresh"), "🎟️"))

        @objc.python_method
        def _notify(self, title, text):
            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(text)
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
