"""Native state, notices, and dispatch through real PyObjC callables, with no AppKit loop."""
from types import SimpleNamespace

import pytest

from app import menubar

pytestmark = pytest.mark.skipif(menubar.objc is None, reason="native callbacks require PyObjC")


def call(name, receiver, *args):
    return getattr(menubar.AGLDelegate, name).callable(receiver, *args)


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    monkeypatch.setattr(menubar.subprocess, "Popen",
                        lambda *_a, **_k: pytest.fail("native contract spawned a subprocess"))
    monkeypatch.setattr(menubar.bridge, "handle",
                        lambda *_a, **_k: pytest.fail("unexpected bridge dispatch"))


@pytest.mark.parametrize("settings,notify", [(None, True), ({}, True),
                                             ({"notify": True}, True), ({"notify": False}, False)])
@pytest.mark.parametrize("tool,email", [("codex", "next@example.com"),
                                        ("claude", "next@example.com"), (None, None)])
def test_usage_handoff_notifies_restarts_and_primes_new_seat(settings, notify, tool, email):
    events = []
    receiver = SimpleNamespace(
        _usage_poll_inflight=True, _pending_usage_request={"force": True, "only": "pending@example.com"},
        applyResult_=lambda result: events.append(("apply", result)),
        _notify=lambda *args: events.append(("notice", args)),
        _restartCodexAfterSwap_=lambda tool: events.append(("restart", tool)),
        pollUsage_=lambda request: events.append(("poll", request)))
    result = {"auto_switch": {"status": "switched", "tool": tool,
                               "from": "old@example.com", "to": email},
              "signin_notices": [{"title": "sign in", "text": "credentials expired"}]}
    if settings is not None:
        result["state"] = {"settings": settings}
    call("finishUsagePoll_", receiver, result)
    expected = [("apply", result), ("notice", ("sign in", "credentials expired"))]
    if notify:
        expected.append(("notice", (f"switched {tool} ✨",
                         f"old@example.com hit its usage limit — {email} is on now")))
    expected.append(("restart", tool))
    if tool and email:
        expected.append(("poll", {"scope": "active", "tool": tool, "only": email, "force": True}))
    expected.append(("poll", {"force": True, "only": "pending@example.com"}))
    assert events == expected
    assert receiver._usage_poll_inflight is False and receiver._pending_usage_request is None


@pytest.mark.parametrize("error", [None, "", "locked"])
def test_usage_handoff_failure_is_notified_once_per_distinct_failure(error):
    notices = []
    receiver = SimpleNamespace(_usage_poll_inflight=True, _pending_usage_request=None,
        _auto_handoff_warnings=set(), applyResult_=lambda _: None,
        _notify=lambda *args: notices.append(args))
    handoff = {"status": "failed", "tool": "claude", "from": "old@example.com",
               "to": "next@example.com", "error": error}
    for _ in range(2):
        call("finishUsagePoll_", receiver, {"auto_switch": handoff})
    assert notices == ([("couldn't switch automatically", error)] if error else [])
    handoff = {**handoff, "error": "changed failure"}
    call("finishUsagePoll_", receiver, {"auto_switch": handoff})
    assert notices[-1] == ("couldn't switch automatically", "changed failure")
    assert len(receiver._auto_handoff_warnings) == (2 if error else 1)


def test_state_signature_tracks_revision_and_each_session_heartbeat(ctx, monkeypatch):
    state = ctx.load_state()
    state.save()
    beats = {tool: index + 10 for index, tool in enumerate(menubar.TOOLS)}
    calls = []
    def mtime(store, tool):
        assert store == ctx.data_dir
        calls.append(tool)
        return beats[tool]
    monkeypatch.setattr(menubar.session, "session_mtime_ns", mtime)
    receiver = SimpleNamespace(ctx=ctx)
    signature = menubar.AGLDelegate._stateSignature(receiver)
    assert signature == (ctx.load_state().data["rev"], tuple(beats.values()))
    assert calls == list(menubar.TOOLS)
    beats[menubar.TOOLS[-1]] += 1
    assert menubar.AGLDelegate._stateSignature(receiver) != signature
    state.save()
    assert menubar.AGLDelegate._stateSignature(receiver)[0] > signature[0]


@pytest.mark.parametrize("failure", ["load", "revision", "heartbeat"])
def test_state_signature_handles_unreadable_state_and_heartbeats(monkeypatch, failure):
    errors = []
    monkeypatch.setattr(menubar, "_log_exception", errors.append)
    def load():
        if failure == "load":
            raise OSError("unreadable")
        return SimpleNamespace(data={"rev": "invalid" if failure == "revision" else 7})
    def mtime(store, tool):
        if tool == menubar.TOOLS[0]:
            raise OSError("unreadable heartbeat")
        return 9
    monkeypatch.setattr(menubar.session, "session_mtime_ns", mtime)
    receiver = SimpleNamespace(ctx=SimpleNamespace(load_state=load, data_dir=object()))
    result = menubar.AGLDelegate._stateSignature(receiver)
    assert result == ((7, (0, 9)) if failure == "heartbeat" else None)
    assert len(errors) == 1


