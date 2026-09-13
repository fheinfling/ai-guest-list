"""Exercise native callbacks without starting AppKit, providers, or real applications."""
from types import SimpleNamespace

import pytest

from app import menubar

pytestmark = pytest.mark.skipif(menubar.objc is None, reason="native callbacks require PyObjC")


def call(name, receiver, argument):
    getattr(menubar.AGLDelegate, name).callable(receiver, argument)


def test_overlapping_polls_keep_one_targeted_followup():
    dispatched = []
    applied = []
    receiver = SimpleNamespace(
        _usage_poll_inflight=False, _pending_usage_request=None,
        pollBg_=lambda _: None,
        performSelectorInBackground_withObject_=lambda selector, arg: dispatched.append(arg),
        applyResult_=applied.append,
    )
    receiver.pollUsage_ = lambda request: call("pollUsage_", receiver, request)
    receiver.pollUsage_(None)
    receiver.pollUsage_(None)
    target = {"scope": "active", "tool": "claude", "only": "spare@test.example", "force": True}
    receiver.pollUsage_(target)
    receiver.pollUsage_(target)
    assert dispatched == [None]
    call("finishUsagePoll_", receiver, {"ok": False, "background": True, "error": "offline"})
    assert len(applied) == 1
    assert dispatched == [None, target]
    assert receiver._usage_poll_inflight is True
    call("finishUsagePoll_", receiver, {"ok": True})
    assert receiver._usage_poll_inflight is False


def test_restart_waits_for_exit_then_launches_once(monkeypatch):
    apps = [SimpleNamespace(bundleIdentifier=lambda: "com.openai.codex", localizedName=lambda: "Codex")]
    monkeypatch.setattr(menubar, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda:
                        SimpleNamespace(runningApplications=lambda: apps)))
    events = []
    receiver = SimpleNamespace(_codex_restart_attempts=0, _codex_restart_pending=True,
                               launchCodexAfterSwap_=lambda _: events.append("launch"))
    timer = SimpleNamespace(invalidate=lambda: events.append("stop timer"))
    call("waitForCodexExit_", receiver, timer)
    assert events == []
    apps.clear()
    call("waitForCodexExit_", receiver, timer)
    assert events == ["stop timer", "launch"]


def test_restart_timeout_reports_failure_without_launching(monkeypatch):
    app = SimpleNamespace(bundleIdentifier=lambda: "com.openai.codex", localizedName=lambda: "Codex")
    monkeypatch.setattr(menubar, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda:
                        SimpleNamespace(runningApplications=lambda: [app])))
    events = []
    receiver = SimpleNamespace(_codex_restart_attempts=39, _codex_restart_pending=True,
                               _notify=lambda title, detail: events.append((title, detail)),
                               launchCodexAfterSwap_=lambda _: pytest.fail("app has not exited"))
    timer = SimpleNamespace(invalidate=lambda: None)
    call("waitForCodexExit_", receiver, timer)
    assert receiver._codex_restart_pending is False
    assert events == [("couldn't restart Codex", "Codex did not quit within 10 seconds")]
