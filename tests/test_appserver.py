"""A scripted child exercises bidirectional JSON-RPC without starting Codex or networking.

Unlike a canned StringIO transcript, scripts may withhold a response until the client answers a
server request. That fails a read-one-response loop instead of letting its deadlock go unnoticed.
"""
from __future__ import annotations

import io
import json
from queue import Queue
import socket
import subprocess
from urllib import request

import pytest

from acctsw import appserver as A
from acctsw.providers import get_provider


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Test attempted network or a real subprocess")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(request, "build_opener", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class Output:
    def __init__(self):
        self.lines = Queue()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get()
        if line is None:
            raise StopIteration
        return line

    def close(self):
        self.closed = True


class Input(io.StringIO):
    def __init__(self, child):
        super().__init__()
        self.child = child

    def write(self, value):
        message = json.loads(value)
        self.child.sent.append(message)
        self.child.script(self.child, message)
        return len(value)

    def close(self):
        super().close()
        if not self.child.stubborn:
            self.child.finish(0)


class FakeChild:
    pid = 987654321

    def __init__(self, script=lambda child, msg: None, *, stubborn=False):
        self.script = script
        self.stubborn = stubborn
        self.sent = []
        self.spawn_calls = []
        self.stops = []
        self.returncode = None
        self.stdout = Output()
        self.stdin = Input(self)

    def emit(self, message):
        self.stdout.lines.put(json.dumps(message) + "\n")

    def reply(self, message, result):
        self.emit({"id": message["id"], "result": result})

    def finish(self, code):
        self.returncode = code
        self.stdout.lines.put(None)

    def spawn(self, argv, **kwargs):
        self.spawn_calls.append((argv, kwargs))
        return self

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake child", timeout)
        return self.returncode

    def terminate(self, pid):
        self.stops.append(pid)
        assert self.stdin.closed  # Give the server EOF before resorting to group termination.
        self.finish(-15)
        return -15

    def client(self, ctx, provider="openai", **kwargs):
        return A.AppServer(ctx, get_provider(provider), spawn=self.spawn, terminate=self.terminate,
                           timeout=0.5, **kwargs)


@pytest.mark.parametrize("resume", [False, True])
def test_handshake_thread_turn_and_launch_contract(ctx, resume):
    def script(child, msg):
        if "id" in msg:
            child.reply(msg, {"thread": {"id": "existing-thread"}})
    child = FakeChild(script)
    ctx.codex_bin = "/fake/bin/codex"
    env = {"CODEX_HOME": "/fake/private-home"}
    with child.client(ctx, env=env, cwd=ctx.data_dir) as client:
        client.initialize()
        if resume:
            result = client.resume_thread("existing-thread", model="chosen-model", model_provider="langdock_eu")
        else:
            result = client.start_thread(model="chosen-model", model_provider="langdock_eu")
        client.start_turn(result["thread"]["id"], "Continue the task")
    assert [m["method"] for m in child.sent] == ["initialize", "initialized",
                                                "thread/resume" if resume else "thread/start", "turn/start"]
    assert child.sent[0]["params"] == {"clientInfo": {"name": "acctsw", "version": "1"}}
    assert "id" not in child.sent[1]
    assert all("jsonrpc" not in m for m in child.sent)
    params = {"model": "chosen-model", "modelProvider": "langdock_eu"}
    if resume:
        params["threadId"] = "existing-thread"
    assert child.sent[2]["params"] == params
    assert child.sent[3]["params"] == {"threadId": "existing-thread",
                                       "input": [{"type": "text", "text": "Continue the task"}]}
    argv, options = child.spawn_calls[0]
    assert argv == [ctx.codex_bin, "app-server"]
    assert options["start_new_session"] is True
    assert options["stdin"] == options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.DEVNULL
    assert options["env"] == env and options["cwd"] == ctx.data_dir
    assert child.stdin.closed and child.stdout.closed and not client._reader.is_alive()
    assert child.stops == []


COUNTS = {"inputTokens": 120, "cachedInputTokens": 30, "cacheWriteInputTokens": 10,
          "outputTokens": 40, "reasoningOutputTokens": 12, "totalTokens": 160}
USAGE = {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread",
         "turnId": "turn", "tokenUsage": {"total": COUNTS,
         "last": {**COUNTS, "inputTokens": 60, "totalTokens": 100}}, "future": True}}


