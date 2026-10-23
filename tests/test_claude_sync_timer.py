"""Run actual native callback bodies without AppKit, with fake dispatch and bridge dependencies."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def callback(name, namespace):
    source = Path(__file__).resolve().parents[1] / 'app' / 'menubar.py'
    tree = ast.parse(source.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<native callback>', 'exec'), namespace)
    return namespace[name]


def test_three_second_timer_checks_credentials_even_when_state_signature_is_unchanged():
    source = Path(__file__).resolve().parents[1] / 'app' / 'menubar.py'
    tree = ast.parse(source.read_text())
    interval = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'STATE_POLL_SECONDS' for t in n.targets))
    assert interval == 3.0
    dispatched, applied = [], []
    namespace = {'objc': SimpleNamespace(selector=lambda value, **kw: value),
                 'bridge': SimpleNamespace(handle=lambda *_: pytest.fail('unchanged UI state'))}
    poll = callback('pollState_', namespace)
    receiver = SimpleNamespace(_claude_sync_inflight=False, _last_state_sig=(1,()),
        _stateSignature=lambda: (1,()), syncClaudeBg_=object(), applyResult_=applied.append,
        performSelectorInBackground_withObject_=lambda *args: dispatched.append(args))
    poll(receiver, None)
    poll(receiver, None)
    assert dispatched == [(receiver.syncClaudeBg_, None)]
    assert receiver._claude_sync_inflight is True
    assert applied == []
    receiver._claude_sync_inflight = False
    poll(receiver, None)
    assert len(dispatched) == 2


@pytest.mark.parametrize('fail', [True,False])
def test_background_sync_resets_inflight_and_calls_bridge(fail):
    calls = []
    def handle(ctx, message):
        calls.append((ctx,message))
        if fail:
            raise RuntimeError('fake failure')
    sync = callback('syncClaudeBg_', {'bridge': SimpleNamespace(handle=handle)})
    receiver = SimpleNamespace(ctx=object(), _claude_sync_inflight=True)
    if fail:
        with pytest.raises(RuntimeError):
            sync(receiver, None)
    else:
        sync(receiver, None)
    assert calls == [(receiver.ctx, {'action':'claude_sync'})]
    assert receiver._claude_sync_inflight is False


def test_initialized_delegate_dispatches_its_first_credential_scan():
    dispatched = []
    receiver = SimpleNamespace()
    namespace = {'objc': SimpleNamespace(
        super=lambda _cls,obj:SimpleNamespace(init=lambda:obj),
        selector=lambda value,**_kw:value),
        'AGLDelegate': object, 'KeySwitchNotices': lambda:object(),
        'bridge':SimpleNamespace(handle=lambda *_:pytest.fail('unchanged UI state'))}
    init = callback('initWithContext_',namespace)
    assert init(receiver,object()) is receiver
    receiver._last_state_sig = (1,())
    receiver._stateSignature = lambda:(1,())
    receiver.syncClaudeBg_ = object()
    receiver.performSelectorInBackground_withObject_ = lambda *args:dispatched.append(args)
    callback('pollState_',namespace)(receiver,None)
    assert dispatched == [(receiver.syncClaudeBg_,None)]
