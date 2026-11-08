"""Local seat proxy: a running Claude Code session continues on another seat without a restart.

Each launcher run owns one :class:`SeatProxy`, a ``ThreadingHTTPServer`` on ``127.0.0.1:0``. The
child gets ``HTTPS_PROXY=<start()>``, ``http://acctsw:<secret>@127.0.0.1:<port>``, so Claude Code
keeps talking to api.anthropic.com itself and stays a first-party client. The listener accepts
``CONNECT host:port`` with that Basic ``Proxy-Authorization`` (anything else: 407, then close):

- ``CONNECT api.anthropic.com:443`` (the upstream's own host and port) with a per-run ``ca``: the
  client's TLS is terminated with ``ca.server_context()`` and its requests are served below. A
  client that rejects the certificate (a certificate alert, or ``EOF_REJECT_COUNT`` hang-ups
  mid-handshake before any handshake succeeded) reports through ``on_tls_reject(reason)``, once. A ``Connection: upgrade`` request (voice mode's WebSocket) goes
  out verbatim on a fresh upstream connection and, after a 101, is spliced both ways.
- any other host, or no ``ca``: a blind byte tunnel. Never decrypted, never cut for being quiet.

Both go out directly or through ``upstream_proxy`` (the user's own proxy) by CONNECT, unless the
launcher's ``NO_PROXY`` exempts the host. Plain requests under the random path ``/<secret>/`` are
still served the same way (the base-URL transport, kept for tests); any other path gets a 403.

Every decrypted request is forwarded to ``upstream`` with the client's own headers, except
``POST /v1/messages`` and ``POST /v1/messages/count_tokens``: those carry the token of the BOUND
seat when it has one. A seat without a token is the client's own login (the home seat), so a proxy
bound to home is a pure passthrough. Only a request that authenticates the way Claude Code's own
login does (``Authorization: Bearer``, no ``x-api-key``, ``oauth-`` in ``anthropic-beta``) is ever
given a seat token: SDK scripts and MCP servers in the child inherit the proxy too.

Before the first response byte reaches the client, three upstream answers are replayed instead of
passed on, at most once per seat credential:

- a usage limit (429 with ``anthropic-ratelimit-unified-status: rejected``), on any seat;
- a 401 on a seat whose token the proxy injected (a 401 on home is the client's to handle);
- a model rejection on an injected seat (400/403/404, a ``MODEL_ERRORS`` type, "model" in the
  message): that seat's plan may not offer the session's model. ``Hit(kind="model")``.

The proxy asks ``decide(hit)`` for the next seat, on a helper thread, for at most
``decide_timeout`` seconds; the seat it answers is bound for later requests too. No answer, or a
late one, passes a limit or a model rejection through unchanged (the launcher's restart path takes over) and replays an
injected 401 with the client's own login instead. Transient 429s and 529s always pass through.

This module does I/O only: it never reads state.json or the Keychain. Tokens live in memory and are
logged as ``sha256[:10]`` only. A handler exception answers 502 and leaves the listener running.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import secrets
import select
import socket
import ssl
import threading
import time
import urllib.request
import zlib
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
# A seat's plan may not offer the model the session uses: the API answers one of these statuses with
# one of these error types, naming the model in the message.
MODEL_STATUSES = frozenset({400, 403, 404})
MODEL_ERRORS = frozenset({"not_found_error", "permission_error", "invalid_request_error"})
MAX_TRIES = 8                  # upstream sends per request, whatever ``decide`` answers
UPSTREAM_TIMEOUT = 660.0       # long thinking pauses between SSE events
CLIENT_IDLE_TIMEOUT = 900.0    # an idle keep-alive client connection frees its thread after this
TUNNEL_SEND_TIMEOUT = 300.0    # a tunnel peer that takes no bytes for this long ends the tunnel
# The alerts a client sends when it does not accept the certificate (OpenSSL's reason, after
# "TLSV1_ALERT_" or "SSLV3_ALERT_"). Any other handshake failure says nothing about our CA.
CERT_ALERTS = frozenset({"UNKNOWN_CA", "BAD_CERTIFICATE", "CERTIFICATE_UNKNOWN",
                         "UNSUPPORTED_CERTIFICATE", "ACCESS_DENIED", "CERTIFICATE_EXPIRED"})
# Some clients (Claude Code's Bun) reject the certificate by hanging up without an alert. Before any
# intercepted handshake has succeeded, this many such hang-ups within the window count as a reject.
EOF_REJECT_COUNT = 3
EOF_REJECT_WINDOW = 60.0
CHUNK = 65536
PROXY_USER = "acctsw"


@dataclass(frozen=True)
class Seat:
    email: str
    token: str | None = field(default=None, repr=False)   # None: the client's own login


@dataclass(frozen=True)
class Hit:
    kind: str                  # "limit" | "auth" | "model" (the seat cannot use the requested model)
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


def _decoded(headers, data: bytes) -> bytes:
    """An error body without its gzip/deflate content-encoding, for reading only (b"" when it
    cannot be decoded). The client still gets the bytes as they came."""
    encoding = (_header(headers, "content-encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        return data
    if encoding in ("gzip", "x-gzip", "deflate"):
        try:
            return zlib.decompressobj(47).decompress(data, 1 << 20)    # gzip or zlib header, <= 1 MiB
        except zlib.error:
            return b""
    return b""


def _model_rejection(data: bytes) -> bool:
    """Whether an error body says the requested model is not available (to this seat)."""
    try:
        err = json.loads(data).get("error")
    except (ValueError, AttributeError):
        return False
    return (isinstance(err, dict) and err.get("type") in MODEL_ERRORS
            and "model" in str(err.get("message") or "").lower())


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

def _tls_context() -> ssl.SSLContext:
    """The default trust store plus the CA files the user points Python, OpenSSL or Node at (a
    corporate TLS-inspecting proxy is usually trusted only through one of those)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    for var, kind in (("SSL_CERT_FILE", "cafile"), ("SSL_CERT_DIR", "capath"),
                      ("NODE_EXTRA_CA_CERTS", "cafile")):
        value = os.environ.get(var)
        if value:
            try:
                ctx.load_verify_locations(**{kind: value})
            except (OSError, ssl.SSLError):
                pass    # an unreadable file is left to preflight() to surface
    return ctx


