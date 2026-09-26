"""The paid-use switch, across the app, consent and scripted PTY boundaries."""
import json
import os
from pathlib import Path

import pytest

from acctsw import bridge, handoff, launcher as L, session
from acctsw import paths as P
from tests.test_launcher_keyseats import (
    Child, THREAD, isolated_environment, price_get, setup, usage_get,
)
from tests.test_rollout import write_rollout


def disable(ctx, *, raw=False):
    with ctx.locked():
        state = ctx.load_state()
        if raw:
            state.settings()["key_fallback"] = False
        else:
            state.set_setting("key_fallback", False)
        state.save()


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_running_key_stops_on_heartbeat_and_can_resume_its_saved_turns(ctx, tool):
    seat, reset = setup(ctx, resting=True, confirm=False, tool=tool)
    calls, messages, transcripts = [], [], []
    if tool == "codex":
        original = write_rollout(ctx._codex_real / "sessions", thread=THREAD, cwd=os.getcwd())
    else:
        original = P.CLAUDE_CONFIG_DIR / "projects" / "test-project" / f"{THREAD}.jsonl"
        original.parent.mkdir(parents=True)
        original.write_text(json.dumps({"sessionId": THREAD, "cwd": os.getcwd()}) + "\n")
    original_bytes = original.read_bytes()

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        assert env is not None
        home = Path(env["CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"])
        if len(calls) == 2:
            assert argv[1:3] == ["resume" if tool == "codex" else "--resume", THREAD]
            assert '"paid turn flushed":true' in transcripts[0].read_text()
            assert not on_tick()
            return 0
        if tool == "codex":
            path = write_rollout(home / "sessions", thread=THREAD, cwd=os.getcwd())
        else:
            path = home / "projects" / "test-project" / f"{THREAD}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"sessionId": THREAD, "cwd": os.getcwd()}) + "\n")
        transcripts.append(path)
        assert bridge.snapshot_state(ctx)["running_key_seats"] == [seat["id"]]
        assert not on_tick()
        assert not output(b"ordinary paid output\n")
        disable(ctx)
        # A silent child gets the same stop request as a chatty child, without a limit banner.
        assert on_tick()
        with path.open("a") as saved:
            saved.write('{"paid turn flushed":true}\n')
        return 143  # the existing supervisor owns SIGTERM and gives the child time to flush

    original_active = ctx.load_state().active(tool)
    assert L.run(ctx, tool, [], spawn=spawn, get=usage_get(reset, tool=tool),
                 price_get=price_get, notify=messages.append) == 143
    assert len(calls) == 1
    assert transcripts[0].exists()
    assert ctx.load_state().active(tool) == original_active
    assert session.active_session(ctx.data_dir, tool) is None
    assert bridge.snapshot_state(ctx)["running_key_seats"] == []
    assert any("stopping the session and new requests" in m and
               "turn already sent may still bill" in m for m in messages)
    state = ctx.load_state()
    state.set_setting("key_fallback", True)
    state.save()
    args = ["resume", "--last"] if tool == "codex" else ["--continue"]
    assert L.run(ctx, tool, args, spawn=spawn, get=usage_get(reset, tool=tool),
                 price_get=price_get) == 0
    assert len(calls) == 2
    assert original.read_bytes() == original_bytes


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_subscription_child_ignores_paid_kill_switch(ctx, tool):
    _, reset = setup(ctx, tool=tool)
    calls = []

    def spawn(argv, output, on_tick=None):
        calls.append(argv)
        assert not on_tick()
        disable(ctx)
        assert not on_tick()
        assert not on_tick()
        assert not output(b"subscription work continues\n")
        return 0

    assert L.run(ctx, tool, [], spawn=spawn, get=usage_get(reset, 20, tool=tool),
                 price_get=lambda *_: pytest.fail("no paid handoff")) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("parked", ["cold", "post_exit"])