def test_out_of_order_responses_interleaved_request_and_usage(ctx):
    waiting = []
    def script(child, msg):
        if "method" in msg:
            waiting.append(msg)
            if len(waiting) == 2:
                child.reply(waiting[1], {"second": True})
                child.emit(USAGE)
                # Deliberately collide with a client request id: method distinguishes direction.
                child.emit({"id": waiting[0]["id"], "method": "item/commandExecution/requestApproval",
                            "params": {"threadId": "thread", "command": "echo hello"}})
        else:
            assert msg == {"id": waiting[0]["id"], "result": {"decision": "decline"}}
            child.reply(waiting[0], {"first": True})
    handled = []
    def handle(method, params):
        handled.append((method, params))
        return {"decision": "decline"}
    child = FakeChild(script)
    with child.client(ctx, handle_request=handle) as client:
        first = client.send_request("first", {})
        second = client.send_request("second", {})
        assert client.wait_response(first) == {"first": True}
        assert client.wait_response(second) == {"second": True}
        event = client.next_event()
        assert event.method == USAGE["method"] and event.params == USAGE["params"]
        assert isinstance(event.signal, A.UsageUpdate)
        assert event.signal.total == A.TokenCounts(120, 30, 10, 40, 12, 160)
        assert event.signal.last == A.TokenCounts(60, 30, 10, 40, 12, 100)
        assert len(handled) == 1


@pytest.mark.parametrize("info,expected", [
    ("unauthorized", "invalid_key"), ("usageLimitExceeded", "insufficient_quota"),
    ("rateLimitExceeded", "rate_limited"), ("sessionBudgetExceeded", "insufficient_quota"),
    *[({variant: {"httpStatusCode": status}}, "unknown")
      for variant in ("httpConnectionFailed", "responseStreamConnectionFailed",
                      "responseStreamDisconnected", "responseTooManyFailedAttempts")
      for status in (401, 402, 429, 500)],
    ({"httpConnectionFailed": {"httpStatusCode": "429"}}, "unknown"),
    ({"futureVariant": {"httpStatusCode": 429}}, "unknown"),
    ("unknownNewVariant", "unknown"), (None, "unknown"),
    ("Your key is unauthorized and you hit a rate limit", "unknown"),
])
def test_typed_error_notifications(ctx, info, expected):
    child = FakeChild()
    with child.client(ctx) as client:
        child.emit({"method": "error", "params": {"error": {"codexErrorInfo": info,
                    "message": "contradictory prose: revoked key / quota / throttled"},
                    "willRetry": False, "threadId": "thread", "turnId": "turn"}})
        event = client.next_event()
        assert event.signal == A.ErrorSignal(expected, info)
        assert event.params["willRetry"] is False


