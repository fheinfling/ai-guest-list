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
    from Foundation import NSObject, NSURL, NSTimer, NSMakeRect
except ImportError:  # allows importing this module's pure helpers without pyobjc installed
    objc = None

from acctsw import bridge
from acctsw.context import Context

WEB_DIR = Path(__file__).resolve().parent / "web"
USAGE_POLL_SECONDS = 180.0
DOT_GLYPH = {"fresh": "🟢", "resting": "🟡", "switched": "🔵", "hello": "🌸"}


def _push_state(webview, result: dict) -> None:
    """Send a bridge result's state into the web view via window.AGL.update()."""
    if not webview or "state" not in result:
        return
    payload = json.dumps(result["state"])
    webview.evaluateJavaScript_completionHandler_(f"window.AGL.update({payload});", None)
    if result.get("celebrate"):
        webview.evaluateJavaScript_completionHandler_("window.AGL.celebrate&&window.AGL.celebrate();", None)


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
            self._buildPopover()
            self._startUsageTimer()

        @objc.python_method
        def _buildPopover(self):
            cfg = WKWebViewConfiguration.alloc().init()
            ucc = WKUserContentController.alloc().init()
            ucc.addScriptMessageHandler_name_(self, "agl")
            cfg.setUserContentController_(ucc)
            self.webview = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 348, 560), cfg)
            self.webview.loadFileURL_allowingReadAccessToURL_(
                NSURL.fileURLWithPath_(str(WEB_DIR / "index.html")),
                NSURL.fileURLWithPath_(str(WEB_DIR)))
            vc = NSViewController.alloc().init()
            vc.setView_(self.webview)
            self.popover = NSPopover.alloc().init()
            self.popover.setContentViewController_(vc)
            self.popover.setBehavior_(1)  # NSPopoverBehaviorTransient

        @objc.python_method
        def _startUsageTimer(self):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                USAGE_POLL_SECONDS, self, objc.selector(self.pollUsage_, signature=b"v@:@"), None, True)

        # --- actions ------------------------------------------------------------------------
        def togglePopover_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
            else:
                btn = self.statusItem.button()
                self.popover.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, 1)

        def pollUsage_(self, _timer):
            result = bridge.handle(self.ctx, {"action": "usage"})
            _push_state(self.webview, result)
            self._updateDot(result.get("state"))

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
            if action == "add":
                self._handleAdd(msg)
                return
            if action == "headroom_install":
                from .terminal import open_in_terminal
                from acctsw.headroom import INSTALL_COMMAND
                open_in_terminal(INSTALL_COMMAND)
                self._notify("installing headroom", "i'll enable save-credit once it's ready ✨")
                return
            result = bridge.handle(self.ctx, msg)
            if action == "dot":
                self._updateDot(result.get("state"))
                return
            _push_state(self.webview, result)
            self._updateDot(result.get("state"))
            if action == "switch" and result.get("ok") and self.ctx.load_state().settings().get("notify", True):
                self._notify("just switched you ✨", f"{msg.get('email')} is on the floor")

        @objc.python_method
        def _handleAdd(self, msg):
            plan = bridge.login_plan(msg["tool"])
            # Sync-back the active seat FIRST (invariant), then run the official login in Terminal.
            from .terminal import prepare_then_login
            prepare_then_login(self.ctx, msg["tool"], plan["methods"][0].get("command"))
            self._notify("finish signing in", "come back and i'll save the seat 🎟️")

        # --- helpers ------------------------------------------------------------------------
        @objc.python_method
        def _updateDot(self, state):
            if not state:
                return
            from .web_state import dot_for  # pure python mirror of render.dotState
            self.statusItem.button().setTitle_(DOT_GLYPH.get(dot_for(state), "🎟️"))

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
