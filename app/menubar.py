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

from acctsw import appalive, bridge
from acctsw.context import Context, hydrate_path

WEB_DIR = Path(__file__).resolve().parent / "web"
USAGE_POLL_SECONDS = 180.0
# The menu-bar mark is the door (icon handoff): open onto the disco when a model's free, shut when
# every seat is resting. SF Symbols give a native, template (auto light/dark) glyph; emoji is the
# fallback on older macOS where the symbol is missing (🪩 disco = open, 🚪 = shut).
DOOR_SYMBOL = {"open": "door.left.hand.open", "shut": "door.left.hand.closed"}
DOOR_EMOJI = {"open": "🪩", "shut": "🚪"}
NS_TERMINATE_NOW = 1      # NSApplicationTerminateReply.terminateNow


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
            self._acctWarned = set()              # shared-account warnings already toasted this session
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
            result = bridge.handle(self.ctx, {"action": "usage"})
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)

        def pasteBg_(self, msg):
            # `paste` verifies creds against the provider (Claude shells out / hits the network), so
            # run it off the main thread — a synchronous handle() would beachball the app behind the
            # add-seat "saving your seat…" spinner. ctx.locked() already serialises against the poll.
            result = bridge.handle(self.ctx, dict(msg))
            result["_action"] = "paste"
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(self.applyResult_, signature=b"v@:@"), result, False)

        def applyResult_(self, result):
            self._pushResult(result)
            self._updateDot(result.get("state"))
            self._notifyAccountWarnings(result)
            # A backgrounded add (paste/snapshot) still deserves the "seat added" nudge the main-thread
            # path fires. Keyed on `added` so it says the right thing whether it was a paste or a login.
            if result.pop("_action", None) == "paste" and result.get("added") and result.get("ok") \
                    and self.ctx.load_state().settings().get("notify", True):
                self._notify("seat saved ✨", "your seat's on the floor")

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
                # native: sync-back-before-login (invariant) + run the chosen flow in Terminal.
                # Absolute import (not `.terminal`): under py2app the main script runs as top-level
                # __main__ with no package context, so a relative import would fail in the .app.
                from app.terminal import prepare_then_login
                # The old modal sent `command`; the new sub-view sends `method` and the command is
                # resolved by the engine. Honour both while the modal is still present.
                command = msg.get("command") or bridge.login_command(
                    msg["tool"], msg.get("method", "browser"))
                prepare_then_login(self.ctx, msg["tool"], command)
                self._notify("finish signing in", "then tap ‘save my seat’ 🎟️")
                self._pushResult({"ok": True, "await_snapshot": True, "tool": msg["tool"]})
                return
            if action == "paste":
                # off the main thread (see pasteBg_) — Claude verification can touch the network.
                self.performSelectorInBackground_withObject_(
                    objc.selector(self.pasteBg_, signature=b"v@:@"), msg)
                return

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
