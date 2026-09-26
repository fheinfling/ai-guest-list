"""Consent notifications consume the real bridge contract without AppKit or real credentials."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from acctsw import bridge, handoff, pricing
from acctsw.util import now
from app import menubar


def pending(ctx, monkeypatch, *, notify=True, at=None):
    monkeypatch.setattr(pricing, "_default_get", lambda *_: (200, '{"data": []}'))
    state = ctx.load_state()
    state.set_setting("notify", notify)
    state.save()
    result = bridge.handle(ctx, {"action": "key_add", "provider": "openrouter",
                                "secret": "fake-secret-for-tests", "label": "late-night",
                                "model": "vendor/model"})
    assert result["ok"]
    return handoff.request(ctx, "codex", "work@example.test", result["added"], at=at)


@pytest.mark.parametrize("enabled", [True, False])
def test_pending_confirmation_notifies_once_honouring_setting(ctx, monkeypatch, enabled):
    id = pending(ctx, monkeypatch, notify=enabled)
    notices = menubar.KeySwitchNotices()
    delivered = []
    receiver = SimpleNamespace(_notify=lambda *pair: delivered.append(pair))
    result = bridge.handle(ctx, {"action": "status"})
    notices.deliver(result["state"], receiver._notify)
    notices.deliver(result["state"], receiver._notify)
    assert len(delivered) == int(enabled)
    if enabled:
        title, text = delivered[0]
        assert title == "a paid key needs your okay"
        assert "work@example.test" in text and "late-night" in text and "vendor/model" in text
        assert "approve paid use or decline" in text
    answer = bridge.handle(ctx, {"action": "answer_key_switch", "id": id, "approved": False})
    notices.deliver(answer["state"], receiver._notify)
    notices.deliver(result["state"], receiver._notify)  # stale pending snapshot
    assert len(delivered) == int(enabled)


def test_notification_respects_current_setting_and_request_setting(ctx, monkeypatch):
    pending(ctx, monkeypatch)
    bridge.handle(ctx, {"action": "toggle", "key": "notify", "value": False})
    menubar.KeySwitchNotices().deliver(bridge.snapshot_state(ctx), lambda *_: pytest.fail("disabled now"))
    pending(ctx, monkeypatch, notify=False)
    snapshot = bridge.snapshot_state(ctx)
    snapshot["pending_key_switches"] = snapshot["pending_key_switches"][-1:]
    snapshot["settings"]["notify"] = True
    menubar.KeySwitchNotices().deliver(snapshot, lambda *_: pytest.fail("disabled when requested"))


def test_expired_pending_confirmation_never_notifies(ctx, monkeypatch):
    pending(ctx, monkeypatch, at=now() - timedelta(minutes=3))
    snapshot = bridge.snapshot_state(ctx)
    assert snapshot["pending_key_switches"] == []
    menubar.KeySwitchNotices().deliver(snapshot, lambda *_: pytest.fail("expired"))


@pytest.mark.skipif(menubar.objc is None, reason="native callback requires PyObjC")
def test_native_push_reaches_existing_notify_path_even_without_webview(ctx, monkeypatch):
    pending(ctx, monkeypatch)
    delivered = []
    receiver = SimpleNamespace(_key_notices=menubar.KeySwitchNotices(),
                               _notify=lambda *pair: delivered.append(pair), webview=None)
    menubar.AGLDelegate._pushResult(receiver, bridge.handle(ctx, {"action": "status"}))
    assert len(delivered) == 1


@pytest.mark.skipif(menubar.objc is None, reason="native callback requires PyObjC")
def test_closing_popover_keeps_consent_poll_alive():
    events = []
    receiver = SimpleNamespace(_stopStateTimer=lambda: pytest.fail("hidden consent must still poll"),
                               _setWebVisible=lambda visible: events.append(visible),
                               _setUsagePollInterval=lambda interval: events.append(interval))
    menubar.AGLDelegate.popoverDidClose_.callable(receiver, None)
    assert events == [False, menubar.USAGE_POLL_HIDDEN_SECONDS]


@pytest.mark.skipif(menubar.objc is None, reason="native callback requires PyObjC")
def test_key_worker_correlates_without_echoing_secret(ctx, monkeypatch):
    results = []
    receiver = SimpleNamespace(ctx=ctx, applyResult_=lambda _: None,
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda selector, result, wait: results.append(result))
    msg = {"action": "models_list", "provider": "openai", "secret": "fake-private-key",
           "key_request_id": "key-2"}
    monkeypatch.setattr(bridge, "handle", lambda *_: {"ok": True, "models": []})
    menubar.AGLDelegate.keyBg_.callable(receiver, msg)
    assert results[-1]["key_action"] == "models_list"
    assert results[-1]["key_request_id"] == "key-2"
    assert "secret" not in results[-1]

    def fail(*_):
        raise RuntimeError(msg["secret"])

    monkeypatch.setattr(bridge, "handle", fail)
    menubar.AGLDelegate.keyBg_.callable(receiver, msg)
    assert results[-1]["ok"] is False
    assert msg["secret"] not in str(results[-1])
