"""Fake Anthropic Messages API for the seat proxy tests and scripts/smoke-inplace.sh. Never forwards.

:class:`FakeAnthropic` decides every answer from its rules, without sockets: the unit tests drive it
through an in-memory transport, :func:`serve` puts it behind a real ``ThreadingHTTPServer``, and
``python fake_anthropic.py --port-file F --log L`` runs it standalone (``POST /__fake/rules`` patches
the rules, ``GET /__fake/log`` returns the request log).

Rules:

- ``status_by_token``: token sha256[:10] -> 401 | 429 (a usage limit) | 4291 (a transient 429) | 529
- ``next``: forced statuses for the next ``/v1/messages`` calls, consumed in order
- ``tool_first``: answer a fresh user turn with a Bash ``tool_use``, then text once a result is back
- ``chunk_delay``: seconds between SSE deltas, to prove the proxy does not buffer a stream

Tokens are logged as sha256[:10] only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TRANSIENT_429 = 4291


def tok(secret: str | None) -> str | None:
    return hashlib.sha256(secret.encode()).hexdigest()[:10] if secret else None


def bearer(headers: dict) -> str | None:
    low = {k.lower(): v for k, v in headers.items()}
    auth = low.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else low.get("x-api-key")


DEFAULT_RULES = {"status_by_token": {}, "next": [], "tool_first": False,
                 "tool_command": "echo seatproxy-tool-ok", "chunk_delay": 0.0}


@dataclass
class Answer:
    status: int
    headers: list[tuple[str, str]]
    chunks: list[bytes] = field(default_factory=list)   # the body, one element per wire chunk
    chunked: bool = False                                # no content-length: sent chunk by chunk
    delay: float = 0.0                                   # seconds before each chunk after the first


def _json(status: int, payload, extra: dict | None = None) -> Answer:
    data = json.dumps(payload).encode()
    headers = [("content-type", "application/json"), ("content-length", str(len(data))),
               ("request-id", "req_fake")] + list((extra or {}).items())
    return Answer(status, headers, [data])


def limit_headers(reset: int, status: str = "rejected") -> dict:
    return {"retry-after": "3600",
            "anthropic-ratelimit-unified-status": status,
            "anthropic-ratelimit-unified-reset": str(reset),
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
            "anthropic-ratelimit-unified-5h-status": status,
            "anthropic-ratelimit-unified-5h-reset": str(reset)}


def _has_tool_result(body: dict) -> bool:
    for m in body.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content):
            return True
    return False


class FakeAnthropic:
    """Rules plus a request log. ``respond`` is the whole fake API."""

    def __init__(self, log_path: Path | None = None):
        self.lock = threading.Lock()
        self.rules = json.loads(json.dumps(DEFAULT_RULES))
        self.requests: list[dict] = []
        self.log_path = log_path
        self.reset = int(time.time()) + 3600

    def update(self, patch: dict) -> dict:
        with self.lock:
            self.rules.update(patch)
            return json.loads(json.dumps(self.rules))

    def _status(self, token: str | None) -> int:
        with self.lock:
            if self.rules["next"]:
                return int(self.rules["next"].pop(0))
            return int(self.rules["status_by_token"].get(tok(token) or "", 200))

    def _record(self, entry: dict) -> None:
        with self.lock:
            entry = {"seq": len(self.requests) + 1, **entry}
            self.requests.append(entry)
            if self.log_path:
                with self.log_path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")

    def respond(self, method: str, target: str, headers: dict, raw: bytes) -> Answer:
        path = target.split("?")[0]
        token = bearer(headers)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        is_msg = method == "POST" and path == "/v1/messages"
        status = self._status(token) if is_msg else 200
        low = {k.lower(): v for k, v in headers.items()}
        self._record({"method": method, "path": target, "bearer": tok(token),
                      "x_api_key": "x-api-key" in low, "host": low.get("host"),
                      "accept_encoding": low.get("accept-encoding"), "status": status,
                      "stream": bool(body.get("stream")), "body_bytes": len(raw)})
        if method == "POST" and path == "/v1/messages/count_tokens":
            return _json(200, {"input_tokens": 42})
        if not is_msg:
            return _json(404, {"type": "error", "error": {"type": "not_found_error",
                                                          "message": f"fake has no {path}"}})
        if status == 401:
            return _json(401, {"type": "error", "error": {"type": "authentication_error",
                                                          "message": "OAuth token has expired."}})
        if status == 429:
            return _json(429, {"type": "error", "error": {
                "type": "rate_limit_error", "message": "This request would exceed your account's rate limit."}},
                limit_headers(self.reset))
        if status == TRANSIENT_429:
            return _json(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                          "message": "Slow down."}},
                         {"retry-after": "1", "anthropic-ratelimit-unified-status": "allowed"})
        if status == 529:
            return _json(529, {"type": "error", "error": {"type": "overloaded_error",
                                                          "message": "Overloaded"}})
        return self._answer(body, token)

    def _answer(self, body: dict, token: str | None) -> Answer:
        rules = self.rules
        label = tok(token) or "none"
        model = body.get("model", "claude-fake")
        mid = f"msg_fake_{len(self.requests)}"
        ok = {"anthropic-ratelimit-unified-status": "allowed",
              "anthropic-ratelimit-unified-reset": str(self.reset)}
        if rules["tool_first"] and not _has_tool_result(body):
            block = {"type": "tool_use", "id": f"toolu_fake_{len(self.requests)}", "name": "Bash",
                     "input": {"command": rules["tool_command"], "description": "seatproxy smoke"}}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": f"hello from fake upstream, seat {label}"}
            stop = "end_turn"
        if not body.get("stream"):
            return _json(200, {"id": mid, "type": "message", "role": "assistant", "model": model,
                               "content": [block], "stop_reason": stop, "stop_sequence": None,
                               "usage": {"input_tokens": 10, "output_tokens": 5}}, ok)

        def ev(name, data) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

        chunks = [ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})]
        if block["type"] == "text":
            chunks.append(ev("content_block_start", {"type": "content_block_start", "index": 0,
                                                     "content_block": {"type": "text", "text": ""}}))
            for piece in block["text"].split(" "):
                chunks.append(ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                         "delta": {"type": "text_delta", "text": piece + " "}}))
        else:
            chunks.append(ev("content_block_start", {"type": "content_block_start", "index": 0,
                                                     "content_block": {**block, "input": {}}}))
            js = json.dumps(block["input"])
            for i in range(0, len(js), 12):
                chunks.append(ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                         "delta": {"type": "input_json_delta",
                                                                   "partial_json": js[i:i + 12]}}))
        chunks += [ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ev("message_delta", {"type": "message_delta",
                                        "delta": {"stop_reason": stop, "stop_sequence": None},
                                        "usage": {"output_tokens": 5}}),
                   ev("message_stop", {"type": "message_stop"})]
        headers = [("content-type", "text/event-stream"), ("cache-control", "no-cache"),
                   ("request-id", "req_fake")] + list(ok.items())
        return Answer(200, headers, chunks, chunked=True, delay=float(rules["chunk_delay"]))


def make_handler(fake: FakeAnthropic):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _serve(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self.path.startswith("/__fake/"):
                return self._control(raw)
            ans = fake.respond(self.command, self.path, dict(self.headers.items()), raw)
            self.send_response(ans.status)
            for k, v in ans.headers:
                self.send_header(k, v)
            if ans.chunked:
                self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for i, chunk in enumerate(ans.chunks):
                if i and ans.delay:
                    time.sleep(ans.delay)
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" if ans.chunked else chunk)
                self.wfile.flush()
            if ans.chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        def _control(self, raw: bytes):
            if self.path.startswith("/__fake/rules"):
                payload = fake.update(json.loads(raw)) if raw else fake.update({})
            elif self.path.startswith("/__fake/log"):
                payload = list(fake.requests)
            else:
                payload = {"error": "unknown control path"}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _serve

    return Handler


def serve(fake: FakeAnthropic, port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(fake))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file", required=True)
    ap.add_argument("--log", required=True, help="JSONL request log")
    args = ap.parse_args()
    srv = serve(FakeAnthropic(Path(args.log)), args.port)
    Path(args.port_file).write_text(str(srv.server_address[1]))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
