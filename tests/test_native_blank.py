"""Blank-popover recovery contracts, using fakes even when PyObjC is unavailable."""
import ast
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def native(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "app" / "menubar.py"
    # Import the whole shell with fake framework boundaries; never allocate AppKit or WebKit.
    for framework in ("AppKit", "WebKit", "Foundation"):
        module = ModuleType(framework)
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == framework:
                for alias in node.names:
                    setattr(module, alias.name, object())
        monkeypatch.setitem(sys.modules, framework, module)
    class NSObject:
        def init(self):
            return self
    sys.modules["Foundation"].NSObject = NSObject
    sys.modules["Foundation"].NSURL = SimpleNamespace(fileURLWithPath_=lambda path: path)
    objc = ModuleType("objc")
    objc.python_method = lambda fn: fn
    objc.super = super
    objc.selector = lambda fn, **kw: fn
    monkeypatch.setitem(sys.modules, "objc", objc)
    spec = importlib.util.spec_from_file_location("app._blank_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.logs = []
    module.NSLog = lambda fmt, *args: module.logs.append((fmt, args))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: pytest.fail("subprocess"))
    monkeypatch.setattr(module.bridge, "handle", lambda *a, **kw: pytest.fail("unexpected engine call"))
    return module


def receiver(native):
    loads, events = [], []
    obj = SimpleNamespace(_web_failed=False, _web_reload_times=[],
        webview=SimpleNamespace(loadFileURL_allowingReadAccessToURL_=lambda *urls: loads.append(urls),
            evaluateJavaScript_completionHandler_=lambda js, _done: events.append(("javascript", js))),
        popover=SimpleNamespace(isShown=lambda: False,
            showRelativeToRect_ofView_preferredEdge_=lambda *args: events.append("show")),
        statusItem=SimpleNamespace(button=lambda: SimpleNamespace(bounds=lambda: "bounds")),
        _setWebVisible=lambda visible: events.append(("visible", visible)),
        _startStateTimer=lambda: None, _setUsagePollInterval=lambda _: None, pollUsage_=lambda _: None)
    obj._loadWebPage = lambda: native.AGLDelegate._loadWebPage(obj)
    obj._reloadWebPage = lambda: native.AGLDelegate._reloadWebPage(obj)
    return obj, loads, events


def dispatch(native, obj, body):
    native.AGLDelegate.userContentController_didReceiveScriptMessage_(obj, None,
        SimpleNamespace(body=lambda: body))


def test_termination_logs_and_reloads_the_local_page(native):
    obj, loads, _ = receiver(native)
    native.AGLDelegate.webViewWebContentProcessDidTerminate_(obj, obj.webview)
    assert loads == [(str(native.WEB_DIR / "index.html"), str(native.WEB_DIR))]
    assert obj._web_failed is True
    assert native.logs == [("[agl web] web content process terminated", ())]


def test_reload_loop_guard_applies_to_termination_reopen_and_button(native, monkeypatch):
    obj, loads, _ = receiver(native)
    clock = [10.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    for _ in range(8):
        native.AGLDelegate.webViewWebContentProcessDidTerminate_(obj, obj.webview)
        native.AGLDelegate.togglePopover_(obj, None)
        dispatch(native, obj, {"action": "reload"})
    assert len(loads) == 3
    assert obj._web_failed is True
    assert any("reload limit reached" in fmt for fmt, _ in native.logs)
    # Neither a good render nor ready may reset the rolling budget.
    dispatch(native, obj, {"action": "client_health", "ok": True})
    dispatch(native, obj, {"action": "reload"})
    assert len(loads) == 3
    clock[0] += 60
    native.AGLDelegate.togglePopover_(obj, None)
    assert len(loads) == 4


def test_popover_open_after_termination_reloads_before_showing(native):
    obj, loads, events = receiver(native)
    native.AGLDelegate.webViewWebContentProcessDidTerminate_(obj, obj.webview)
    obj.popover.showRelativeToRect_ofView_preferredEdge_ = lambda *a: events.append(len(loads))
    native.AGLDelegate.togglePopover_(obj, None)
    assert events[0] == 2


def test_reload_limit_tells_the_fallback_when_to_retry(native, monkeypatch):
    obj, loads, events = receiver(native)
    clock = [10.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    for _ in range(native.WEB_RELOAD_LIMIT):
        dispatch(native, obj, {"action": "reload"})
    assert events == []
    dispatch(native, obj, {"action": "reload"})
    assert len(loads) == native.WEB_RELOAD_LIMIT
    assert obj._web_failed is True
    assert len(events) == 1
    kind, script = events[0]
    assert kind == "javascript"
    assert 'document.querySelector(\'#root [role="alert"] p\')' in script
    assert 'if (text)' in script  # safe when the page has no fallback
    assert 'text.textContent = "still not drawing — try again in a minute"' in script
    assert "AGL" not in script  # the renderer and its bridge may be broken
    clock[0] += native.WEB_RELOAD_WINDOW_S
    dispatch(native, obj, {"action": "reload"})
    assert len(loads) == native.WEB_RELOAD_LIMIT + 1
    assert len(events) == 1


@pytest.mark.parametrize("failure", [{"action": "client_error", "message": "drawing failed"},
                                     {"action": "client_health", "ok": False}])
def test_reopening_after_client_failure_reloads_and_good_render_clears_flag(native, failure):
    obj, loads, _ = receiver(native)
    dispatch(native, obj, failure)
    native.AGLDelegate.togglePopover_(obj, None)
    assert len(loads) == 1 and obj._web_failed is True
    dispatch(native, obj, {"action": "client_health", "ok": True})
    native.AGLDelegate.togglePopover_(obj, None)
    assert len(loads) == 1 and obj._web_failed is False


def test_client_error_logs_only_redacted_bounded_message(native):
    obj, _, _ = receiver(native)
    secrets = ["owner@example.com", "sk-example-test-key", "bearer-test-token",
               "assigned-secret", "quoted-secret", "eyJheader.eyJpayload.signature",
               "short-access", "short-api", "private-jwt-payload"]
    message = ('drawing failed for owner@example.com sk-example-test-key Bearer bearer-test-token '
               'secret=assigned-secret "quoted-secret" eyJheader.eyJpayload.signature '
               'access_token=short-access api_key=short-api '
               'eyJlongheaderlongheaderlongheader.private-jwt-payload.signature '
               'https://example.com/private?token=bad file:///Users/owner/private.js '
               + 'word ' * 1000 + 'sk-trailing-secret')
    dispatch(native, obj, {"action": "client_error", "message": message,
        "stack": "private stack owner@example.com", "state": {"secret": "private-state"},
        "secret": "private-payload", "location": "private-location"})
    assert obj._web_failed is True
    assert len(native.logs) == 1
    fmt, (logged,) = native.logs[0]
    assert fmt == "[agl web] client_error: %@"  # free text is never an NSLog format string
    assert logged.startswith("drawing failed for") and "[redacted]" in logged
    assert len(logged) <= 1024
    assert all(secret not in logged for secret in secrets)
    assert all(text not in logged for text in ("private stack", "private-state", "private-payload", "private-location"))


@pytest.mark.parametrize("message", [None, {}, 123])
def test_invalid_client_diagnostic_is_not_stringified(native, message):
    obj, _, _ = receiver(native)
    dispatch(native, obj, {"action": "client_error", "message": message})
    assert native.logs == [("[agl web] client_error: %@", ("unknown client error",))]


@pytest.mark.parametrize("token, cut", [("eyJh.e.payload", 7), ("sk-private-secret", 2)])
def test_client_error_drops_partial_tokens_at_the_input_bound(native, token, cut):
    obj, _, _ = receiver(native)
    prefix = "drawing failed "
    message = prefix + " " * (8192 - len(prefix) - cut) + token + " ignored tail"
    dispatch(native, obj, {"action": "client_error", "message": message})
    _, (logged,) = native.logs[0]
    assert logged == prefix.strip()


def test_reload_button_is_handled_natively(native):
    obj, loads, _ = receiver(native)
    dispatch(native, obj, {"action": "reload"})
    assert len(loads) == 1 and obj._web_failed is True


def test_build_popover_sets_navigation_delegate_before_initial_load(native):
    events = []
    class Boundary:
        def alloc(self): return self
        def init(self): return self
        def initWithFrame_configuration_(self, *args): return self
        def __getattr__(self, name):
            return lambda *args: events.append((name, args))
    for name in ("WKWebViewConfiguration", "WKUserContentController", "WKWebView",
                 "NSViewController", "NSPopover"):
        setattr(native, name, Boundary())
    native.NSMakeRect = lambda *args: args
    native.NSMakeSize = lambda *args: args
    obj, _, _ = receiver(native)
    native.AGLDelegate._buildPopover(obj)
    delegate = events.index(("setNavigationDelegate_", (obj,)))
    load = next(i for i, (name, _) in enumerate(events) if name == "loadFileURL_allowingReadAccessToURL_")
    assert delegate < load


def test_ready_after_reload_immediately_pushes_fresh_state(native, monkeypatch):
    obj, _, _ = receiver(native)
    native.AGLDelegate.webViewWebContentProcessDidTerminate_(obj, obj.webview)
    snapshot = {"ok": True, "state": {"rev": 42}}
    calls = []
    obj.ctx = object()
    obj._pushResult = lambda result: calls.append(result)
    obj._updateDot = lambda state: None
    monkeypatch.setattr(native.bridge, "handle", lambda ctx, msg: snapshot)
    dispatch(native, obj, {"action": "ready"})
    assert calls == [snapshot]
    # State delivery/navigation isn't proof of a successful paint.
    assert obj._web_failed is True