@pytest.mark.parametrize("provider", ["openai", "anthropic", "openrouter", "langdock", "groq"])
def test_bare_http_status_does_not_invent_provider_error_codes(ctx, provider):
    child = FakeChild()
    with child.client(ctx, provider) as client:
        child.emit({"method": "error", "params": {"error": {
                    "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}}})
        assert client.next_event().signal.category == "unknown"


def test_failed_turn_and_unknown_notification_are_retained(ctx):
    child = FakeChild()
    with child.client(ctx) as client:
        child.emit({"method": "turn/completed", "params": {"turn": {"status": "failed",
                    "error": {"codexErrorInfo": "unauthorized"}}}})
        child.emit({"method": "future/event", "params": {"newField": 42}})
        assert client.next_event().signal.category == "invalid_key"
        assert client.next_event() == A.Notification("future/event", {"newField": 42})


def test_missing_or_invalid_usage_is_unknown_not_zero(ctx):
    child = FakeChild()
    with child.client(ctx) as client:
        child.emit({"method": USAGE["method"], "params": {"tokenUsage": {
                    "total": {"inputTokens": -1, "cachedInputTokens": True,
                              "outputTokens": "4", "totalTokens": 0}}}})
        usage = client.next_event().signal
        assert usage.total == A.TokenCounts(None, None, None, None, None, 0)
        assert usage.last == A.TokenCounts(None, None, None, None, None, None)


@pytest.mark.parametrize("mode,code", [("missing", -32601), ("failure", -32603), ("rpc", -32000)])
def test_server_request_always_gets_a_reply(ctx, mode, code):
    def script(child, msg):
        if "method" in msg:
            child.emit({"id": "server-request", "method": "new/approval", "params": {}})
        else:
            assert msg["id"] == "server-request" and msg["error"]["code"] == code
            child.emit({"id": 0, "result": "unblocked"})
    def handler(method, params):
        if mode == "rpc":
            raise A.RpcError({"code": code, "message": "declined"})
        raise RuntimeError("must not be echoed")
    child = FakeChild(script)
    with child.client(ctx, handle_request=None if mode == "missing" else handler) as client:
        assert client.request("pending", {}) == "unblocked"
        assert "must not be echoed" not in repr(child.sent)


def test_handler_can_make_a_reentrant_request(ctx):
    def script(child, msg):
        if msg.get("method") == "outer":
            child.emit({"method": "approval", "id": "approval", "params": {}})
        elif msg.get("method") == "inner":
            child.reply(msg, "answer")
        else:
            assert msg == {"id": "approval", "result": "answer"}
            child.emit({"id": 0, "result": "done"})
    child = FakeChild(script)
    with child.client(ctx, handle_request=lambda *args: client.request("inner", {})) as client:
        assert client.request("outer", {}) == "done"


def test_rpc_errors_are_matched_by_id_and_do_not_parse_prose(ctx):
    error = {"code": -32000, "message": "rate limit exceeded", "data": {"future": True}}
    child = FakeChild(lambda child, msg: child.emit({"id": msg["id"], "error": error}))
    with child.client(ctx) as client:
        with pytest.raises(A.RpcError) as caught:
            client.request("test", {})
        assert caught.value.error == error
        assert str(caught.value) == "App-server request failed"


def test_timeout_keeps_pending_response_and_eof_fails_without_hanging(ctx):
    child = FakeChild()
    with child.client(ctx) as client:
        id = client.send_request("pending", {})
        with pytest.raises(TimeoutError):
            client.wait_response(id, timeout=0.01)
        child.emit({"id": id, "result": "late"})
        assert client.wait_response(id) == "late"
        child.finish(0)
        with pytest.raises(EOFError):
            client.next_event()
        with pytest.raises(EOFError):
            client.next_event()


@pytest.mark.parametrize("line", ["not json\n", "[]\n", '{"id": 900, "result": {}}\n'])
def test_bad_protocol_still_closes_child(ctx, line):
    child = FakeChild()
    with pytest.raises(ValueError):
        with child.client(ctx) as client:
            child.stdout.lines.put(line)
            client.next_event()
    assert child.stdin.closed and child.stdout.closed


def test_shutdown_closes_stdin_then_terminates_group_and_is_idempotent(ctx):
    child = FakeChild(stubborn=True)
    client = child.client(ctx)
    client.close()
    client.close()
    assert child.stops == [child.pid]
    assert child.returncode == -15
    assert child.stdout.closed and not client._reader.is_alive()


def test_lifecycle_guards_and_optional_resume_overrides(ctx):
    child = FakeChild(lambda child, msg: child.reply(msg, {}) if "id" in msg else None)
    with child.client(ctx) as client:
        with pytest.raises(ValueError, match="Initialize"):
            client.start_thread()
        client.initialize()
        with pytest.raises(ValueError, match="already initialized"):
            client.initialize()
        client.resume_thread("thread")
        assert child.sent[-1]["params"] == {"threadId": "thread"}
    with pytest.raises(RuntimeError, match="closed"):
        client.request("test", {})


def test_event_flood_drops_only_chatter_and_never_a_signal(ctx):
    """Chatter accumulates while a request is in flight; signals must still survive it.

    A turn emits far more progress than its reply, so wait_response pumps hundreds of
    notifications before the supervisor next drains them. That buffer must stay bounded, while a
    usage update or an error signal — the whole reason this client exists — is never discarded.
    """
    flood = A.MAX_PLAIN_EVENTS * 3

    def script(child, message):
        # Everything below arrives BEFORE the reply, so wait_response pumps it all while the
        # caller is blocked and cannot drain events.
        if message.get("method") == "turn/start":
            child.emit({"method": "thread/tokenUsage/updated",
                        "params": {"tokenUsage": {"total": {"inputTokens": 7}, "last": {}}}})
            for i in range(flood):
                child.emit({"method": "agent/message/delta", "params": {"text": f"chunk {i}"}})
            child.emit({"method": "error",
                        "params": {"error": {"codexErrorInfo": "usageLimitExceeded"}}})
            child.reply(message, {})

    child = FakeChild(script)
    with child.client(ctx) as server:
        server.request("turn/start", {"threadId": "t", "input": []})
        drained = []
        while True:
            try:
                drained.append(server.next_event(timeout=0.3))
            except TimeoutError:
                break

    signals = [event.signal for event in drained if event.signal is not None]
    assert len(signals) == 2, "signal-bearing notifications must never be discarded"
    assert signals[0].total.input_tokens == 7
    assert isinstance(signals[1], A.ErrorSignal) and signals[1].category == "insufficient_quota"
    plain = [event for event in drained if event.signal is None]
    assert len(plain) == A.MAX_PLAIN_EVENTS, "signal-less chatter must be capped"
    assert server.dropped_events == flood - A.MAX_PLAIN_EVENTS  # loss reported, not hidden