@pytest.mark.parametrize("raw", [False, True])
def test_pending_confirmation_withdrawn_releases_parked_launcher(ctx, monkeypatch, parked, raw):
    _, reset = setup(ctx, resting=parked == "cold")
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "1")
    child = Child(ctx)
    sleeps, messages, outcomes = [], [], []
    resolve = handoff.resolve

    def record_outcome(*args, **kwargs):
        record = resolve(*args, **kwargs)
        if record and record["status"] != "pending":
            outcomes.append(record)
        return record

    monkeypatch.setattr(handoff, "resolve", record_outcome)

    def sleep(seconds):
        sleeps.append(seconds)
        assert seconds == L.TICK_INTERVAL_S
        assert len(sleeps) == 1  # neither the consent deadline nor the hours-long rest wait
        assert handoff.pending(ctx)
        disable(ctx, raw=raw)

    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 sleep=sleep, notify=messages.append) == L.EXIT_GAVE_UP
    assert len(child.calls) == (0 if parked == "cold" else 1)
    assert sleeps == [L.TICK_INTERVAL_S]
    assert [r["status"] for r in outcomes] == ["withdrawn"]
    assert outcomes[0]["decision_source"] == "setting"
    assert any("setting withdrew" in m for m in messages)
    assert not any("invitation expired —" in m or "invitation declined" in m for m in messages)
    assert not ctx.load_state().data.get("handoffs")


def test_withdrawing_live_confirmation_does_not_stop_subscription_child(ctx):
    _, reset = setup(ctx)
    child = Child(ctx, [lambda: disable(ctx)])
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 sleep=lambda _: pytest.fail("withdrawal must end consent waiting")) == 1
    assert child.stops == 0 and len(child.calls) == 1
    assert not ctx.load_state().data.get("handoffs")


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_setting_flipped_after_approval_and_setup_prevents_spawn(ctx, monkeypatch, tool):
    _, reset = setup(ctx, resting=True, confirm=False, tool=tool)
    marked = L.mark_session
    marks = []

    def mark(*args):
        marked(*args)
        marks.append(args)
        # Past the old last approval check, argv construction and the launch notification.
        disable(ctx)

    monkeypatch.setattr(L, "mark_session", mark)
    messages = []
    assert L.run(ctx, tool, [], spawn=lambda *a, **kw: pytest.fail("paid spawn raced switch"),
                 get=usage_get(reset, tool=tool), price_get=price_get,
                 notify=messages.append) == L.EXIT_GAVE_UP
    assert marks
    assert any("no paid session started" in m for m in messages)
    assert session.active_session(ctx.data_dir, tool) is None


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_off_blocks_new_paid_handoff_end_to_end(ctx, monkeypatch, tool):
    _, reset = setup(ctx, resting=True, enabled=False, confirm=False, tool=tool)
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    assert L.run(ctx, tool, [], spawn=lambda *a, **kw: pytest.fail("must not spawn"),
                 get=usage_get(reset, tool=tool),
                 price_get=lambda *_: pytest.fail("must not offer a paid seat")) == L.EXIT_GAVE_UP
    assert not ctx.load_state().data.get("handoffs")


@pytest.mark.parametrize("approved", [False, True])
def test_app_switch_withdraws_consent_atomically_and_reenable_cannot_revive_it(ctx, approved):
    seat, _ = setup(ctx, resting=True)
    id = handoff.request(ctx, "codex", "a@x.com", seat["id"])
    if approved:
        assert handoff.answer(ctx, id, True)
    result = bridge.handle(ctx, {"action": "toggle", "key": "key_fallback", "value": False})
    assert result["ok"] and result["state"]["pending_key_switches"] == []
    assert ctx.load_state().data["handoffs"][id]["status"] == "withdrawn"
    assert not handoff.answer(ctx, id, True)
    bridge.handle(ctx, {"action": "toggle", "key": "key_fallback", "value": True})
    record = handoff.resolve(ctx, id)
    assert record["status"] == "withdrawn" and record["decision_source"] == "setting"
    assert handoff.resolve(ctx, id) is None