def test_account_warnings_use_nested_state_and_dedupe_exact_messages():
    notices = []
    receiver = SimpleNamespace(_acctWarned=set(), _notify=lambda *args: notices.append(args))
    method = menubar.AGLDelegate._notifyAccountWarnings
    for result in ({}, {"state": None}, {"warnings": ["ignored"]}):
        method(receiver, result)
    for warnings in (["shared account"], ["shared account", "changed warning"]):
        method(receiver, {"state": {"warnings": warnings}})
    assert notices == [("heads up — seats share one account", "shared account"),
                       ("heads up — seats share one account", "changed warning")]
    assert receiver._acctWarned == {"shared account", "changed warning"}


def test_engine_warnings_dedupe_identities_and_only_toast_hard_notices():
    notices = []
    receiver = SimpleNamespace(_engineWarned=set(), _notify=lambda *args: notices.append(args))
    method = menubar.AGLDelegate._notifyEngineWarnings
    for result in ({}, {"state": None}):
        method(receiver, result)
    warnings = [None, {"text": "soft", "severity": "soft"},
                {"kind": "app", "text": "update app"},
                {"kind": "sessions", "text": "old terminal", "notify_ids": ["session-1"]}]
    method(receiver, {"state": {"engine_warnings": warnings}})
    method(receiver, {"state": {"engine_warnings": warnings}})
    method(receiver, {"state": {"engine_warnings": [
        {"kind": "sessions", "text": "changed start time", "notify_ids": ["session-1"]},
        {"kind": "sessions", "text": "new terminal", "notify_ids": ["session-1", "session-2"]}]}})
    assert notices == [("AI Guest List is out of date", "update app"),
        ("heads up — terminals run an older engine", "old terminal"),
        ("heads up — terminals run an older engine", "new terminal")]
    assert receiver._engineWarned == {("app", "update app"),
                                      ("sessions", "session-1"), ("sessions", "session-2")}


@pytest.mark.parametrize("action,handler", [
    *[(action, "keyBg_") for action in sorted(menubar.bridge.KEY_ACTIONS)],
    ("paste", "addBg_"), ("snapshot", "addBg_"), ("import_current", "addBg_"),
    ("adopt_engine", "adoptEngineBg_"), ("update_check", "updateCheckBg_"),
    ("update_install", "updateInstallBg_"), ("switch", "switchBg_"),
])
def test_script_dispatch_runs_blocking_actions_in_background(action, handler):
    dispatched = []
    target = lambda _: None
    receiver = SimpleNamespace(_update_inflight=False,
        performSelectorInBackground_withObject_=lambda *args: dispatched.append(args),
        **{handler: target})
    msg = {"action": action, "tool": "codex", "id": "fake-seat"}
    call("userContentController_didReceiveScriptMessage_", receiver, None,
         SimpleNamespace(body=lambda: msg))
    assert len(dispatched) == 1
    selector, argument = dispatched[0]
    assert selector.callable is target and argument == msg
    if action == "update_install":
        assert receiver._update_inflight is True
        call("userContentController_didReceiveScriptMessage_", receiver, None,
             SimpleNamespace(body=lambda: msg))
        assert len(dispatched) == 1


@pytest.mark.parametrize("action", ["status", "dot"])
def test_script_dispatch_applies_bridge_reply_and_updates_dot(monkeypatch, action):
    events = []
    result = {"ok": True, "state": {"dot": "open"}}
    receiver = SimpleNamespace(ctx=object(), _pushResult=lambda reply: events.append(("reply", reply)),
                               _updateDot=lambda state: events.append(("dot", state)))
    msg = {"action": action}
    def handle(ctx, message):
        assert ctx is receiver.ctx and message == msg
        return result
    monkeypatch.setattr(menubar.bridge, "handle", handle)
    call("userContentController_didReceiveScriptMessage_", receiver, None,
         SimpleNamespace(body=lambda: msg))
    assert events == ([] if action == "dot" else [("reply", result)]) + [("dot", result["state"])]


@pytest.mark.parametrize("action", ["resize", "quit", "reveal", "update_notes"])
def test_script_dispatch_native_actions(monkeypatch, action):
    events = []
    receiver = SimpleNamespace(popover=object(), _revealCodexAuth=lambda: events.append("reveal"),
                               _openUpdateNotes=lambda: events.append("update_notes"))
    monkeypatch.setattr(menubar, "resize_popover", lambda popover, height, make_size:
                        events.append((popover, height, make_size)))
    monkeypatch.setattr(menubar, "NSApplication", SimpleNamespace(sharedApplication=lambda:
        SimpleNamespace(terminate_=lambda sender: events.append(("quit", sender)))))
    call("userContentController_didReceiveScriptMessage_", receiver, None,
         SimpleNamespace(body=lambda: {"action": action, "height": 321}))
    expected = {"resize": (receiver.popover, 321, menubar.NSMakeSize), "quit": ("quit", receiver),
                "reveal": "reveal", "update_notes": "update_notes"}
    assert events == [expected[action]]


def test_script_dispatch_logs_invalid_message_and_returns(monkeypatch):
    errors = []
    monkeypatch.setattr(menubar, "_log_exception", errors.append)
    call("userContentController_didReceiveScriptMessage_", SimpleNamespace(), None,
         SimpleNamespace(body=lambda: None))
    assert len(errors) == 1 and isinstance(errors[0], TypeError)
