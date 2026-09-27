"""Explicit paid terminals use the fallback's consent and heartbeat, never OAuth machinery."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from acctsw import appalive, bridge, cli, handoff, keyhome, keyseats, launcher as L, session
from acctsw.errors import AcctswError
from acctsw.util import write_json
from app import terminal
from tests.test_launcher_keyseats import (
    MODEL, SECRET, THREAD, forbid_credentials, isolated_environment, price_get, setup,
)
from tests.test_paid_kill_switch import disable
from tests.test_rollout import write_rollout


def no_network(*args, **kwargs):
    pytest.fail("a pin must not probe subscription usage")


def run_pin(ctx, seat, **kwargs):
    return L.run(ctx, "codex", [], key=seat["id"], price_get=price_get,
                 get=no_network, **kwargs)


@pytest.mark.parametrize("by_label", [False, True])
@pytest.mark.parametrize("app_open", [False, True])
def test_cx_key_id_or_label_uses_recorded_gate_and_child_only_env(ctx, monkeypatch, by_label, app_open):
    seat, _ = setup(ctx, confirm=False)
    original = ctx.load_state().active("codex")
    env_before = dict(os.environ)
    outcomes, calls = [], []
    resolve = handoff.resolve

    def recorded(*args, **kwargs):
        record = resolve(*args, **kwargs)
        if record:
            outcomes.append(record)
        return record

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        assert env["OPENROUTER_API_KEY"] == SECRET
        assert SECRET not in repr(argv) and "--key" not in argv
        assert argv[-4:] == ["--model", MODEL, "-c", 'model_provider="openrouter"']
        assert not output(b"Your workspace is out of credits.\n")
        assert not on_tick()
        row = bridge.snapshot_state(ctx)["pinned_sessions"][0]
        assert row["paid"] and row["key_seat"]["label"] == seat["label"]
        assert row["key_seat"]["provider"] == seat["provider"]
        return 7

    real_run = L.run
    monkeypatch.setattr(handoff, "resolve", recorded)
    monkeypatch.setattr(appalive, "app_running", lambda *_: app_open)
    monkeypatch.setattr(L, "run", lambda *a, **kw: real_run(
        *a, **kw, spawn=spawn, price_get=price_get, get=no_network))
    monkeypatch.setattr(L, "exec_stock", no_network)
    monkeypatch.setattr("builtins.input", no_network)  # auto-approval must not prompt
    spies = forbid_credentials(ctx, monkeypatch)
    ns = cli.build_parser().parse_args(["run", "codex", "--key",
                                        seat["label"] if by_label else seat["id"]])
    assert cli._cmd_run(ctx, ns) == 7
    assert len(calls) == 1
    assert outcomes[-1]["status"] == "approved"
    assert outcomes[-1]["decision_source"] == "setting"
    assert outcomes[-1]["pinned"] is True
    assert outcomes[-1]["price"]["rates"]["input"]["value"] == "2.000000"
    assert ctx.load_state().active("codex") == original
    assert dict(os.environ) == env_before
    assert bridge.snapshot_state(ctx)["pinned_sessions"] == []
    for spy in spies:
        spy.assert_not_called()
    for path in ctx.data_dir.parent.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_ambiguous_label_names_every_candidate(ctx):
    seat, _ = setup(ctx)
    state = ctx.load_state()
    other = {**seat, "id": "second-key"}
    state.data["keys"][other["id"]] = other
    state.save()
    with pytest.raises(AcctswError) as error:
        L.run(ctx, "codex", [], key=seat["label"], spawn=no_network, price_get=no_network)
    assert "ambiguous" in str(error.value)
    assert seat["id"] in str(error.value) and "second-key" in str(error.value)
    assert keyseats.resolve(ctx, seat["id"], harness="codex")["id"] == seat["id"]


def test_paid_off_refuses_before_consent_or_spawn(ctx):
    seat, _ = setup(ctx, enabled=False)
    with pytest.raises(AcctswError, match="paid use is off — turn on 'allow paid key use' before using --key"):
        run_pin(ctx, seat, spawn=no_network)
    assert not ctx.load_state().data.get("handoffs")


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("terminal_consent", [False, True])
def test_explicit_consent_is_required(ctx, monkeypatch, approved, terminal_consent):
    seat, _ = setup(ctx)
    spawned = Mock(return_value=0)
    records = []
    spies = forbid_credentials(ctx, monkeypatch)

    def decide(record):
        records.append(record)
        assert record["status"] == "pending" and record["pinned"]
        assert record["price"]["rates"]["output"]["value"] == "8.000000"
        assert not spawned.called
        assert not list(ctx._homes_root.glob("*/config.toml"))
        return approved

    def sleep(_):
        record, = handoff.pending(ctx)
        handoff.answer(ctx, record["id"], decide(record))

    assert run_pin(ctx, seat, spawn=spawned, sleep=sleep,
                   confirm=decide if terminal_consent else None) == (0 if approved else L.EXIT_GAVE_UP)
    assert spawned.call_count == int(approved)
    assert len(records) == 1 and not ctx.load_state().data.get("handoffs")
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize("race", ["approval", "prepared", "marked"])
def test_paid_off_during_gate_or_immediately_before_spawn_wins(ctx, monkeypatch, race):
    seat, _ = setup(ctx, confirm=race == "approval")

    def sleep(_):
        record, = handoff.pending(ctx)
        assert handoff.answer(ctx, record["id"], True)
        disable(ctx)

    if race in ("prepared", "marked"):
        owner, name = (keyhome, "build_env") if race == "prepared" else (L, "mark_session")
        original = getattr(owner, name)

        def flip(*args, **kwargs):
            result = original(*args, **kwargs)
            disable(ctx)
            return result

        monkeypatch.setattr(owner, name, flip)
    assert run_pin(ctx, seat, spawn=no_network, sleep=sleep) == L.EXIT_GAVE_UP
    assert not session.active_sessions(ctx.data_dir, "codex")


@pytest.mark.parametrize("stop", ["end", "kill_switch"])
def test_pin_stays_put_then_stops_and_subscription_resume_sees_flushed_work(ctx, stop):
    seat, _ = setup(ctx, confirm=False, resting=True)
    calls, private = [], []
    active = ctx.load_state().active("codex")

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        path = write_rollout(Path(env["CODEX_HOME"]) / "sessions", thread=THREAD, cwd=os.getcwd())
        private.append(path)
        # A subscription waking or being manually selected must never dislodge the pin.
        state = ctx.load_state()
        state.set_limited_until("codex", "b@x.com", None)
        state.set_active("codex", "b@x.com")
        state.data["tools"]["codex"]["manual_switch"] = {"email": "b@x.com"}
        state.save()
        assert not on_tick()
        assert not output(b"You've hit your usage limit\n")
        row, = bridge.snapshot_state(ctx)["pinned_sessions"]
        if stop == "end":
            result = bridge.handle(ctx, {"action": "end_pinned_session", "tool": "codex", "pin": row["pin"]})
            assert result["ok"]
            assert result["state"]["pinned_sessions"][0]["end_requested"]
        else:
            disable(ctx)
        assert on_tick()  # scripted SIGTERM: save only AFTER the stop request
        with path.open("a") as f:
            f.write('{"paid turn flushed":true}\n')
        return 143

    assert run_pin(ctx, seat, spawn=spawn) == 143
    assert len(calls) == 1
    assert ctx.load_state().active("codex") == "b@x.com"  # only our simulated user changed it
    assert not bridge.snapshot_state(ctx)["pinned_sessions"]
    exported, = (ctx._codex_real / "sessions").rglob("rollout-*.jsonl")
    assert '"paid turn flushed":true' in exported.read_text()
    assert '"paid turn flushed":true' in private[0].read_text()
    assert json.loads(exported.read_text().splitlines()[0])["payload"]["model_provider"] == "openai"

    def subscription(argv, output, on_tick=None):
        assert argv[1:] == ["resume", THREAD]
        home = Path(os.environ["CODEX_HOME"])
        assert '"paid turn flushed":true' in next((home / "sessions").rglob("rollout-*.jsonl")).read_text()
        disable(ctx)
        assert not on_tick()  # paid kill switch never kills a subscription child
        return 0

    assert L.run(ctx, "codex", ["resume", THREAD], spawn=subscription, get=no_network) == 0


def test_two_pins_and_subscription_record_are_independent(ctx, monkeypatch):
    seat, _ = setup(ctx, confirm=False)
    monkeypatch.setattr(session, "_alive", lambda _: True)
    monkeypatch.setattr(session.os, "getpid", lambda: 101)
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    subscription_path = session._process_file(ctx.data_dir, "codex", 101)
    subscription_record = subscription_path.read_bytes()
    homes, pins = [], []

    def second(argv, output, on_tick=None, *, env=None):
        homes.append(env["CODEX_HOME"])
        rows = bridge.snapshot_state(ctx)["pinned_sessions"]
        assert len(rows) == 2
        pins.extend(r["pin"] for r in rows)
        first = next(r for r in rows if r["pid"] == 202)
        other = next(r for r in rows if r["pid"] == 303)
        assert session.request_end(ctx, "codex", first["pin"])
        assert not on_tick()  # requesting the first does not end this one
        assert session.request_end(ctx, "codex", other["pin"])
        assert on_tick()
        return 143

    def first(argv, output, on_tick=None, *, env=None):
        homes.append(env["CODEX_HOME"])
        with monkeypatch.context() as child_patch:
            child_patch.setattr(session.os, "getpid", lambda: 303)
            assert run_pin(ctx, seat, spawn=second) == 143
        assert len(bridge.snapshot_state(ctx)["pinned_sessions"]) == 1
        assert on_tick()
        return 143

    with monkeypatch.context() as first_patch:
        first_patch.setattr(session.os, "getpid", lambda: 202)
        assert run_pin(ctx, seat, spawn=first) == 143
    assert len(set(homes)) == 2 and len(set(pins)) == 2
    assert subscription_path.read_bytes() == subscription_record
    assert session.active_sessions(ctx.data_dir, "codex")[0]["pid"] == 101
    assert not session.request_end(ctx, "codex", pins[0])  # stale UI cannot stop a new run
    assert not session.request_end(ctx, "codex", "a@x.com")
    assert ctx.load_state().active("codex") == "a@x.com"


def test_only_key_seats_no_subscription_required(ctx):
    seat, _ = setup(ctx, confirm=False)
    state = ctx.load_state()
    state.data["tools"]["codex"]["accounts"] = {}
    state.set_active("codex", None)
    state.save()
    assert run_pin(ctx, seat, spawn=lambda *a, **kw: 0) == 0
    assert ctx.load_state().active("codex") is None


def test_terminal_command_uses_wrapper_and_launchservices_without_oauth(ctx, monkeypatch):
    seat, _ = setup(ctx)
    commands = []
    monkeypatch.setattr(terminal, "open_in_terminal", commands.append)
    monkeypatch.setattr(terminal, "sync_back", no_network)
    monkeypatch.setattr(ctx, "snapshot_get", no_network)
    result = bridge.key_action(ctx, {"action": "key_terminal", "id": seat["id"]})
    assert result == {"ok": True, "key_terminal": seat["id"]}
    terminal.open_key_terminal(ctx, result["key_terminal"])
    assert commands == [f"cx --key {seat['id']}"]
    assert SECRET not in commands[0]
    disable(ctx)
    assert bridge.key_action(ctx, {"action": "key_terminal", "id": seat["id"]}) == {
        "ok": False, "error": "paid use is off — turn on 'allow paid key use' to continue",
        "code": "paid_use_disabled",
    }


@pytest.mark.parametrize("enable_paid", [None, False, "true", 1, True])
def test_terminal_paid_opt_in_is_explicit_and_uses_the_master_switch(ctx, enable_paid):
    seat, _ = setup(ctx, enabled=False)
    result = bridge.key_action(ctx, {"action": "key_terminal", "id": seat["id"],
                                     "enable_paid": enable_paid})
    assert result["ok"] is (enable_paid is True)
    assert ctx.load_state().settings()["key_fallback"] is (enable_paid is True)
    assert not ctx.load_state().data.get("handoffs")
    if enable_paid is True:
        assert result["key_terminal"] == seat["id"]
        disable(ctx)
        assert bridge.key_action(ctx, {"action": "key_terminal", "id": seat["id"]})["code"] == "paid_use_disabled"
        with pytest.raises(AcctswError, match="allow paid key use"):
            run_pin(ctx, seat, spawn=no_network)


def test_terminal_opt_in_cannot_enable_paid_use_for_a_missing_seat(ctx):
    setup(ctx, enabled=False)
    result = bridge.key_action(ctx, {"action": "key_terminal", "id": "missing", "enable_paid": True})
    assert not result["ok"]
    assert ctx.load_state().settings()["key_fallback"] is False


def test_subscription_cannot_fallback_onto_another_terminals_pinned_key(ctx, monkeypatch):
    from tests.test_launcher_keyseats import usage_get
    seat, reset = setup(ctx, resting=True, confirm=False)
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    monkeypatch.setattr(session, "_alive", lambda _: True)
    real_pid = os.getpid()
    with monkeypatch.context() as other:
        other.setattr(session.os, "getpid", lambda: real_pid + 100)
        session.mark_session(ctx.data_dir, "codex", seat["id"], pin="other-pin", key_seat=seat)
    assert L.run(ctx, "codex", [], spawn=no_network, get=usage_get(reset),
                 price_get=no_network) == L.EXIT_GAVE_UP
    assert session.active_sessions(ctx.data_dir, "codex")[0]["pin"] == "other-pin"
    assert not ctx.load_state().data.get("handoffs")


@pytest.mark.parametrize("answer", ["y", "n"])
def test_cli_without_app_records_terminal_consent(ctx, monkeypatch, answer):
    seat, _ = setup(ctx)
    monkeypatch.setattr(appalive, "app_running", lambda *_: False)
    replies = []

    def input_reply(prompt):
        record, = handoff.pending(ctx)
        replies.append(record)
        return answer

    monkeypatch.setattr("builtins.input", input_reply)
    spawn = Mock(return_value=0)
    real_run = L.run
    monkeypatch.setattr(L, "run", lambda *a, **kw: real_run(
        *a, **kw, spawn=spawn, price_get=price_get, get=no_network))
    ns = cli.build_parser().parse_args(["run", "codex", "--key", seat["id"]])
    assert cli._cmd_run(ctx, ns) == (0 if answer == "y" else L.EXIT_GAVE_UP)
    assert len(replies) == 1 and replies[0]["pinned"]
    assert spawn.call_count == int(answer == "y")


def test_pinned_terminal_command_reaches_command_script_without_secret(ctx, monkeypatch):
    from tests.test_terminal import _capture_open
    seat, _ = setup(ctx)
    captured = _capture_open(monkeypatch)
    monkeypatch.setattr(terminal, "sync_back", no_network)
    terminal.open_key_terminal(ctx, seat["id"])
    assert captured["argv"][:3] == ["open", "-a", "Terminal"]
    assert captured["argv"][-1].endswith(".command")
    assert f"cx --key {seat['id']}" in captured["script"]
    assert SECRET not in captured["script"]


def test_claude_pin_ends_on_same_heartbeat_without_oauth(ctx, monkeypatch):
    from acctsw import paths
    seat, _ = setup(ctx, confirm=False, tool="claude")
    active = ctx.load_state().active("claude")
    spies = forbid_credentials(ctx, monkeypatch)

    def spawn(argv, output, on_tick=None, *, env=None):
        assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
        assert SECRET not in repr(argv)
        path = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "work" / f"{THREAD}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": THREAD, "cwd": os.getcwd()}) + "\n")
        disable(ctx)
        assert on_tick()
        with path.open("a") as saved:
            saved.write('{"flushed":true}\n')
        return 143

    assert L.run(ctx, "claude", [], key=seat["id"], spawn=spawn, price_get=price_get,
                 get=no_network) == 143
    assert ctx.load_state().active("claude") == active
    saved = paths.CLAUDE_CONFIG_DIR / "projects" / "work" / f"{THREAD}.jsonl"
    assert '"flushed":true' in saved.read_text()
    for spy in spies:
        spy.assert_not_called()


def test_pin_expiry_never_spawns(ctx):
    seat, _ = setup(ctx)
    assert run_pin(ctx, seat, spawn=no_network, sleep=lambda _: None) == L.EXIT_GAVE_UP
    assert not ctx.load_state().data.get("handoffs")


def test_pinned_notice_does_not_claim_to_leave_subscription(ctx):
    from app.menubar import KeySwitchNotices
    seat, _ = setup(ctx)
    handoff.request(ctx, "codex", None, seat["id"], pinned=True)
    notices = []
    KeySwitchNotices().deliver(bridge.snapshot_state(ctx), lambda *args: notices.append(args))
    assert "pin this terminal" in notices[0][1]
    assert "leave" not in notices[0][1]
