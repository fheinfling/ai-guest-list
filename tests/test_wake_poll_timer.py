"""Wake polling uses coalesced sweeps and retries through the real engine backoff."""
import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import menubar
from acctsw import usage
from acctsw.util import now
from tests.conftest import make_codex_blob
from tests.test_usage import codex_ok_body

native = pytest.mark.skipif(menubar.objc is None, reason="native callbacks require PyObjC")


def call(name, receiver, argument):
    return getattr(menubar.AGLDelegate, name).callable(receiver, argument)


@pytest.fixture
def callbacks():
    """Execute the actual callback bodies with fake dispatch even without PyObjC/AppKit."""
    names = {"systemDidWake_", "pollUsageAfterWake_", "pollUsage_", "pollBg_", "finishUsagePoll_"}
    tree = ast.parse(Path(menubar.__file__).read_text())
    delegate = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef) and node.name == "AGLDelegate")
    methods = [node for node in delegate.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    namespace = dict(vars(menubar))
    namespace["objc"] = SimpleNamespace(selector=lambda function, signature:
        SimpleNamespace(callable=function, signature=signature))
    exec(compile(ast.Module(body=methods, type_ignores=[]), menubar.__file__, "exec"), namespace)
    return SimpleNamespace(globals=namespace,
                           call=lambda name, receiver, argument: namespace[name](receiver, argument))


def test_wake_poll_delay_is_shorter_than_an_open_popover_tick():
    assert 0 < menubar.WAKE_POLL_DELAY_SECONDS == 5.0 < menubar.USAGE_POLL_OPEN_SECONDS


def test_wake_followup_lands_after_offline_retry_delay():
    assert menubar.WAKE_FOLLOWUP_SECONDS == 40.0 > usage.OFFLINE_RETRY_SECONDS


@native
def test_delegate_initializes_without_a_pending_wake_poll():
    receiver = menubar.AGLDelegate.alloc().initWithContext_(object())
    assert receiver._wake_poll_timer is None
    assert receiver._wake_followup_timer is None


def test_wake_schedules_two_one_shot_polls_and_replaces_both_timers(callbacks):
    timers, polls = [], []

    def schedule(interval, target, selector, user_info, repeats):
        assert target is receiver and user_info is None and repeats is False
        assert selector.signature == b"v@:@"
        timer = SimpleNamespace(invalidated=False, interval=interval)
        timer.invalidate = lambda: setattr(timer, "invalidated", True)
        timer.fire = lambda: selector.callable(timer)
        timers.append(timer)
        return timer

    callbacks.globals["NSTimer"] = SimpleNamespace(
        scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=schedule)
    receiver = SimpleNamespace(_wake_poll_timer=None, _wake_followup_timer=None, pollUsage_=polls.append)
    receiver.pollUsageAfterWake_ = lambda timer: callbacks.call("pollUsageAfterWake_", receiver, timer)

    callbacks.call("systemDidWake_", receiver, None)
    assert len(timers) == 2
    assert receiver._wake_poll_timer is timers[0] and receiver._wake_followup_timer is timers[1]
    assert all(not timer.invalidated for timer in timers) and polls == []
    callbacks.call("systemDidWake_", receiver, None)
    assert len(timers) == 4
    assert receiver._wake_poll_timer is timers[2] and receiver._wake_followup_timer is timers[3]
    assert [timer.invalidated for timer in timers] == [True, True, False, False]
    assert [timer.interval for timer in timers] == [5.0, 40.0, 5.0, 40.0]
    assert polls == []

    timers[2].fire()
    assert polls == [{"scope": "all"}]
    assert receiver._wake_poll_timer is None
    assert receiver._wake_followup_timer is timers[3]
    timers[3].fire()
    assert polls == [{"scope": "all"}, {"scope": "all"}]
    assert receiver._wake_poll_timer is None and receiver._wake_followup_timer is None


@pytest.mark.parametrize("pending_forced", [False, True])
@pytest.mark.parametrize("timer_name", ["_wake_poll_timer", "_wake_followup_timer"])
def test_wake_during_inflight_poll_dispatches_retained_request(callbacks, pending_forced, timer_name):
    dispatched, applied = [], []
    target = {"scope": "active", "tool": "claude", "only": "spare@example.test", "force": True}
    receiver = SimpleNamespace(
        _wake_poll_timer=object(), _wake_followup_timer=object(),
        _usage_poll_inflight=True, _pending_usage_request=None,
        pollBg_=lambda _: None, applyResult_=applied.append,
        performSelectorInBackground_withObject_=lambda selector, arg: dispatched.append(arg),
    )
    receiver.pollUsage_ = lambda request: callbacks.call("pollUsage_", receiver, request)
    if pending_forced:
        receiver.pollUsage_(target)
    timer = getattr(receiver, timer_name)
    callbacks.call("pollUsageAfterWake_", receiver, timer)
    callbacks.call("pollUsageAfterWake_", receiver, timer)  # repeated requests coalesce
    expected = target if pending_forced else {"scope": "all"}
    assert getattr(receiver, timer_name) is None and dispatched == []
    assert receiver._pending_usage_request == expected

    callbacks.call("finishUsagePoll_", receiver, {"ok": False, "background": True, "error": "offline"})
    assert len(applied) == 1
    assert dispatched == [expected]
    assert receiver._pending_usage_request is None and receiver._usage_poll_inflight
    callbacks.call("finishUsagePoll_", receiver, {"ok": True})
    assert dispatched == [expected] and not receiver._usage_poll_inflight


def test_forced_request_replaces_pending_all_scope_poll(callbacks):
    dispatched = []
    receiver = SimpleNamespace(
        _usage_poll_inflight=True, _pending_usage_request=None,
        pollBg_=lambda _: None, applyResult_=lambda _: None,
        performSelectorInBackground_withObject_=lambda selector, arg: dispatched.append(arg),
    )
    receiver.pollUsage_ = lambda request: callbacks.call("pollUsage_", receiver, request)
    receiver.pollUsage_({"scope": "all"})
    assert receiver._pending_usage_request == {"scope": "all"}
    target = {"scope": "active", "tool": "codex", "only": "new@example.test", "force": True}
    receiver.pollUsage_(target)
    receiver.pollUsage_({"scope": "all"})
    callbacks.call("finishUsagePoll_", receiver, {"ok": True})
    assert dispatched == [target]


def test_wake_followup_retries_real_get_after_retained_sweep_was_cached(ctx, monkeypatch, callbacks):
    start, elapsed = now(), 0
    monkeypatch.setattr(usage, "now", lambda: start + timedelta(seconds=elapsed))
    callbacks.globals["time"] = SimpleNamespace(monotonic=lambda: 1000 + elapsed)
    callbacks.globals["appalive"] = SimpleNamespace(mark_alive=lambda _: None)
    email = "person@example.test"
    state = ctx.load_state()
    state.upsert_seat("codex", email)
    state.set_active("codex", email)
    ctx.snapshot_set("codex", email, make_codex_blob(email))
    state.save()
    timers, background, main_thread, gets, sweeps = [], [], [], [], []

    def schedule(interval, target, selector, user_info, repeats):
        assert user_info is None and repeats is False
        timer = SimpleNamespace(interval=interval, invalidate=lambda: None)
        timer.fire = lambda: selector.callable(timer)
        timers.append(timer)
        return timer

    callbacks.globals["NSTimer"] = SimpleNamespace(
        scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=schedule)
    receiver = SimpleNamespace(ctx=ctx, _wake_poll_timer=None, _wake_followup_timer=None,
        _usage_poll_inflight=False, _pending_usage_request=None,
        _usage_interval=menubar.USAGE_POLL_HIDDEN_SECONDS, _last_all_usage_poll=0.0,
        applyResult_=lambda _: None,
        performSelectorInBackground_withObject_=lambda selector, arg: background.append((selector, arg)),
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda selector, arg, wait:
            main_thread.append((selector, arg)))
    for name in ("pollUsage_", "pollBg_", "finishUsagePoll_", "pollUsageAfterWake_"):
        setattr(receiver, name, lambda argument, name=name: callbacks.call(name, receiver, argument))

    def get(url, headers, timeout):
        nonlocal elapsed
        gets.append(elapsed)
        if len(gets) == 1:
            # The initial request starts at wake, and the 5s timer fires before it finishes.
            elapsed = 5
            timers[0].fire()
            assert receiver._pending_usage_request == {"scope": "all"}
            elapsed = 6
            return usage.HttpReply(0, "", None, error="offline")
        return 200, codex_ok_body()

    def handle(context, request):
        assert request["action"] == "usage" and not request.get("force")
        result = usage.refresh_live(context, "codex", active_only=request["scope"] == "active", get=get)
        sweeps.append((elapsed, request["scope"], result["codex"][email]))
        return {"ok": True}

    callbacks.globals["bridge"] = SimpleNamespace(handle=handle)

    def drain():
        while background or main_thread:
            queue = background if background else main_thread
            selector, argument = queue.pop(0)
            selector.callable(argument)

    receiver.pollUsage_(None)  # a hidden-mode poll is already in flight at wake
    callbacks.call("systemDidWake_", receiver, None)
    drain()
    assert gets == [0]
    assert sweeps == [(6, "all", "offline"), (6, "all", "cached")]
    assert receiver._pending_usage_request is None and not receiver._usage_poll_inflight
    assert receiver._wake_poll_timer is None and receiver._wake_followup_timer is timers[1]
    elapsed = 40
    timers[1].fire()
    drain()
    assert gets == [0, 40]
    assert sweeps[-1] == (40, "all", "ok")
    assert ctx.load_state().get_seat("codex", email)["usage"]["ok"] is True
    assert receiver._wake_followup_timer is None and receiver._pending_usage_request is None
    assert receiver._last_all_usage_poll == 1040


@native
def test_launch_registers_for_workspace_wake_notifications(monkeypatch):
    from acctsw import updater

    observers = []
    center = SimpleNamespace(addObserver_selector_name_object_=lambda *args: observers.append(args))
    monkeypatch.setattr(menubar, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda:
        SimpleNamespace(notificationCenter=lambda: center)))
    monkeypatch.setattr(menubar, "install_edit_menu", lambda _: None)
    monkeypatch.setattr(menubar, "NSApplication", SimpleNamespace(sharedApplication=lambda: None))
    button = SimpleNamespace(setTarget_=lambda _: None, setAction_=lambda _: None)
    item = SimpleNamespace(button=lambda: button)
    monkeypatch.setattr(menubar, "NSStatusBar", SimpleNamespace(systemStatusBar=lambda:
        SimpleNamespace(statusItemWithLength_=lambda _: item)))
    monkeypatch.setattr(menubar.appalive, "mark_alive", lambda _: None)
    monkeypatch.setattr(updater, "acknowledge", lambda _: None)
    monkeypatch.setattr(updater, "reconcile", lambda _: None)
    receiver = SimpleNamespace(ctx=SimpleNamespace(data_dir=object()),
        _setBarDoor=lambda _: None, togglePopover_=lambda _: None,
        _buildPopover=lambda: None, _startStateTimer=lambda: None,
        _startUsageTimer=lambda: None, _startUpdateChecks=lambda: None,
        systemDidWake_=lambda _: None, recoverBg_=lambda _: None, bootstrapBg_=lambda _: None,
        indexHistory_=lambda _: None, performSelectorInBackground_withObject_=lambda *_: None)
    timers = []
    monkeypatch.setattr(menubar, "NSTimer", SimpleNamespace(
        scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=lambda *args:
        timers.append(args) or "history-timer"))

    call("applicationDidFinishLaunching_", receiver, None)

    # Token history indexing starts once, 45 s after launch, off the launch path.
    assert len(timers) == 1 and receiver._history_timer == "history-timer"
    interval, target, history, info, repeats = timers[0]
    assert (interval, target, info, repeats) == (45.0, receiver, None, False)
    assert history.callable is receiver.indexHistory_ and history.signature == b"v@:@"

    assert len(observers) == 1
    target, selector, name, obj = observers[0]
    assert target is receiver and selector.callable is receiver.systemDidWake_
    assert selector.signature == b"v@:@"
    assert name == menubar.NSWorkspaceDidWakeNotification and obj is None
