"""Run real native callbacks with fake dispatch and bridge dependencies."""
from types import SimpleNamespace

import pytest

from app import menubar

pytestmark = pytest.mark.skipif(menubar.objc is None, reason="native callbacks require PyObjC")


def call(name, receiver, argument):
    getattr(menubar.AGLDelegate, name).callable(receiver, argument)


def test_three_second_timer_checks_credentials_even_when_state_signature_is_unchanged(monkeypatch):
    assert menubar.STATE_POLL_SECONDS == 3.0
    dispatched, applied = [], []
    monkeypatch.setattr(menubar.bridge, 'handle', lambda *_: pytest.fail('unchanged UI state'))
    receiver = SimpleNamespace(_claude_sync_inflight=False, _last_state_sig=(1,()),
        _stateSignature=lambda: (1,()), syncClaudeBg_=lambda _: None, applyResult_=applied.append,
        performSelectorInBackground_withObject_=lambda *args: dispatched.append(args))
    call('pollState_', receiver, None)
    call('pollState_', receiver, None)
    assert len(dispatched) == 1
    assert dispatched[0][0].callable is receiver.syncClaudeBg_
    assert dispatched[0][1] is None
    assert receiver._claude_sync_inflight is True
    assert applied == []
    receiver._claude_sync_inflight = False
    call('pollState_', receiver, None)
    assert len(dispatched) == 2


@pytest.mark.parametrize('fail', [True,False])
def test_background_sync_resets_inflight_and_calls_bridge(fail, monkeypatch):
    calls = []
    def handle(ctx, message):
        calls.append((ctx,message))
        if fail:
            raise RuntimeError('fake failure')
    monkeypatch.setattr(menubar.bridge, 'handle', handle)
    receiver = SimpleNamespace(ctx=object(), _claude_sync_inflight=True)
    if fail:
        with pytest.raises(RuntimeError):
            call('syncClaudeBg_', receiver, None)
    else:
        call('syncClaudeBg_', receiver, None)
    assert calls == [(receiver.ctx, {'action':'claude_sync'})]
    assert receiver._claude_sync_inflight is False


def test_initialized_delegate_dispatches_its_first_credential_scan(monkeypatch):
    dispatched = []
    receiver = menubar.AGLDelegate.alloc()
    context = object()
    assert receiver.initWithContext_(context) is receiver
    assert receiver.ctx is context
    monkeypatch.setattr(menubar.bridge, 'handle', lambda *_: pytest.fail('unchanged UI state'))
    receiver._last_state_sig = (1,())
    receiver._stateSignature = lambda:(1,())
    receiver.syncClaudeBg_ = lambda _: None
    receiver.performSelectorInBackground_withObject_ = lambda *args:dispatched.append(args)
    call('pollState_', receiver, None)
    assert len(dispatched) == 1
    assert dispatched[0][0].callable is receiver.syncClaudeBg_
    assert dispatched[0][1] is None
