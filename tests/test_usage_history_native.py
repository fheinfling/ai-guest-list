"""Execute the actual callback bodies with fake selectors, including without PyObjC."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def callback(name, **globals):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app" / "menubar.py").read_text())
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    namespace = {"objc": SimpleNamespace(selector=lambda fn, **kw: SimpleNamespace(callable=fn)), **globals}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "native-callback", "exec"), namespace)
    return namespace[name]


def test_usage_history_dispatch_never_calls_bridge_on_main_thread():
    events = []
    target = lambda _: None
    receiver = SimpleNamespace(usageHistoryBg_=target,
        performSelectorInBackground_withObject_=lambda *args: events.append(args))
    def forbidden(*args):
        pytest.fail("bridge called on main thread")
    bridge = SimpleNamespace(KEY_ACTIONS=set(), handle=forbidden)
    callback("userContentController_didReceiveScriptMessage_", bridge=bridge)(
        receiver, None, SimpleNamespace(body=lambda: {"action": "usage_history"}))
    assert len(events) == 1 and events[0][0].callable is target
    assert events[0][1] == {"action": "usage_history"}


def test_first_index_is_scheduled_45_seconds_after_launch():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app" / "menubar.py").read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "applicationDidFinishLaunching_")
    call = next(n for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_"
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == 45.0)
    assert call.args[-1].value is False


@pytest.mark.parametrize("failure", [False, True])
def test_usage_history_worker_uses_apply_result_on_main_thread(failure):
    events = []
    target = lambda _: None
    ctx = object()
    result = {"ok": True, "usage_history": {"v": 1}}
    def handle(context, message):
        assert context is ctx and message == {"action": "usage_history"}
        if failure:
            raise OSError("/private/fake-secret")
        return result
    receiver = SimpleNamespace(ctx=ctx, applyResult_=target,
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda *args: events.append(args))
    callback("usageHistoryBg_", bridge=SimpleNamespace(handle=handle))(receiver, {"action": "usage_history"})
    assert events[0][0].callable is target and events[0][2] is False
    assert events[0][1] == ({"ok": False, "usage_history_error": "Could not read usage history"} if failure else result)


def test_index_timer_is_background_coalesced_and_ten_minutes():
    events = []
    timer = SimpleNamespace(invalidate=lambda: events.append("invalidate"))
    target = lambda _: None
    receiver = SimpleNamespace(_history_timer=timer, _history_timer_repeating=False, _history_index_inflight=False,
        indexHistory_=lambda _: None, indexHistoryBg_=target,
        performSelectorInBackground_withObject_=lambda *args: events.append(args))
    timers = SimpleNamespace(scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=lambda *args:
        events.append(args) or object())
    run = callback("indexHistory_", NSTimer=timers)
    run(receiver, object())  # PyObjC may supply another wrapper for the same native timer.
    run(receiver, None)
    assert events[0] == "invalidate" and events[1][0] == 600.0 and events[1][-1] is True
    assert len(events) == 3 and events[2][0].callable is target
    callback("finishIndexHistory_")(receiver, None)
    assert receiver._history_index_inflight is False


@pytest.mark.parametrize("failure", [False, True])
def test_index_worker_budget_and_finally_release(failure):
    events = []
    target = lambda _: None
    def refresh(ctx, *, budget_s):
        assert budget_s == 20
        if failure:
            raise OSError("fake")
    receiver = SimpleNamespace(ctx=object(), finishIndexHistory_=target,
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda *args: events.append(args))
    callback("indexHistoryBg_", tokenlog=SimpleNamespace(refresh=refresh), _log_exception=lambda exc: None)(receiver, None)
    assert events[0][0].callable is target and events[0][1:] == (None, False)


def test_app_and_bridge_import_without_sqlite3():
    # The release bundle only carries stdlib modules the app imports. If sqlite3 were ever missing,
    # the menubar app must still launch; only the usage view may fail (with its own error reply).
    import subprocess
    import sys
    from pathlib import Path
    code = ("import sys; sys.modules['sqlite3'] = None; import app.menubar, acctsw.bridge, acctsw.session; "
            "from acctsw import bridge; from acctsw.context import Context; import tempfile, pathlib; "
            "ctx = Context.for_test(pathlib.Path(tempfile.mkdtemp())); ctx.ensure_dirs(); "
            "r = bridge.handle(ctx, {'action': 'usage_history'}); "
            "assert r == {'ok': False, 'usage_history_error': 'Could not read usage history'}, r; print('ok')")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]


def test_bridge_history_refreshes_and_returns_indexed_payload(ctx, monkeypatch):
    from acctsw import bridge, tokenlog
    from tests.test_tokenlog import AT, claude, cp, write
    from datetime import datetime, timedelta

    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat(AT) + timedelta(hours=1))
    write(cp(ctx), claude("bridge"))
    calls = []
    real_refresh = tokenlog.refresh

    def refresh_history(context, *, budget_s):
        calls.append((context, budget_s))
        real_refresh(context, budget_s=budget_s)

    monkeypatch.setattr(tokenlog, "refresh", refresh_history)
    result = bridge.handle(ctx, {"action": "usage_history"})
    assert calls == [(ctx, 3.0)]
    assert result == {"ok": True, "usage_history": tokenlog.history(ctx)}
    assert result["usage_history"]["calendar"]["week"][0]["by"]["claude:?"] == {
        "claude-opus-5-5": [10, 4, 3, 5]}

