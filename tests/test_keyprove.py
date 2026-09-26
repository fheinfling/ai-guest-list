"""Paid endpoint checks run against a scripted child, never Codex or the network."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
from threading import Event
from time import monotonic
import tomllib
from urllib import request

import pytest

from acctsw import appserver, bridge, cli, keyhome, keyprove, keyseats, providers
from test_appserver import FakeChild

SECRET = "sk-probe-secret-must-never-be-echoed"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Probe test attempted a real child or network")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(request, "build_opener", forbidden)


@pytest.fixture
def seat(ctx):
    provider = providers.get_provider("openai_compatible", base_url="https://endpoint.test/v1")
    return keyseats.add(ctx, provider, SECRET, label="probe", model="chosen-model",
                        allow_unverified=True, get=lambda *_: (200, "{}"))


def scripted(*, info=None, status="completed", approval=False, rpc=False, phase="turn/start",
             quiet=False, died=False):
    def complete(child):
        error = {"codexErrorInfo": info, "message": SECRET, "additionalDetails": SECRET}
        child.emit({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
            "id": "turn", "status": status, "error": error if info is not None else None}}})

    def script(child, message):
        method = message.get("method")
        if quiet:
            return
        if method == phase and rpc:
            child.emit({"id": message["id"], "error": {"code": -32000, "message": SECRET,
                         "data": {"codexErrorInfo": info, "body": SECRET}}})
        elif method == "initialize":
            child.reply(message, {})
        elif method == "thread/start":
            child.reply(message, {"thread": {"id": "thread"}, "model": "chosen-model",
                                 "modelProvider": "openai_compatible", "cwd": message["params"]["cwd"],
                                 "approvalPolicy": "never", "sandbox": {"type": "readOnly"}})
        elif method == "turn/start":
            child.reply(message, {"turn": {"id": "turn"}})
            if died:
                child.finish(1)
            elif approval:
                for name in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                             "item/tool/call", "item/permissions/requestApproval"):
                    child.emit({"id": name, "method": name, "params": {"command": "cat /secret",
                                "path": "/user/repo/file", "body": SECRET}})
            else:
                complete(child)
        elif "method" not in message:
            assert message["error"]["code"] == -32601
            assert "result" not in message
            if message["id"] == "item/permissions/requestApproval":
                complete(child)
    return FakeChild(script, stubborn=True)


def inject(monkeypatch, child):
    def kill(pid):
        child.stops.append(pid)
        child.finish(-signal.SIGKILL)
    monkeypatch.setattr(appserver, "_spawn", child.spawn)
    monkeypatch.setattr(appserver, "_kill_group", kill)
    return kill


CASES = [(None, "completed", "proven", None),
         ("unauthorized", "failed", "refused", "invalid_key"),
         ("usageLimitExceeded", "failed", "refused", "insufficient_quota"),
         ("sessionBudgetExceeded", "failed", "refused", "insufficient_quota"),
         ("rateLimitExceeded", "failed", "refused", "rate_limited"),
         ({"httpConnectionFailed": {"httpStatusCode": 429}}, "failed", "refused", "unknown"),
         ("badRequest", "failed", "incompatible", "request_rejected"),
         ({"responseStreamConnectionFailed": {"httpStatusCode": 404}}, "failed", "incompatible", "request_rejected"),
         ({"httpConnectionFailed": {"httpStatusCode": 400}}, "failed", "incompatible", "request_rejected"),
         ({"httpConnectionFailed": {"httpStatusCode": 503}}, "failed", "inconclusive", "no_verdict"),
         ("other", "failed", "inconclusive", "no_verdict"),
         (SECRET, "failed", "inconclusive", "no_verdict"),
         (None, "interrupted", "inconclusive", "no_verdict")]


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("info,status,outcome,error", CASES)
def test_outcomes_only_success_changes_flag(ctx, seat, monkeypatch, capsys, caplog,
                                          verified, info, status, outcome, error):
    state = ctx.load_state()
    state.data["keys"][seat["id"]]["responses_verified"] = verified
    state.save()
    child = scripted(info=info, status=status)
    inject(monkeypatch, child)
    result = keyprove.prove(ctx, seat["id"])
    assert result == {"outcome": outcome, "error": error, "model": "chosen-model",
                      "checked_at": result["checked_at"]}
    assert result["checked_at"]
    saved = keyseats.get(ctx, seat["id"])
    assert saved["last_proof"] == result
    assert saved["responses_verified"] is (verified or outcome == "proven")
    assert child.stops == [child.pid]
    assert child.stdin.closed and child.stdout.closed
    output = capsys.readouterr()
    assert SECRET not in json.dumps(ctx.load_state().data) + output.out + output.err + caplog.text


def test_real_home_env_empty_cwd_no_handler_and_all_approvals_refused(ctx, seat, monkeypatch):
    child = scripted(approval=True)
    inject(monkeypatch, child)
    caller = Path.cwd()
    seen = []
    original = child.spawn
    def spawn(argv, **kwargs):
        cwd = kwargs["cwd"]
        assert cwd.is_dir() and list(cwd.iterdir()) == [] and cwd != caller
        assert ctx.data_dir not in cwd.parents
        env = kwargs["env"]
        assert env["ACCTSW_API_KEY"] == SECRET
        assert env["RUST_LOG"] == "off"
        config = tomllib.loads((Path(env["CODEX_HOME"]) / "config.toml").read_text())
        assert config["model"] == seat["model"]
        provider = config["model_providers"][config["model_provider"]]
        assert provider["base_url"] == seat["base_url"] and provider["wire_api"] == "responses"
        assert provider["request_max_retries"] == provider["stream_max_retries"] == 0
        assert SECRET not in str(argv) + json.dumps(config)
        assert kwargs["start_new_session"] and kwargs["stderr"] == subprocess.DEVNULL
        seen.append(cwd)
        return original(argv, **kwargs)
    monkeypatch.setattr(appserver, "_spawn", spawn)
    assert keyprove.prove(ctx, seat["id"])["outcome"] == "proven"
    assert not seen[0].exists() and Path.cwd() == caller
    replies = [m for m in child.sent if "method" not in m]
    assert len(replies) == 4 and all(m["error"]["code"] == -32601 for m in replies)
    params = next(m["params"] for m in child.sent if m.get("method") == "thread/start")
    assert params["cwd"] == str(seen[0]) and params["ephemeral"] is True
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
    assert params["config"]["features.shell_tool"] is False
    assert params["config"]["tools.view_image"] is False
    assert "model" not in params and "modelProvider" not in params  # Actual generated config wins.
    turns = [m for m in child.sent if m.get("method") == "turn/start"]
    assert len(turns) == 1 and turns[0]["params"]["input"] == [{"type": "text", "text": keyprove.PROMPT}]


def test_timeout_terminates_group_and_records_uncertainty(ctx, seat, monkeypatch):
    child = scripted(quiet=True)
    inject(monkeypatch, child)
    start = monotonic()
    result = keyprove.prove(ctx, seat["id"], timeout=0.03)
    assert monotonic() - start < 1
    assert result["outcome"] == "inconclusive" and result["error"] == "timeout"
    assert child.stops and child.returncode is not None
    assert child.stdin.closed and child.stdout.closed
    assert not keyseats.get(ctx, seat["id"])["responses_verified"]


def test_hard_watchdog_also_breaks_a_blocked_write(ctx, seat, monkeypatch):
    killed = Event()
    def script(child, message):
        # Waiting inside stdin.write means no Queue.get/per-response timeout can help.
        assert killed.wait(0.5), "Only the lifetime watchdog can release this writer"
    child = FakeChild(script, stubborn=True)
    kill = inject(monkeypatch, child)
    def release(pid):
        kill(pid)
        killed.set()
    monkeypatch.setattr(appserver, "_kill_group", release)
    result = keyprove.prove(ctx, seat["id"], timeout=0.03)
    assert result["outcome"] == "inconclusive" and result["error"] == "timeout"
    assert child.stops and child.stdin.closed and child.stdout.closed


def test_chatter_does_not_restart_the_overall_deadline(ctx, seat, monkeypatch):
    child = scripted()
    script = child.script
    def chatter(child, message):
        if message.get("method") == "turn/start":
            child.reply(message, {"turn": {"id": "turn"}})
            for _ in range(1000):
                child.emit({"method": "item/agentMessage/delta", "params": {"delta": SECRET}})
        else:
            script(child, message)
    child.script = chatter
    inject(monkeypatch, child)
    start = monotonic()
    result = keyprove.prove(ctx, seat["id"], timeout=0.04)
    assert monotonic() - start < 1 and child.stops
    assert result["outcome"] == "inconclusive" and result["error"] == "timeout"


@pytest.mark.parametrize("info,outcome", [("unauthorized", "refused"), ("badRequest", "incompatible")])
def test_interleaved_error_notification_is_a_verdict(ctx, seat, monkeypatch, info, outcome):
    child = scripted()
    script = child.script
    def error(child, message):
        if message.get("method") == "turn/start":
            child.emit({"method": "error", "params": {"threadId": "thread", "turnId": "turn",
                "willRetry": False, "error": {"codexErrorInfo": info, "message": SECRET}}})
            child.reply(message, {"turn": {"id": "turn"}})
        else:
            script(child, message)
    child.script = error
    inject(monkeypatch, child)
    result = keyprove.prove(ctx, seat["id"])
    assert result["outcome"] == outcome and child.stops


def test_unrelated_completion_cannot_certify_the_probe(ctx, seat, monkeypatch):
    child = scripted(died=True)
    script = child.script
    def unrelated(child, message):
        if message.get("method") == "turn/start":
            for thread, turn in (("other", "turn"), ("thread", "other")):
                child.emit({"method": "turn/completed", "params": {"threadId": thread,
                            "turn": {"id": turn, "status": "completed"}}})
        script(child, message)
    child.script = unrelated
    inject(monkeypatch, child)
    assert keyprove.prove(ctx, seat["id"])["outcome"] == "inconclusive"


@pytest.mark.parametrize("field,value", [("model", "fallback-model"), ("modelProvider", "openai"),
    ("cwd", "/user/repo"), ("approvalPolicy", "on-request"), ("sandbox", {"type": "dangerFullAccess"})])
def test_runtime_mismatch_stops_before_spending(ctx, seat, monkeypatch, field, value):
    child = scripted()
    reply = child.reply
    def changed(message, result):
        if message.get("method") == "thread/start":
            result[field] = value
        reply(message, result)
    child.reply = changed
    inject(monkeypatch, child)
    result = keyprove.prove(ctx, seat["id"])
    assert result["outcome"] == "inconclusive" and result["error"] == "runtime_mismatch"
    assert not any(m.get("method") == "turn/start" for m in child.sent)
    assert child.stops


@pytest.mark.parametrize("phase", ["initialize", "thread/start", "turn/start"])
def test_rpc_error_only_inference_phase_can_reject_responses(ctx, seat, monkeypatch, phase):
    child = scripted(info="badRequest", rpc=True, phase=phase)
    inject(monkeypatch, child)
    result = keyprove.prove(ctx, seat["id"])
    assert result["outcome"] == ("incompatible" if phase == "turn/start" else "inconclusive")
    assert child.stops
    assert SECRET not in json.dumps(result)


def test_eof_without_verdict(ctx, seat, monkeypatch):
    child = scripted(died=True)
    inject(monkeypatch, child)
    assert keyprove.prove(ctx, seat["id"])["outcome"] == "inconclusive"
    assert child.stops == [child.pid]  # Even a dead leader can have living descendants.


@pytest.mark.parametrize("info,status,outcome,error", CASES)
def test_bridge_cli_envelopes_and_exit_codes(ctx, seat, monkeypatch, capsys, caplog,
                                            info, status, outcome, error):
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
    for entry in ("bridge", "json", "human"):
        child = scripted(info=info, status=status)
        inject(monkeypatch, child)
        if entry == "bridge":
            result = bridge.handle(ctx, {"action": "key_prove", "id": seat["id"]})
            assert result["state"]["keys"][0]["last_proof"] == result["proof"]
            assert result["proof"]["outcome"] == outcome
            assert result["ok"] is (outcome == "proven")
            assert SECRET not in json.dumps(result)
        else:
            code = cli.main(["keys", "prove", seat["id"], *(["--json"] if entry == "json" else [])])
            assert code == (cli.EXIT_OK if outcome == "proven" else cli.EXIT_ERR)
            output = capsys.readouterr()
            assert SECRET not in output.out + output.err + caplog.text
            if entry == "json":
                result = json.loads(output.out)
                assert result["proof"]["outcome"] == outcome and not output.err
            else:
                assert outcome in output.out + output.err


def test_bridge_failures_do_not_raise_or_leak(ctx, seat, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(keyprove, "prove", fail)
    result = bridge.handle(ctx, {"action": "key_prove", "id": seat["id"]})
    assert not result["ok"] and "state" in result
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("failure", [OSError, ValueError, KeyboardInterrupt])
def test_failure_mid_protocol_still_cleans_up(ctx, seat, monkeypatch, failure, caplog):
    child = scripted()
    script = child.script
    def fail(child, message):
        if message.get("method") == "turn/start":
            raise failure(SECRET)
        script(child, message)
    child.script = fail
    inject(monkeypatch, child)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            keyprove.prove(ctx, seat["id"])
    else:
        result = bridge.handle(ctx, {"action": "key_prove", "id": seat["id"]})
        assert result["proof"]["outcome"] == "inconclusive"
        assert SECRET not in json.dumps(result)
    assert child.stops and child.stdin.closed and child.stdout.closed
    assert SECRET not in caplog.text


def test_no_implicit_probe_on_add_validation_or_poll(ctx, seat, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Implicit paid request")
    monkeypatch.setattr(keyprove, "prove", forbidden)
    monkeypatch.setattr(bridge.pricing_mod, "_default_get", lambda *_: (200, "{}"))
    for message in ({"action": "status"}, {"action": "ready"},
                    {"action": "key_validate", "id": seat["id"]},
                    {"action": "key_add", "provider": "openai", "secret": SECRET,
                     "label": "new seat", "model": "model"}):
        assert bridge.handle(ctx, message)["ok"]


def test_changed_seat_does_not_receive_stale_proof(ctx, seat, monkeypatch):
    child = scripted()
    script = child.script
    def change(child, message):
        if message.get("method") == "turn/start":
            state = ctx.load_state()
            state.data["keys"][seat["id"]]["model"] = "different"
            state.save()
        script(child, message)
    child.script = change
    inject(monkeypatch, child)
    result = bridge.handle(ctx, {"action": "key_prove", "id": seat["id"]})
    assert not result["ok"] and child.stops
    assert "last_proof" not in keyseats.get(ctx, seat["id"])


def test_group_cleanup_uses_session_id_even_when_leader_has_exited(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(args))
    monkeypatch.setattr(os, "getpgid", lambda *_: pytest.fail("Leader may already be reaped"))
    appserver._kill_group(1234)
    assert calls == [(1234, signal.SIGKILL)]
