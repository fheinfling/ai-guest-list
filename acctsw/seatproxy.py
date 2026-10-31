"""Local seat proxy: a running Claude Code session continues on another seat without a restart.

Each launcher run owns one :class:`SeatProxy`, a ``ThreadingHTTPServer`` on ``127.0.0.1:0``. The
child gets ``ANTHROPIC_BASE_URL=<start()>``, a URL whose path is a random per-run secret; a request
for any other path gets a 403. Every request is forwarded to ``upstream`` with the client's own
headers, except ``POST /v1/messages`` and ``POST /v1/messages/count_tokens``: those carry the token
of the BOUND seat when it has one. A seat without a token is the client's own login (the home seat),
so a proxy bound to home is a pure passthrough.

Before the first response byte reaches the client, two upstream answers are replayed instead of
passed on, at most once per seat credential:

- a usage limit (429 with ``anthropic-ratelimit-unified-status: rejected``), on any seat;
- a 401 on a seat whose token the proxy injected (a 401 on home is the client's to handle).

The proxy asks ``decide(hit)`` for the next seat, on a helper thread, for at most
``decide_timeout`` seconds; the seat it answers is bound for later requests too. No answer, or a
late one, passes a limit through unchanged (the launcher's restart path takes over) and replays an
injected 401 with the client's own login instead. Transient 429s and 529s always pass through.

This module does I/O only: it never reads state.json or the Keychain. Tokens live in memory and are
logged as ``sha256[:10]`` only. A handler exception answers 502 and leaves the listener running.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import unquote, urlsplit

INJECT_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                        "proxy-connection", "te", "trailer", "trailers", "transfer-encoding",
                        "upgrade", "host", "content-length", "expect"})
CLIENT_AUTH = frozenset({"authorization", "x-api-key"})
MAX_BODY = 64 * 1024 * 1024
MAX_TRIES = 8                  # upstream sends per request, whatever ``decide`` answers
UPSTREAM_TIMEOUT = 660.0       # long thinking pauses between SSE events
CLIENT_IDLE_TIMEOUT = 900.0    # an idle keep-alive client connection frees its thread after this
CHUNK = 65536


@dataclass(frozen=True)
class Seat:
    email: str
    token: str | None = field(default=None, repr=False)   # None: the client's own login


@dataclass(frozen=True)
class Hit:
    kind: str                  # "limit" | "auth"
    seat: str                  # email of the seat the request went out on
    reset_at: str | None       # ISO 8601, from anthropic-ratelimit-unified-reset
    detail: str | None         # upstream error message, if any
    claim: str | None          # anthropic-ratelimit-unified-representative-claim, e.g. "five_hour"


def _noop(*_args, **_kwargs) -> None:
    pass


def tok(secret: str | None) -> str | None:
    """How a token appears in logs: the first 10 hex digits of its sha256, never the text."""
    return hashlib.sha256(secret.encode()).hexdigest()[:10] if secret else None


def _header(headers, name: str) -> str | None:
    """Case-insensitive lookup in an ``HTTPMessage``, a mapping or a list of pairs."""
    items = headers.items() if hasattr(headers, "items") else headers
    for k, v in items:
        if k.lower() == name:
            return v
    return None


def classify(status: int, headers) -> str | None:
    """``"limit"`` for a usage-limit 429, ``"auth"`` for a 401, None for everything else.

    Only a 429 the unified limiter REJECTED is a seat limit; a 429 without that verdict is a
    transient rate limit the client retries on its own, like a 529."""
    if status == 429:
        verdict = (_header(headers, "anthropic-ratelimit-unified-status") or "").strip().lower()
        return "limit" if verdict == "rejected" else None
    if status == 401:
        return "auth"
    return None


def _reset_iso(headers) -> str | None:
    raw = (_header(headers, "anthropic-ratelimit-unified-reset") or "").strip()
    try:
        return datetime.fromtimestamp(float(raw), timezone.utc).isoformat() if raw else None
    except (ValueError, OverflowError, OSError):
        return None


def _error_detail(data: bytes) -> str | None:
    try:
        err = json.loads(data).get("error") or {}
        message = err.get("message") if isinstance(err, dict) else None
    except (ValueError, AttributeError):
        return None
    return str(message)[:200] if message else None


def _key(seat: Seat) -> tuple:
    """One try per credential: the client's own login is one, each seat token another (a seat
    re-read after a 401 with a fresh token is a new credential)."""
    return ("client",) if seat.token is None else ("seat", seat.email, seat.token)


def _label(seat: Seat) -> str:
    return seat.email or "client"


# --- upstream ----------------------------------------------------------------------------------

def _tunnel_proxy(scheme: str, host: str) -> str | None:
    """The ``HTTPS_PROXY``/``HTTP_PROXY`` to tunnel through, unless ``NO_PROXY`` exempts ``host``.

    Environment only: the macOS system proxy settings are not consulted."""
    env = urllib.request.getproxies_environment()
    if urllib.request.proxy_bypass_environment(host, env):
        return None
    return env.get(scheme) or env.get("all") or None


class _Upstream:
    """Keep-alive connections to one origin, through a CONNECT tunnel when a proxy is configured."""

    def __init__(self, url: str, timeout: float = UPSTREAM_TIMEOUT):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"upstream must be an http(s) URL, got {url!r}")
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.netloc = u.netloc.rpartition("@")[2]
        self.base = u.path.rstrip("/")
        self.timeout = timeout
        self.ssl = ssl.create_default_context() if self.https else None
        self.tunnel: tuple[str, int, dict] | None = None
        proxy = _tunnel_proxy(u.scheme, self.host)
        if proxy:
            p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            headers = {}
            if p.username:
                cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
                headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
            self.tunnel = (p.hostname, p.port or 8080, headers)
        self._idle: list[http.client.HTTPConnection] = []
        self._lock = threading.Lock()

    def _new(self) -> http.client.HTTPConnection:
        host, port = (self.tunnel[0], self.tunnel[1]) if self.tunnel else (self.host, self.port)
        if self.https:
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self.ssl)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        if self.tunnel:
            conn.set_tunnel(self.host, self.port, headers=self.tunnel[2])
        return conn

    def acquire(self, fresh: bool = False) -> tuple[http.client.HTTPConnection, bool]:
        """A connection and whether it was reused from the pool."""
        if not fresh:
            with self._lock:
                if self._idle:
                    return self._idle.pop(), True
        return self._new(), False

    def release(self, conn) -> None:
        with self._lock:
            self._idle.append(conn)

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()


# --- client side -------------------------------------------------------------------------------

class _Reply:
    """The response to the client, written as raw HTTP/1.1 onto a binary file."""

    def __init__(self, wfile, *, head_only: bool = False):
        self.wfile = wfile
        self.head_only = head_only
        self.started = False
        self.chunked = False

    def head(self, status: int, reason: str, headers: list[tuple[str, str]]) -> None:
        self.started = True    # before the write: a failed first write still counts as sent
        lines = [f"HTTP/1.1 {status} {reason or http.client.responses.get(status, '')}"]
        lines += [f"{k}: {v}" for k, v in headers]
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self.wfile.flush()

    def chunk(self, data: bytes) -> None:
        if self.head_only or not data:
            return
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n" if self.chunked else data)
        self.wfile.flush()

    def end(self) -> None:
        if self.chunked and not self.head_only:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def whole(self, status: int, reason: str, headers: list[tuple[str, str]], data: bytes) -> None:
        self.head(status, reason, [*headers, ("content-length", str(len(data)))])
        self.chunk(data)
        self.end()

    def error(self, status: int, kind: str, message: str) -> None:
        data = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        self.whole(status, "", [("content-type", "application/json")], data)


def _forward_headers(headers, token: str | None, netloc: str, length: int | None) -> dict:
    items = list(headers.items() if hasattr(headers, "items") else headers)
    named = {n.strip().lower() for k, v in items if k.lower() == "connection" for n in v.split(",")}
    out: dict[str, str] = {}
    for k, v in items:
        low = k.lower()
        if low in HOP_BY_HOP or low in named or (token is not None and low in CLIENT_AUTH):
            continue
        out[k] = f"{out[k]}, {v}" if k in out else v
    if token is not None:
        out["Authorization"] = f"Bearer {token}"
    out["Host"] = netloc
    if length is not None:
        out["Content-Length"] = str(length)
    return out


def _response_headers(resp) -> list[tuple[str, str]]:
    return [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]


# --- the proxy ---------------------------------------------------------------------------------

class SeatProxy:
    def __init__(self, decide: Callable[[Hit], Seat | None], *, upstream="https://api.anthropic.com",
                 decide_timeout=10.0, log: Callable[..., None] = _noop):
        self._decide = decide
        self._decide_timeout = decide_timeout
        self._log_fn = log
        self._transport = _Upstream(upstream)
        self._secret = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._decide_lock = threading.Lock()
        self._stalled_until = 0.0         # after a decide timeout, the rest of a burst asks nobody
        self._seat = Seat("", None)
        self._known: set[str] = set()     # every token ever bound, for redacting exception texts
        self._active = 0
        self._last = time.monotonic()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> str:
        """Listen on 127.0.0.1 (ephemeral port) and return the base URL for the child.

        Raises ``OSError`` when the port cannot be bound; the caller falls back to restarts."""
        if self._server is not None:
            return self._base_url()
        server = _Server(("127.0.0.1", 0), _make_handler(self))
        server.proxy = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="seatproxy", daemon=True)
        self._thread.start()
        self._log("proxy_start", port=server.server_address[1])
        return self._base_url()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self._transport.close()
        self._log("proxy_stop")

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/{self._secret}"

    # -- seat binding and activity --

    def bind(self, seat: Seat) -> None:
        with self._lock:
            changed = (seat.email, seat.token) != (self._seat.email, self._seat.token)
            self._seat = seat
            if seat.token:
                self._known.add(seat.token)
        if changed:
            self._log("bind", seat=_label(seat), token=tok(seat.token))

    def bound(self) -> Seat:
        with self._lock:
            return self._seat

    def in_flight(self) -> int:
        with self._lock:
            return self._active

    def idle_for(self) -> float:
        """Seconds since the last request finished; 0.0 while any request is in flight."""
        with self._lock:
            return 0.0 if self._active else max(0.0, time.monotonic() - self._last)

    # -- logging --

    def _log(self, event: str, **fields) -> None:
        try:
            self._log_fn(event, **fields)
        except Exception:
            pass

    def _redact(self, text: str, *extra: str | None) -> str:
        """``text`` with every known secret replaced by its hash (exception texts may quote one)."""
        with self._lock:
            known = [*self._known, self._secret]
        for value in (*extra, *known):
            for s in (value, (value or "").partition(" ")[2]):    # "Bearer <token>" and <token>
                if s:
                    text = text.replace(s, f"<{tok(s)}>")
        return text

    # -- request handling (socket-free: the server below only parses and calls this) --

    def handle(self, method: str, target: str, headers, body: bytes, wfile) -> bool:
        """Serve one request; the response goes to ``wfile``. Returns whether the client connection
        may be kept alive. Never raises for an upstream or handler fault: those answer 502."""
        reply = _Reply(wfile, head_only=method == "HEAD")
        with self._lock:
            self._active += 1
        try:
            return self._handle(method, target, headers, body, reply)
        except Exception as e:
            client = [v for k, v in (headers.items() if hasattr(headers, "items") else headers)
                      if k.lower() in CLIENT_AUTH]
            self._log("proxy_error", error=self._redact(f"{e.__class__.__name__}: {e}", *client))
            if not reply.started:
                try:
                    reply.error(502, "api_error", "seat proxy: internal error")
                except OSError:
                    pass
            return False
        finally:
            with self._lock:
                self._active -= 1
                self._last = time.monotonic()

    def _route(self, target: str) -> str | None:
        """The upstream path for ``target``, or None when it does not start with the run's secret."""
        n = len(self._secret)
        head, rest = target[1:n + 1], target[n + 1:]
        if not (target.startswith("/") and secrets.compare_digest(head.encode(), self._secret.encode())
                and rest[:1] in ("", "/", "?")):
            return None
        return rest if rest.startswith("/") else "/" + rest

    def _handle(self, method: str, target: str, headers, body: bytes, reply: _Reply) -> bool:
        path = self._route(target)
        if path is None:
            self._log("forbidden", method=method)
            reply.error(403, "permission_error", "seat proxy: unknown path")
            return True
        inject = method == "POST" and urlsplit(path).path in INJECT_PATHS
        seat = self.bound() if inject else None    # None: the client's headers go out untouched
        tried = [seat] if seat is not None else []
        length = len(body) if body or method in ("POST", "PUT", "PATCH") else None
        t0 = time.monotonic()
        fresh = False
        while True:
            token = seat.token if seat is not None else None
            conn, reused = self._transport.acquire(fresh)
            try:
                conn.request(method, self._transport.base + path, body=body or None,
                             headers=_forward_headers(headers, token, self._transport.netloc, length))
                resp = conn.getresponse()
                kind = classify(resp.status, resp.headers) if seat is not None else None
                if kind == "auth" and seat.token is None:
                    kind = None    # the client's own login: it re-reads its own credentials
                if kind and not reply.started and len(tried) < MAX_TRIES:
                    data = resp.read()
                    self._done(conn, resp)
                    nxt = self._next_seat(kind, seat, resp.headers, data, tried)
                    if nxt is not None:
                        self._log("replay", kind=kind, seat=_label(seat), token=tok(token),
                                  next=_label(nxt), next_token=tok(nxt.token), path=path)
                        seat = nxt
                        tried.append(nxt)
                        continue
                    reply.whole(resp.status, resp.reason, _response_headers(resp), data)
                    self._request_log(method, path, seat, tried, resp.status, t0)
                    return True
                self._relay(resp, reply)
                self._done(conn, resp)
                self._request_log(method, path, seat, tried, resp.status, t0)
                return True
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                error = self._redact(f"{e.__class__.__name__}: {e}", token)
                if reply.started:
                    # The client holds part of a response: resending would splice a second one into
                    # it. Drop the connection; the client retries the request on its own.
                    self._log("upstream_broken", path=path, error=error)
                    return False
                if reused and not fresh:
                    fresh = True    # a pooled keep-alive socket the upstream had already closed
                    continue
                self._log("upstream_error", path=path, error=error)
                reply.error(502, "api_error", "seat proxy: upstream unreachable")
                return True

    def _done(self, conn, resp) -> None:
        if resp.will_close:
            conn.close()
        else:
            self._transport.release(conn)

    def _relay(self, resp, reply: _Reply) -> None:
        """Stream the upstream response through, flushing every chunk (SSE must not buffer)."""
        headers = _response_headers(resp)
        length = resp.getheader("content-length")
        no_body = reply.head_only or resp.status in (204, 304) or 100 <= resp.status < 200
        if length is not None and resp.getheader("transfer-encoding") is None:
            headers.append(("content-length", length))
        elif not no_body:
            reply.chunked = True
            headers.append(("transfer-encoding", "chunked"))
        reply.head(resp.status, resp.reason, headers)
        while not no_body:
            data = resp.read1(CHUNK)
            if not data:
                break
            reply.chunk(data)
        reply.end()

    def _next_seat(self, kind: str, seat: Seat, headers, data: bytes, tried: list[Seat]) -> Seat | None:
        """The seat to replay on after ``kind`` hit ``seat``, or None to pass the answer through."""
        hit = Hit(kind, seat.email, _reset_iso(headers) if kind == "limit" else None,
                  _error_detail(data), _header(headers, "anthropic-ratelimit-unified-representative-claim"))
        done = {_key(s) for s in tried}
        with self._decide_lock:
            current = self.bound()
            if _key(current) != _key(seat) and _key(current) not in done:
                return current    # a concurrent request already moved on: follow it, ask nobody
            self._log("hit", kind=kind, seat=_label(seat), token=tok(seat.token),
                      reset_at=hit.reset_at, claim=hit.claim)
            answer = self._ask(hit) if time.monotonic() >= self._stalled_until else None
            if answer is not None and _key(answer) not in done:
                self.bind(answer)
                return answer
        if kind == "auth" and ("client",) not in done:
            return Seat("", None)    # fail open: this request goes out on the client's own login
        return None

    def _ask(self, hit: Hit) -> Seat | None:
        """``decide(hit)`` on a helper thread, for at most ``decide_timeout`` seconds."""
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["seat"] = self._decide(hit)
            except Exception as e:
                box["error"] = f"{e.__class__.__name__}: {e}"
            finally:
                done.set()

        threading.Thread(target=run, name="seatproxy-decide", daemon=True).start()
        if not done.wait(self._decide_timeout):
            self._stalled_until = time.monotonic() + self._decide_timeout
            self._log("decide_timeout", kind=hit.kind, seat=hit.seat)
            return None
        if "error" in box:
            self._log("decide_error", kind=hit.kind, seat=hit.seat, error=self._redact(box["error"]))
        seat = box.get("seat")
        return seat if isinstance(seat, Seat) else None

    def _request_log(self, method, path, seat, tried, status, t0) -> None:
        self._log("request", method=method, path=path, status=status,
                  seat=_label(seat) if seat is not None else None,
                  tried=[[_label(s), tok(s.token)] for s in tried],
                  ms=round((time.monotonic() - t0) * 1000))