def _parse_proxy(proxy: str | None, host: str) -> tuple[str, int, dict] | None:
    """``(host, port, CONNECT headers)`` of ``proxy`` for reaching ``host``, or None to go direct
    (no proxy, or the launcher's own ``NO_PROXY`` exempts ``host``).

    The proxy is passed in, never read from ``HTTPS_PROXY``: in the child that names this proxy."""
    if not proxy or urllib.request.proxy_bypass_environment(host, urllib.request.getproxies_environment()):
        return None
    p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if not p.hostname:
        return None
    headers = {}
    if p.username:
        cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    return p.hostname, p.port or 8080, headers


def _connect_via(proxy: tuple[str, int, dict] | None, host: str, port: int, timeout: float) -> socket.socket:
    """A TCP connection to ``host:port``: direct, or a CONNECT tunnel through ``proxy``."""
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)
    sock = socket.create_connection((proxy[0], proxy[1]), timeout=timeout)
    try:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"    # an IPv6 literal
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        lines += [f"{k}: {v}" for k, v in proxy[2].items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        head = b""
        while b"\r\n\r\n" not in head:
            data = sock.recv(1)    # byte by byte: what follows the reply belongs to the tunnel
            if not data or len(head) > 65536:
                raise OSError("upstream proxy closed during CONNECT")
            head += data
        first = head.split(b"\r\n", 1)[0]
        status = first.split()
        if len(status) < 2 or status[1] != b"200":
            raise OSError(f"upstream proxy refused CONNECT: {first[:100]!r}")
        return sock
    except BaseException:
        sock.close()
        raise


def _dropped(conn) -> bool:
    """Whether an idle keep-alive connection was closed by the far end (it reads as ready: EOF)."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


class _Upstream:
    """Keep-alive connections to one origin, through a CONNECT tunnel when a proxy is configured."""

    def __init__(self, url: str, timeout: float = UPSTREAM_TIMEOUT, proxy: str | None = None):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"upstream must be an http(s) URL, got {url!r}")
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.netloc = u.netloc.rpartition("@")[2]
        self.base = u.path.rstrip("/")
        self.timeout = timeout
        self.ssl = _tls_context() if self.https else None
        self.proxy = proxy
        self.tunnel = _parse_proxy(proxy, self.host)
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
        """A connection and whether it was reused from the pool. Pooled connections the upstream has
        already closed are discarded here, before anything is sent on them."""
        while not fresh:
            with self._lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None:
                break
            if not _dropped(conn):
                return conn, True
            conn.close()
        return self._new(), False

    def release(self, conn) -> None:
        with self._lock:
            self._idle.append(conn)

    def open_tunnel(self, host: str, port: int) -> socket.socket:
        """A raw connection to any ``host:port`` for a blind tunnel, through the same proxy."""
        return _connect_via(_parse_proxy(self.proxy, host), host, port, self.timeout)

    def open_upstream(self) -> socket.socket:
        """A fresh raw connection to the upstream itself, TLS-verified when https (an upgrade)."""
        sock = _connect_via(self.tunnel, self.host, self.port, self.timeout)
        if not self.https:
            return sock
        try:
            return self.ssl.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise

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


def _oauth_client(headers) -> bool:
    """Whether the request comes from Claude Code's own OAuth login: a bearer token, no API key, and
    an ``anthropic-beta`` that opts into OAuth (a bearer alone could be any gateway client)."""
    auth = _header(headers, "authorization") or ""
    items = headers.items() if hasattr(headers, "items") else headers
    beta = ",".join(v for k, v in items if k.lower() == "anthropic-beta").lower()
    return (auth[:7].lower() == "bearer " and _header(headers, "x-api-key") is None
            and "oauth-" in beta)


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


def _upgrading(headers) -> bool:
    """Whether the request asks to switch protocols (``Connection: upgrade``)."""
    items = headers.items() if hasattr(headers, "items") else headers
    return any(t.strip().lower() == "upgrade"
               for k, v in items if k.lower() == "connection" for t in v.split(","))


def _upgrade_head(method: str, url: str, headers, netloc: str) -> bytes:
    """The client's request head as it came, minus its proxy headers; Connection and Upgrade stay."""
    items = headers.items() if hasattr(headers, "items") else headers
    lines = [f"{method} {url} HTTP/1.1", f"Host: {netloc}"]
    lines += [f"{k}: {v}" for k, v in items if not k.lower().startswith("proxy-") and k.lower() != "host"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class _Unbuffered:
    """Hands ``http.client.HTTPResponse`` an unbuffered reader: it reads the head byte by byte, so
    nothing past it is read ahead (those bytes belong to the upgraded stream)."""

    def __init__(self, sock):
        self.sock = sock

    def makefile(self, mode, *_args, **_kwargs):
        return self.sock.makefile(mode, buffering=0)


# --- the proxy ---------------------------------------------------------------------------------

class SeatProxy:
    def __init__(self, decide: Callable[[Hit], Seat | None], *, upstream="https://api.anthropic.com",
                 ca=None, upstream_proxy: str | None = None,
                 on_tls_reject: Callable[[str], None] | None = None,
                 decide_timeout=10.0, log: Callable[..., None] = _noop):
        self._decide = decide
        self._decide_timeout = decide_timeout
        self._log_fn = log
        self._transport = _Upstream(upstream, proxy=upstream_proxy)
        self._ca = ca                      # a SessionCA: server_context() for the intercepted host
        self._on_tls_reject = on_tls_reject
        self._tls_rejected = False
        self._intercepts_ok = 0
        self._eofs: list[float] = []      # when recent handshakes ended in a hang-up (no alert)
        self._upstream_failures = 0
        self._upstream_successes = 0
        self._secret = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._decide_lock = threading.Lock()
        self._stalled_until = 0.0         # after a decide timeout, the rest of a burst asks nobody
        self._seat = Seat("", None)
        self._known: set[str] = set()     # every token ever bound, for redacting exception texts
        self._active = 0
        self._messages = 0
        self._last = time.monotonic()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> str:
        """Listen on 127.0.0.1 (ephemeral port) and return the proxy URL for the child's
        ``HTTPS_PROXY``: ``http://acctsw:<secret>@127.0.0.1:<port>``.

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
        return f"http://{PROXY_USER}:{self._secret}@127.0.0.1:{self._server.server_address[1]}"

    @property
    def _authority(self) -> str:
        """``host:port`` of the one origin whose CONNECTs are decrypted."""
        return f"{self._transport.host}:{self._transport.port}"

    def _proxy_auth_ok(self, value: str | None) -> bool:
        scheme, _, b64 = (value or "").strip().partition(" ")
        try:
            given = base64.b64decode(b64.strip(), validate=True)
        except ValueError:
            return False
        return scheme.lower() == "basic" and secrets.compare_digest(
            given, f"{PROXY_USER}:{self._secret}".encode())

    def _intercepts(self, host: str, port: int) -> bool:
        return self._ca is not None and f"{host}:{port}" == self._authority

    def _ca_expired(self) -> bool:
        try:
            return bool(self._ca.expired())
        except Exception:   # a CA without an end date, or one that can't say: not expired
            return False

    def _handshake_failed(self, reason: str, eof: bool) -> None:
        if reason.rpartition("ALERT_")[2] in CERT_ALERTS:
            return self._tls_reject(reason)
        if eof and self._ca_expired():
            # Bun hangs up instead of sending an alert, and does so even after earlier handshakes
            # worked. With our certificate past its end, that hang-up is the date, not a stray client.
            return self._tls_reject("certificate expired")
        # Plain HTTP or a protocol mismatch says nothing about the CA; neither does one hang-up, or
        # any hang-up once a client has completed a handshake (the CA is trusted).
        now = time.monotonic()
        with self._lock:
            if eof and self._intercepts_ok == 0:
                self._eofs = [t for t in self._eofs if now - t <= EOF_REJECT_WINDOW] + [now]
            streak = len(self._eofs)
        if streak >= EOF_REJECT_COUNT:
            self._tls_reject(f"eof x{EOF_REJECT_COUNT}")
        self._log("tls_handshake_failed", reason=reason)

    def _tls_reject(self, reason: str) -> None:
        with self._lock:
            first, self._tls_rejected = not self._tls_rejected, True
        self._log("tls_reject", reason=reason)
        if first and self._on_tls_reject is not None:
            try:
                self._on_tls_reject(reason)
            except Exception as e:
                self._log("tls_reject_callback_error", error=self._redact(f"{e.__class__.__name__}: {e}"))

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

    def messages_seen(self) -> int:
        """Client ``POST /v1/messages`` requests with Claude Code's own kind of login (bearer, no
        API key) received so far, any status, injected or not: proof that Claude Code itself
        talks to the API through this proxy. An API-key script in the child does not count."""
        with self._lock:
            return self._messages

    def intercepts_ok(self) -> int:
        """Intercepted CONNECTs whose client completed the TLS handshake with the session CA."""
        with self._lock:
            return self._intercepts_ok

    def upstream_failures(self) -> int:
        """Upstream connect, proxy or TLS failures in a row, since the upstream last answered."""
        with self._lock:
            return self._upstream_failures

    def upstream_successes(self) -> int:
        """Upstream answers received so far, any HTTP status: proof the upstream path works."""
        with self._lock:
            return self._upstream_successes

    def _upstream_result(self, ok: bool) -> None:
        with self._lock:
            self._upstream_failures = 0 if ok else self._upstream_failures + 1
            self._upstream_successes += 1 if ok else 0

    def preflight(self, timeout: float = 5.0) -> str | None:
        """Connect to the upstream the way real requests do (tunnel, TLS trust) and send a tokenless
        ``HEAD /``. None when that works, else a short reason. Any HTTP status counts as working."""
        conn, _ = self._transport.acquire(fresh=True)
        conn.timeout = timeout
        try:
            conn.request("HEAD", self._transport.base + "/", headers={"Host": self._transport.netloc})
            conn.getresponse().read()
            return None
        except ssl.SSLCertVerificationError as e:
            return f"tls: {getattr(e, 'verify_message', None) or e}"[:200]
        except ssl.SSLError as e:
            return f"tls: {getattr(e, 'reason', None) or e}"[:200]
        except (OSError, http.client.HTTPException) as e:
            return f"unreachable: {e.__class__.__name__}: {e}"[:200]
        finally:
            conn.close()

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

    def handle(self, method: str, target: str, headers, body: bytes, wfile, *,
               authority: str | None = None) -> bool:
        """Serve one request; the response goes to ``wfile``. Returns whether the client connection
        may be kept alive. Never raises for an upstream or handler fault: those answer 502.

        ``authority`` is the ``host:port`` of the CONNECT a decrypted request arrived on; None for a
        plain request, which must then carry the secret path."""
        reply = _Reply(wfile, head_only=method == "HEAD")
        with self._lock:
            self._active += 1
        try:
            return self._handle(method, target, headers, body, reply, authority)
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

    def _handle(self, method: str, target: str, headers, body: bytes, reply: _Reply,
                authority: str | None) -> bool:
        if authority is not None:
            if authority != self._authority:
                # Decrypted traffic for any other origin is never forwarded, let alone injected.
                self._log("misdirected", authority=authority, method=method)
                reply.error(421, "invalid_request_error", "seat proxy: misdirected request")
                return False
            path = target if target.startswith("/") else None
        else:
            path = self._route(target)
        if path is None:
            self._log("forbidden", method=method)
            reply.error(403, "permission_error", "seat proxy: unknown path")
            return True
        endpoint = path.split("?")[0]
        oauth = _oauth_client(headers)
        if method == "POST" and endpoint == "/v1/messages" and oauth:
            with self._lock:
                self._messages += 1
        inject = method == "POST" and endpoint in INJECT_PATHS and oauth
        seat = self.bound() if inject else None    # None: the client's headers go out untouched
        tried = [seat] if seat is not None else []
        length = len(body) if body or method in ("POST", "PUT", "PATCH") else None
        t0 = time.monotonic()
        fresh = False
        while True:
            token = seat.token if seat is not None else None
            conn, reused = self._transport.acquire(fresh)
            sent = False
            try:
                conn.request(method, self._transport.base + path, body=body or None,
                             headers=_forward_headers(headers, token, self._transport.netloc, length))
                sent = True
                resp = conn.getresponse()
                self._upstream_result(True)
                kind = classify(resp.status, resp.headers) if seat is not None else None
                if kind == "auth" and seat.token is None:
                    kind = None    # the client's own login: it re-reads its own credentials
                if kind is None and seat is not None and seat.token is not None \
                        and resp.status in MODEL_STATUSES:
                    kind = "model"    # a candidate: the body decides (never on the client's own login)
                if kind and not reply.started and len(tried) < MAX_TRIES:
                    data = resp.read()
                    self._done(conn, resp)
                    if kind == "model" and not _model_rejection(_decoded(resp.headers, data)):
                        nxt = None
                    else:
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
                if reused and not sent and not fresh:
                    # A pooled socket that broke while the request was going out: upstream cannot
                    # have acted on it. Once it is sent, a resend could run it twice.
                    fresh = True
                    continue
                if not sent:
                    self._upstream_result(False)    # never reached upstream: connect, proxy or TLS
                self._log("upstream_error", path=path, error=error)
                reply.error(502, "api_error", "seat proxy: upstream unreachable")
                return True

    def upgrade(self, method: str, target: str, headers, client, wfile) -> None:
        """Relay a ``Connection: upgrade`` request from a decrypted CONNECT on a fresh upstream
        connection; after a 101, splice ``client`` and upstream until either ends. Any other answer
        is relayed and the connection closes. The client's headers go out as they came: an upgrade
        never carries a seat token and is not a message."""
        reply = _Reply(wfile)
        if not target.startswith("/"):
            self._log("forbidden", method=method)
            return reply.error(403, "permission_error", "seat proxy: unknown path")
        path = target.split("?")[0]
        try:
            far = self._transport.open_upstream()
        except OSError as e:
            self._upstream_result(False)
            self._log("upstream_error", path=path, error=f"{e.__class__.__name__}: {e}"[:200])
            return reply.error(502, "api_error", "seat proxy: upstream unreachable")
        with far:
            try:
                far.sendall(_upgrade_head(method, self._transport.base + target, headers,
                                          self._transport.netloc))
                resp = http.client.HTTPResponse(_Unbuffered(far), method=method)
                resp.begin()
            except (OSError, http.client.HTTPException) as e:
                self._log("upstream_error", path=path, error=f"{e.__class__.__name__}: {e}"[:200])
                return reply.error(502, "api_error", "seat proxy: upstream unreachable")
            self._upstream_result(True)
            self._log("upgrade", path=path, status=resp.status)
            if resp.status != 101:
                resp.fp = io.BufferedReader(resp.fp)    # nothing follows this answer: buffer away
                return self._relay(resp, reply)
            reply.head(resp.status, resp.reason, resp.getheaders())
            try:
                _pump(client, far)
            except OSError:
                pass

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
                  _error_detail(_decoded(headers, data)),
                  _header(headers, "anthropic-ratelimit-unified-representative-claim"))
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
    request_queue_size = 128    # a client opening many connections at once is not refused
    proxy: SeatProxy

    def handle_error(self, request, client_address):
        # The default prints a traceback to stderr, which is the user's terminal.
        self.proxy._log("connection_error")


def _pending(sock) -> bool:
    return isinstance(sock, ssl.SSLSocket) and sock.pending() > 0


def _pump(a: socket.socket, b: socket.socket, idle: float | None = None) -> None:
    """Copy bytes both ways until both directions have ended (or nothing moves for ``idle``).

    Every send is bounded by ``TUNNEL_SEND_TIMEOUT``: a peer that stops reading ends the tunnel
    instead of holding its thread forever. A TLS socket can hold decrypted bytes the kernel no longer
    reports as readable (``pending()``); those are copied before waiting in ``select``. TLS has no
    half-close here, so the end of either side of a TLS splice ends both."""
    for sock in (a, b):
        sock.settimeout(TUNNEL_SEND_TIMEOUT)
    peer = {a: b, b: a}
    open_ = [a, b]
    while open_:
        ready = [s for s in open_ if _pending(s)]
        if not ready:
            ready, _, _ = select.select(open_, [], [], idle)
            if not ready:
                return
        for src in ready:
            data = src.recv(CHUNK)
            if data:
                peer[src].sendall(data)
                continue
            if isinstance(a, ssl.SSLSocket) or isinstance(b, ssl.SSLSocket):
                return
            open_.remove(src)
            try:
                peer[src].shutdown(socket.SHUT_WR)
            except OSError:
                pass


def _make_handler(proxy: SeatProxy, authority: str | None = None):
    """The request handler: the listener's own (``authority`` None: CONNECT plus secret-path
    requests) or one for the decrypted requests on an intercepted CONNECT to ``authority``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # A two-word request line (e.g. "CONNECT  HTTP/1.1", empty authority) is HTTP/0.9 to the
        # stdlib on 3.11, and 0.9 error replies carry no status line. This proxy never speaks 0.9.
        default_request_version = "HTTP/1.1"
        timeout = CLIENT_IDLE_TIMEOUT
        # Unbuffered: nothing past a head may be read ahead. After a CONNECT those bytes belong to
        # the tunnel or the TLS handshake, after an upgrade to the spliced stream.
        rbufsize = 0

        def log_message(self, *_args):
            pass

        def _serve(self):
            if authority is not None and _upgrading(self.headers):
                self.close_connection = True    # the socket now belongs to the upgraded stream
                return proxy.upgrade(self.command, self.path, self.headers, self.connection, self.wfile)
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
            if not proxy.handle(self.command, self.path, self.headers, body, self.wfile,
                                authority=authority):
                self.close_connection = True

        def _read(self, n: int) -> bytes:
            parts = []
            while n > 0:
                data = self.rfile.read(min(n, CHUNK))    # unbuffered: a read allocates its size
                if not data:
                    raise ValueError("request body cut short")
                parts.append(data)
                n -= len(data)
            return b"".join(parts)

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
                    parts.append(self._read(size))
                    self.rfile.readline(1024)
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                return None
            return self._read(n) if n > 0 else b""

        def _answer(self, status: int, reason: str, extra: str = "") -> None:
            self.wfile.write(f"HTTP/1.1 {status} {reason}\r\n{extra}content-length: 0\r\n\r\n".encode())
            self.wfile.flush()

        def _established(self) -> None:
            # No content-length: a 200 to CONNECT has no body, what follows is the tunnel.
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()

        def do_CONNECT(self):
            self.close_connection = True    # the socket now belongs to the tunnel
            if authority is not None:
                return self._answer(405, "Method Not Allowed")
            if not proxy._proxy_auth_ok(self.headers.get("proxy-authorization")):
                proxy._log("proxy_auth_failed")
                return self._answer(407, "Proxy Authentication Required",
                                    f'proxy-authenticate: Basic realm="{PROXY_USER}"\r\nconnection: close\r\n')
            host, _, port = self.path.rpartition(":")
            host = host.strip("[]").lower()
            if not host or not port.isdigit():
                return self._answer(400, "Bad Request")
            if proxy._intercepts(host, int(port)):
                return self._intercept(f"{host}:{port}")
            self._tunnel(host, int(port))

        def _tunnel(self, host: str, port: int) -> None:
            try:
                far = proxy._transport.open_tunnel(host, port)
            except OSError as e:
                proxy._log("tunnel_error", host=host, port=port, error=f"{e.__class__.__name__}: {e}"[:200])
                return self._answer(502, "Bad Gateway")
            proxy._log("tunnel", host=host, port=port)
            try:
                self._established()
                _pump(self.connection, far)    # no idle cut: a quiet tunnel may be a live stream
            except OSError:
                pass
            finally:
                far.close()

        def _intercept(self, target: str) -> None:
            self._established()
            try:
                tls = proxy._ca.server_context().wrap_socket(self.connection, server_side=True)
            except ssl.SSLError as e:
                reason = f"{getattr(e, 'reason', None) or e}"[:200]
                eof = isinstance(e, ssl.SSLEOFError) or reason == "UNEXPECTED_EOF_WHILE_READING"
                return proxy._handshake_failed(reason, eof)
            except ConnectionResetError:
                return proxy._handshake_failed("connection reset", True)    # a hang-up, like an EOF
            except OSError:
                return
            with proxy._lock:
                proxy._intercepts_ok += 1
            proxy._log("intercept", authority=target)
            try:
                _make_handler(proxy, target)(tls, self.client_address, self.server)
            except OSError:
                pass
            finally:
                tls.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

    return Handler
