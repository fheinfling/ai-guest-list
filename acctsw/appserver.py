"""Supervise BYOK through typed app-server traffic that rollout files do not retain.

One reader drains stdout; a single caller pumps messages while awaiting responses or events.
Responses are buffered by id, notifications are queued, and server requests are answered during
every wait. This avoids the read-one-line-per-request deadlock, including reentrant requests from
an approval handler. Call next_event continuously after turn/start to supervise the running turn.
The transport and group termination are injectable; nothing launches at import time.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
from threading import Thread, Timer
from time import monotonic
from typing import Any, Callable, Protocol, TextIO

from .context import Context
from .launcher import _terminate
from .providers import Provider, classify_error


class Child(Protocol):
    stdin: TextIO
    stdout: TextIO
    pid: int
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[..., Child]
RequestHandler = Callable[[str, dict], Any]

# How many signal-less notifications to retain before dropping the oldest. Generous enough that a
# supervisor polling on the launcher's seconds-scale tick keeps full context around a signal, small
# enough that an undrained session cannot grow without bound. Usage and error signals are exempt.
MAX_PLAIN_EVENTS = 512


def _spawn(argv: list[str], **kwargs: Any) -> Child:
    return subprocess.Popen(argv, **kwargs)


def _kill_group(pid: int) -> None:
    # start_new_session makes pid the group id. Do not look it up via getpgid: the leader
    # may already have exited while a descendant still holds stdout open.
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def classify_codex_error(provider: Provider, info: Any) -> str:
    """Use typed Codex discriminators, never prose or an assumed OpenAI wire vocabulary."""
    if isinstance(info, str):
        return {"unauthorized": "invalid_key", "usageLimitExceeded": "insufficient_quota",
                "rateLimitExceeded": "rate_limited",
                "sessionBudgetExceeded": "insufficient_quota"}.get(info, "unknown")
    if isinstance(info, dict) and len(info) == 1:
        for variant in ("httpConnectionFailed", "responseStreamConnectionFailed",
                        "responseStreamDisconnected", "responseTooManyFailedAttempts"):
            details = info.get(variant)
            if isinstance(details, dict) and type(details.get("httpStatusCode")) is int:
                # Codex forwards status but not the provider's documented error code/type.
                # In particular, a bare 429 cannot distinguish empty credit from throttling.
                return classify_error(provider, details["httpStatusCode"], {"error": {}})
    return "unknown"


@dataclass(frozen=True)
class TokenCounts:
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None

    @classmethod
    def parse(cls, data: Any) -> TokenCounts:
        data = data if isinstance(data, dict) else {}
        # Missing upstream usage is unknown, not free. Reasoning stays separate for the later
        # provider-specific adapter; adding it to output here would double-count many APIs.
        def count(name: str) -> int | None:
            value = data.get(name)
            return value if type(value) is int and value >= 0 else None
        return cls(*(count(name) for name in ("inputTokens", "cachedInputTokens",
                   "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")))


@dataclass(frozen=True)
class UsageUpdate:
    total: TokenCounts
    last: TokenCounts


@dataclass(frozen=True)
class ErrorSignal:
    category: str
    info: Any


@dataclass(frozen=True)
class Notification:
    method: str
    params: dict
    signal: UsageUpdate | ErrorSignal | None = None


def notification(provider: Provider, method: str, params: dict) -> Notification:
    signal = None
    if method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage")
        usage = usage if isinstance(usage, dict) else {}
        signal = UsageUpdate(TokenCounts.parse(usage.get("total")), TokenCounts.parse(usage.get("last")))
    elif method in ("error", "turn/completed"):
        parent = params if method == "error" else params.get("turn", {})
        error = parent.get("error") if isinstance(parent, dict) else None
        if isinstance(error, dict):
            info = error.get("codexErrorInfo")
            signal = ErrorSignal(classify_codex_error(provider, info), info)
    return Notification(method, params, signal)


class RpcError(RuntimeError):
    """Retain structured errors for callers, without echoing server prose in diagnostics."""

    def __init__(self, error: dict):
        self.error = error
        super().__init__("App-server request failed")


class AppServer:
    """Single-caller JSONL client with pipelined request ids and an explicit event pump.

    Use as a context manager to guarantee shutdown on handshake, protocol or callback failures.
    handle_request returns the server request's result, or raises RpcError with an error object.
    Without a handler, requests receive method-not-found; approvals are never silently accepted.
    The future launcher owns the key seat's env/CODEX_HOME; this client does not construct homes.
    lifetime opts into a hard watchdog and unconditional group cleanup for short-lived probes;
    its kill_group callback only signals, leaving child.wait to reap the process during close.
    """

    def __init__(self, ctx: Context, provider: Provider, *, spawn: Spawn = _spawn,
                 terminate: Callable[[int], int] = _terminate,
                 handle_request: RequestHandler | None = None, env: dict[str, str] | None = None,
                 cwd: Path | None = None, timeout: float = 30,
                 lifetime: float | None = None,
                 kill_group: Callable[[int], None] = _kill_group,
                 config_overrides: dict[str, Any] | None = None):
        self.provider = provider
        self.timeout = timeout
        self._terminate = terminate
        self._handle_request = handle_request
        self._next_id = 0
        self._pending: set[int] = set()
        self._responses: dict[int, dict] = {}
        self._events: deque[Notification] = deque()
        self.dropped_events = 0
        self._plain = 0
        self._incoming: Queue[str | None] = Queue()
        self._closed = False
        self._ended = False
        self._initialized = False
        self.expired = False
        self._kill_group = kill_group
        self._watchdog = None
        argv = [ctx.codex_bin or "codex", "app-server"]
        for key, value in (config_overrides or {}).items():
            argv += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        self.child = spawn(argv, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                           encoding="utf-8", bufsize=1, start_new_session=True, env=env, cwd=cwd)
        # Stderr is not part of JSON-RPC, and an unread stderr pipe could stall the child.
        self._reader = Thread(target=self._read, name="acctsw-appserver", daemon=True)
        try:
            self._reader.start()
            if lifetime is not None:
                # Unlike a per-response timeout this also bounds chatter and a blocked write.
                self._watchdog = Timer(lifetime, self._expire)
                self._watchdog.daemon = True
                self._watchdog.start()
        except BaseException:
            self._kill_group(self.child.pid)
            self.child.wait(timeout=2)
            raise

    def _expire(self) -> None:
        self.expired = True
        self._kill_group(self.child.pid)

    def _read(self) -> None:
        try:
            for line in self.child.stdout:
                self._incoming.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._incoming.put(None)

    def _write(self, message: dict) -> None:
        if self._closed:
            raise RuntimeError("App-server is closed")
        # Codex uses JSON-RPC 2.0 with the jsonrpc member omitted, one message per line.
        self.child.stdin.write(json.dumps(message) + "\n")
        self.child.stdin.flush()

    def send_request(self, method: str, params: dict) -> int:
        id = self._next_id
        self._next_id += 1
        self._pending.add(id)
        try:
            self._write({"id": id, "method": method, "params": params})
        except Exception:
            self._pending.remove(id)
            raise
        return id

    def _pump(self, deadline: float) -> None:
        if self.expired:
            raise TimeoutError("App-server lifetime expired")
        if self._closed or self._ended:
            raise EOFError("App-server stream closed")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("App-server response timed out")
        try:
            line = self._incoming.get(timeout=remaining)
        except Empty:
            raise TimeoutError("App-server response timed out") from None
        if line is None:
            self._ended = True
            if self.expired:
                raise TimeoutError("App-server lifetime expired")
            raise EOFError("App-server stream closed")
        try:
            message = json.loads(line)
        except ValueError:
            raise ValueError("Invalid app-server JSON") from None
        if not isinstance(message, dict):
            raise ValueError("Invalid app-server message")
        if "method" in message:
            method, params = message["method"], message.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Invalid app-server method or params")
            if "id" in message:
                # Requests and responses have independent id spaces; a server approval may
                # have exactly the same id as a pending client request.
                try:
                    if self._handle_request is None:
                        raise RpcError({"code": -32601, "message": "Client method not supported"})
                    result = self._handle_request(method, params)
                    reply = {"result": result}
                except RpcError as e:
                    reply = {"error": e.error}
                except Exception:
                    # Reply even when a handler fails, so the server isn't parked forever.
                    reply = {"error": {"code": -32603, "message": "Client request handler failed"}}
                self._write({"id": message["id"], **reply})
            else:
                self._record(notification(self.provider, method, params))
        else:
            id = message.get("id")
            if (type(id) is not int or id not in self._pending or id in self._responses
                    or (("result" in message) == ("error" in message))):
                raise ValueError("Unexpected app-server response")
            self._responses[id] = message

    def _record(self, note: Notification) -> None:
        """Buffer a notification, bounding only the ones we never act on.

        A supervised session runs for hours and drains events on a slow tick, while the server
        emits streaming progress far faster. An unbounded buffer would grow for the whole session.
        Dropping indiscriminately is worse than growing, though: losing a usage update corrupts
        the cost meter and losing an error signal is a missed limit — the exact failure this
        module exists to prevent. So signal-bearing notifications are never discarded, and only
        the signal-less remainder is capped, oldest first, with the loss counted rather than
        hidden.
        """
        self._events.append(note)
        if note.signal is not None:
            return
        self._plain += 1
        if self._plain <= MAX_PLAIN_EVENTS:
            return
        # Counted rather than recounted: this runs per notification during a turn. The oldest
        # signal-less entry is normally at the head, so the scan is O(1) in practice and only
        # walks past retained signals.
        for index, event in enumerate(self._events):
            if event.signal is None:
                del self._events[index]
                self._plain -= 1
                self.dropped_events += 1
                return

    def wait_response(self, id: int, *, timeout: float | None = None) -> Any:
        if id not in self._pending:
            raise ValueError("Unknown request id")
        deadline = monotonic() + (self.timeout if timeout is None else timeout)
        while id not in self._responses:
            self._pump(deadline)
        message = self._responses.pop(id)
        self._pending.remove(id)
        if "error" in message:
            raise RpcError(message["error"])
        return message["result"]

    def request(self, method: str, params: dict) -> Any:
        return self.wait_response(self.send_request(method, params))

    def next_event(self, *, timeout: float | None = None) -> Notification:
        deadline = monotonic() + (self.timeout if timeout is None else timeout)
        while not self._events:
            self._pump(deadline)
        note = self._events.popleft()
        if note.signal is None:
            self._plain -= 1
        return note

    def initialize(self, *, name: str = "acctsw", version: str = "1") -> Any:
        if self._initialized:
            raise ValueError("App-server is already initialized")
        result = self.request("initialize", {"clientInfo": {"name": name, "version": version}})
        self._write({"method": "initialized", "params": {}})
        self._initialized = True
        return result

    def _ready(self) -> None:
        if not self._initialized:
            raise ValueError("Initialize app-server before starting or resuming work")

    def start_thread(self, *, model: str | None = None, model_provider: str | None = None) -> Any:
        self._ready()
        return self.request("thread/start", self._overrides(model, model_provider))

    def resume_thread(self, thread_id: str, *, model: str | None = None,
                      model_provider: str | None = None) -> Any:
        self._ready()
        return self.request("thread/resume", {"threadId": thread_id,
                            **self._overrides(model, model_provider)})

    @staticmethod
    def _overrides(model: str | None, provider: str | None) -> dict:
        params = {}
        if model is not None:
            params["model"] = model
        if provider is not None:
            params["modelProvider"] = provider
        return params

    def start_turn(self, thread_id: str, text: str) -> Any:
        self._ready()
        return self.request("turn/start", {"threadId": thread_id,
                            "input": [{"type": "text", "text": text}]})

    def close(self, *, timeout: float = 2) -> None:
        if self._closed:
            return
        self._closed = True
        if self._watchdog is not None:
            # Probes own the entire group, even after a successful turn or a leader's exit.
            # Kill before closing buffered pipes: a blocked writer can hold their lock.
            self._watchdog.cancel()
            self._watchdog.join()
            self._kill_group(self.child.pid)
        try:
            self.child.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if self._watchdog is not None:
                # No unbounded launcher wait in the hard-deadline path.
                self._kill_group(self.child.pid)
                self.child.wait(timeout=timeout)
                return
            # launcher._terminate already signals the group and reaps with waitpid. Inform
            # Popen of the result so it doesn't try to reap the same child a second time.
            self.child.returncode = self._terminate(self.child.pid)
        finally:
            self._reader.join(timeout=timeout)
            # Closing a TextIO while another thread is blocked in read can itself hang.
            if not self._reader.is_alive():
                self.child.stdout.close()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