# --- socket glue -------------------------------------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    proxy: SeatProxy

    def handle_error(self, request, client_address):
        # The default prints a traceback to stderr, which is the user's terminal.
        self.proxy._log("connection_error")


def _make_handler(proxy: SeatProxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = CLIENT_IDLE_TIMEOUT

        def log_message(self, *_args):
            pass

        def _serve(self):
            try:
                body = self._body()
            except (ValueError, OSError):
                self.close_connection = True
                _Reply(self.wfile).error(400, "invalid_request_error", "seat proxy: bad request body")
                return
            if body is None:
                self.close_connection = True
                _Reply(self.wfile).error(413, "request_too_large", "seat proxy: request too large")
                return
            if not proxy.handle(self.command, self.path, self.headers, body, self.wfile):
                self.close_connection = True

        def _body(self) -> bytes | None:
            if (self.headers.get("transfer-encoding") or "").lower() == "chunked":
                parts, total = [], 0
                while True:
                    size = int(self.rfile.readline(1024).split(b";")[0], 16)
                    if size == 0:
                        while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                            pass
                        return b"".join(parts)
                    total += size
                    if total > MAX_BODY:
                        return None
                    parts.append(self.rfile.read(size))
                    self.rfile.readline(1024)
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                return None
            return self.rfile.read(n) if n > 0 else b""

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

    return Handler
