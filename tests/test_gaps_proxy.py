"""Gaps found by mutation testing of ``seatproxy`` and ``sessionca``.

Each test pins behaviour a surviving mutant could change: the proxy's own error and CONNECT paths,
the upstream-proxy handshake, request framing, the replay bookkeeping, and the session CA's key
hygiene. Everything runs on socketpairs and in-memory transports, like ``test_seatproxy.py``.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import stat
import threading
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from acctsw import seatproxy as SP
from acctsw import sessionca as SC
from acctsw.seatproxy import Seat, SeatProxy, tok
from acctsw.sessionca import SessionCA, SessionCAError

import test_seatproxy as TS
from test_seatproxy import A, B, C, CLIENT_TOKEN, MSG, Harness, PairServer, Wire, parse, rules

OAUTH = [("Authorization", f"Bearer {CLIENT_TOKEN}"), ("anthropic-beta", "oauth-2025-04-20")]


def _within(fn, seconds: float = 5.0):
    """Run ``fn`` on a thread: its result or exception, or a failure when it hangs."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:    # noqa: BLE001 - handed back to the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "hung"
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --- small helpers -----------------------------------------------------------------------------

def test_seat_and_hit_are_immutable():
    with pytest.raises(FrozenInstanceError):
        A.email = "x"
    with pytest.raises(FrozenInstanceError):
        SP.Hit("limit", "a", None, None, None).kind = "auth"


def test_label_falls_back_to_client():
    assert SP._label(Seat("", None)) == "client"
    assert SP._label(A) == A.email


def test_error_detail_is_truncated_to_200():
    assert SP._error_detail(b'{"error": {"message": "' + b"x" * 300 + b'"}}') == "x" * 200


def test_forward_headers_without_a_length_sends_no_content_length():
    out = SP._forward_headers([("Accept", "*/*")], None, "api.anthropic.com", None)
    assert "Content-Length" not in out
    assert SP._forward_headers([("Accept", "*/*")], None, "h", 0)["Content-Length"] == "0"


# --- reply framing -----------------------------------------------------------------------------

def test_reply_defaults_to_writing_bodies():
    wire = Wire()
    r = SP._Reply(wire)
    r.chunked = True
    r.chunk(b"abc")
    assert wire.getvalue() == b"3\r\nabc\r\n"


def test_reply_ignores_empty_chunks():
    wire = Wire()
    r = SP._Reply(wire)
    r.chunked = True
    r.chunk(b"")
    assert wire.getvalue() == b""    # an empty chunk would read as the terminator


def test_reply_head_uses_the_standard_reason_and_flushes():
    wire = Wire()
    SP._Reply(wire).head(200, "", [("x", "y")])
    assert wire.getvalue().startswith(b"HTTP/1.1 200 OK\r\n")
    assert wire.flushed == len(wire.getvalue())


def test_reply_end_flushes_and_terminates_chunked_bodies():
    wire = Wire()
    r = SP._Reply(wire)
    r.chunked = True
    r.whole(200, "OK", [], b"abc")
    assert wire.getvalue().endswith(b"3\r\nabc\r\n0\r\n\r\n")
    assert wire.flushed == len(wire.getvalue())


class _Resp:
    """The few attributes of an upstream response that ``_relay`` reads."""

    def __init__(self, status, headers=(), body=b"", reason=""):
        self.status, self.reason = status, reason
        self._h = list(headers)
        self._body = [body] if body else []

    def getheaders(self):
        return self._h

    def getheader(self, name, default=None):
        return next((v for k, v in self._h if k.lower() == name), default)

    def read1(self, _n):
        return self._body.pop(0) if self._body else b""


@pytest.mark.parametrize("status", [100, 101, 199, 204, 304])
def test_relay_never_chunks_bodyless_statuses(status):
    h = Harness()
    wire = Wire()
    h.proxy._relay(_Resp(status), SP._Reply(wire))
    assert b"transfer-encoding" not in wire.getvalue().lower()
    assert not wire.getvalue().endswith(b"0\r\n\r\n")


def test_relay_chunks_a_bodied_status_without_length():
    h = Harness()
    wire = Wire()
    h.proxy._relay(_Resp(200, body=b"abc"), SP._Reply(wire))
    assert wire.getvalue().endswith(b"3\r\nabc\r\n0\r\n\r\n")
    assert b"transfer-encoding: chunked" in wire.getvalue()


def test_chunked_upstream_answer_has_no_content_length():
    h = Harness()
    h.proxy.bind(B)
    resp, _ = h.send(body=TS.STREAM)
    assert resp.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in resp.headers


# --- proxy URL and upstream construction -------------------------------------------------------

def test_parse_proxy_without_a_host_goes_direct(monkeypatch):
    for var in TS.PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert SP._parse_proxy("http://:8080", "api.anthropic.com") is None


def test_parse_proxy_defaults_to_port_8080(monkeypatch):
    for var in TS.PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert SP._parse_proxy("proxy.corp", "api.anthropic.com") == ("proxy.corp", 8080, {})


@pytest.mark.parametrize("url", ["ftp://example.com", "http://", "api.anthropic.com", "https:///v1"])
def test_upstream_rejects_non_http_urls(url):
    with pytest.raises(ValueError):
        SP._Upstream(url)


def test_upstream_default_ports():
    assert SP._Upstream("http://example.com").port == 80
    assert SP._Upstream("https://example.com").port == 443


def test_dropped_treats_a_closed_socket_as_dropped():
    a, b = socket.socketpair()
    try:
        assert SP._dropped(types.SimpleNamespace(sock=a)) is False     # idle and open
        b.close()
        assert SP._dropped(types.SimpleNamespace(sock=a)) is True      # EOF reads as ready
    finally:
        a.close()
    assert SP._dropped(types.SimpleNamespace(sock=a)) is True          # select() on a closed fd
    assert SP._dropped(types.SimpleNamespace(sock=None)) is False


# --- the upstream-proxy CONNECT handshake ------------------------------------------------------

class UpstreamProxy:
    """What ``socket.create_connection`` reaches: sends ``reply`` after the CONNECT head, then
    ``tail`` or silence; remembers the socket the caller got."""

    def __init__(self, monkeypatch, reply: bytes, *, hold_open: bool = True):
        self.reply, self.hold_open = reply, hold_open
        self.client: socket.socket | None = None
        self.peer: socket.socket | None = None
        monkeypatch.setattr(socket, "create_connection", self.create_connection)

    def create_connection(self, address, *args, **kwargs):
        self.client, self.peer = socket.socketpair()
        self.client.settimeout(5)
        threading.Thread(target=self._serve, args=(self.peer,), daemon=True).start()
        return self.client

    def _serve(self, sock):
        head = b""
        while b"\r\n\r\n" not in head:
            data = sock.recv(1)
            if not data:
                return
            head += data
        sock.sendall(self.reply)
        if not self.hold_open:
            sock.close()


def _via(host="api.anthropic.com", port=443):
    return SP._connect_via(("proxy.corp", 3128, {}), host, port, 5)


def test_connect_via_leaves_the_bytes_after_the_reply_in_the_tunnel(monkeypatch):
    up = UpstreamProxy(monkeypatch, b"HTTP/1.1 200 OK\r\n\r\nEXTRA")    # odd length: no pair boundary
    sock = _via()
    assert sock is up.client
    assert sock.recv(5) == b"EXTRA"


def test_connect_via_accepts_a_head_of_exactly_the_size_limit(monkeypatch):
    pad = b"X: " + b"a" * (65537 - len(b"HTTP/1.1 200 OK\r\n") - len(b"X: ") - 4)
    reply = b"HTTP/1.1 200 OK\r\n" + pad + b"\r\n\r\n"
    assert len(reply) == 65537
    UpstreamProxy(monkeypatch, reply)
    assert _within(_via) is not None


def test_connect_via_rejects_an_endless_head(monkeypatch):
    UpstreamProxy(monkeypatch, b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000)
    with pytest.raises(OSError, match="closed during CONNECT"):
        _within(_via)


def test_connect_via_reports_a_connection_closed_mid_reply(monkeypatch):
    UpstreamProxy(monkeypatch, b"HTTP/1.1 200", hold_open=False)
    with pytest.raises(OSError, match="closed during CONNECT"):
        _within(_via)


@pytest.mark.parametrize("reply", [b"HTTP/1.1\r\n\r\n", b"\r\n\r\n"])
def test_connect_via_rejects_a_malformed_status_line(monkeypatch, reply):
    UpstreamProxy(monkeypatch, reply)
    with pytest.raises(OSError, match="refused CONNECT"):
        _within(_via)


def test_connect_via_refusal_quotes_only_the_status_line_and_closes(monkeypatch):
    line = b"HTTP/1.1 407 " + b"Nope" * 50
    up = UpstreamProxy(monkeypatch, line + b"\r\nX: secret-in-a-header\r\n\r\n")
    with pytest.raises(OSError) as e:
        _within(_via)
    assert str(e.value) == f"upstream proxy refused CONNECT: {line[:100]!r}"
    assert up.client.fileno() == -1


def test_connect_via_sends_the_target_and_credentials(monkeypatch):
    seen = []

    class Spy(UpstreamProxy):
        def _serve(self, sock):
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(1)
            seen.append(head)
            sock.sendall(b"HTTP/1.1 200 OK\r\n\r\n")

    Spy(monkeypatch, b"")
    SP._connect_via(("proxy.corp", 3128, {"Proxy-Authorization": "Basic Zm9v"}), "h.example", 8443, 5)
    assert seen == [b"CONNECT h.example:8443 HTTP/1.1\r\nHost: h.example:8443\r\n"
                    b"Proxy-Authorization: Basic Zm9v\r\n\r\n"]


# --- proxy authentication ----------------------------------------------------------------------

def test_proxy_auth_rejects_base64_with_stray_characters():
    h = Harness()
    good = base64.b64encode(f"{SP.PROXY_USER}:{h.proxy._secret}".encode()).decode()
    assert h.proxy._proxy_auth_ok("Basic " + good)
    assert not h.proxy._proxy_auth_ok("Basic " + good[:4] + "!" + good[4:])
    assert not h.proxy._proxy_auth_ok("Basic " + good[:4] + "\n" + good[4:])


def test_bind_logs_only_a_change():
    h = Harness()
    h.proxy.bind(A)
    h.proxy.bind(A)
    h.proxy.bind(B)
    assert [f["seat"] for e, f in h.logs if e == "bind"] == [A.email, B.email]


# --- handler faults ----------------------------------------------------------------------------

def test_handler_exception_answers_502_and_redacts_the_clients_credentials(monkeypatch):
    h = Harness()
    auth = f"Bearer {CLIENT_TOKEN}"

    def boom(*_a, **_k):
        raise RuntimeError(f"cannot use {auth} here")

    monkeypatch.setattr(h.t, "acquire", boom)
    resp, keep = h.send()
    assert resp.status == 502 and keep is False
    errors = [f["error"] for e, f in h.logs if e == "proxy_error"]
    assert len(errors) == 1
    assert CLIENT_TOKEN not in errors[0] and f"<{tok(auth)}>" in errors[0]


def test_handler_exception_after_the_head_does_not_splice_a_second_reply(monkeypatch):
    h = Harness()
    h.proxy.bind(B)

    def half(resp, reply):
        reply.head(200, "OK", [("content-length", "0")])
        raise RuntimeError("late failure")

    monkeypatch.setattr(h.proxy, "_relay", half)
    resp, keep = h.send()
    assert resp.status == 200 and keep is False
    assert b"502" not in resp.raw and b"api_error" not in resp.raw


def test_misdirected_and_forbidden_requests_are_logged_and_closed():
    h = Harness()
    wire = Wire()
    keep = h.proxy.handle("POST", "/v1/messages", OAUTH, MSG, wire, authority="evil.example:443")
    assert parse(wire.getvalue()).status == 421 and keep is False
    resp, keep = h.send(raw_target="/nope")
    assert resp.status == 403 and keep is True
    assert [e for e, _ in h.logs if e in ("misdirected", "forbidden")] == ["misdirected", "forbidden"]


# --- request framing towards upstream ----------------------------------------------------------

def _sent_length(h, **kw):
    h.send(**kw)
    return h.t.sent[-1]["headers"].get("Content-Length")


def test_content_length_follows_the_method_and_body():
    h = Harness()
    h.proxy.bind(B)
    assert _sent_length(h, body=b"") == "0"                                     # POST, empty
    assert _sent_length(h, method="PUT", path="/v1/files", body=b"") == "0"
    assert _sent_length(h, method="GET", path="/v1/models", body=b"") is None
    assert _sent_length(h, method="GET", path="/v1/models", body=b"xy") == "2"


# --- replay bookkeeping ------------------------------------------------------------------------

def test_replayed_answer_returns_its_connection_to_the_pool():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    h.answers.append(B)
    resp, _ = h.send()
    assert resp.status == 200
    assert h.t.acquired == [False, True]    # the first answer's connection was released and reused


def test_a_seat_already_tried_is_not_asked_for_again():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429, b=429)
    h.answers += [B, B]
    resp, _ = h.send()
    assert resp.status == 429
    assert TS.bearers(h) == [tok(A.token), tok(B.token)]    # B is not retried


def test_a_passed_through_limit_is_logged_as_a_request():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    resp, _ = h.send()
    assert resp.status == 429
    requests = [f for e, f in h.logs if e == "request"]
    assert len(requests) == 1 and requests[0]["status"] == 429


def test_next_seat_asks_when_the_follow_up_seat_was_already_tried():
    h = Harness()
    h.proxy.bind(A)                 # bound moved (concurrently) back to A ...
    h.answers.append(C)
    hdrs = {"anthropic-ratelimit-unified-status": "rejected"}
    nxt = h.proxy._next_seat("limit", B, hdrs, b"", [A, B])    # ... which this request tried already
    assert nxt == C


def test_a_broken_pooled_connection_is_retried_on_a_fresh_one():
    h = Harness()
    h.proxy.bind(B)
    h.t.idle += [TS.FakeConn(h.t, True), TS.FakeConn(h.t, True)]
    h.t.fail.append(ConnectionResetError("stale"))
    resp, _ = h.send()
    assert resp.status == 200
    assert [s["reused"] for s in h.t.sent] == [True, False]
    assert len(h.t.idle) == 2    # the second pooled connection was left alone


def test_a_failed_connection_is_closed():
    h = Harness()
    h.proxy.bind(B)
    conns = []
    real = h.t.acquire

    def spy(fresh=False):
        conn, reused = real(fresh)
        conns.append(conn)
        return conn, reused

    h.t.acquire = spy
    h.t.fail.append(ConnectionResetError("down"))
    resp, _ = h.send()
    assert resp.status == 502
    assert conns[0].closed


def test_a_closing_upstream_connection_is_not_pooled(monkeypatch):
    real = TS.FakeConn.getresponse

    def closing(self):
        r = real(self)
        r.will_close = True
        return r

    monkeypatch.setattr(TS.FakeConn, "getresponse", closing)
    h = Harness()
    h.proxy.bind(B)
    h.send()
    assert h.t.idle == []


# --- _pump -------------------------------------------------------------------------------------

def test_pump_returns_when_nothing_moves():
    a1, a2 = socket.socketpair()
    b1, b2 = socket.socketpair()
    try:
        _within(lambda: SP._pump(a2, b1, 0.05))
    finally:
        for s in (a1, a2, b1, b2):
            s.close()


def test_pump_forwards_half_closes_and_ends_when_both_sides_are_done():
    c_user, c_proxy = socket.socketpair()
    f_proxy, f_user = socket.socketpair()
    for s in (c_user, f_user):
        s.settimeout(5)
    t = threading.Thread(target=SP._pump, args=(c_proxy, f_proxy, 30), daemon=True)
    t.start()
    try:
        c_user.sendall(b"hello")
        c_user.shutdown(socket.SHUT_WR)
        assert f_user.recv(5) == b"hello"
        assert f_user.recv(1) == b""        # the half-close reached the far end ...
        f_user.sendall(b"bye")              # ... which can still answer
        f_user.shutdown(socket.SHUT_WR)
        assert c_user.recv(3) == b"bye"
        assert c_user.recv(1) == b""
        t.join(5)
        assert not t.is_alive()
    finally:
        for s in (c_user, c_proxy, f_proxy, f_user):
            s.close()


# --- listener: CONNECT edge cases --------------------------------------------------------------

def _listener(proxy, authority=None):
    sock = PairServer(SP._make_handler(proxy, authority)).connect()._sock
    sock.settimeout(5)
    return sock


def _status(sock) -> int:
    head = b""
    while b"\r\n\r\n" not in head:
        data = sock.recv(1)
        if not data:
            break
        head += data
    return int(head.split()[1]) if head else 0


@pytest.mark.parametrize("target", ["nohost", "example.com:", "example.com:abc", ":443", ""])
def test_connect_with_a_bad_authority_is_a_400(monkeypatch, target):
    proxy = SeatProxy(lambda hit: None)
    far = TS.FarEnd(monkeypatch)
    sock, status, _ = TS._connect(proxy, target)
    sock.close()
    assert status == 400
    assert far.addresses == []


def test_failed_proxy_auth_is_logged():
    logs = []
    proxy = SeatProxy(lambda hit: None, log=lambda e, **f: logs.append(e))
    sock, status, _ = TS._connect(proxy, "example.com:443", auth="Basic eA==")
    sock.close()
    assert status == 407 and "proxy_auth_failed" in logs


def test_connect_inside_a_decrypted_stream_is_refused():
    proxy = SeatProxy(lambda hit: None)
    sock = _listener(proxy, "api.anthropic.com:443")
    with sock:
        sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        assert _status(sock) == 405
        assert sock.recv(1) == b""


def test_bytes_sent_with_the_connect_head_reach_the_tunnel(monkeypatch):
    proxy = SeatProxy(lambda hit: None)
    TS.FarEnd(monkeypatch)
    sock = _listener(proxy)
    with sock:
        auth = TS._proxy_auth(proxy)
        sock.sendall(f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode()
                     + b"early")
        assert _status(sock) == 200
        assert sock.recv(5) == b"early"    # echoed: the listener did not read ahead into its buffer


# --- listener: request bodies ------------------------------------------------------------------

def _request(proxy, lines: list[str], body: bytes = b"", *, shutdown: bool = False):
    sock = _listener(proxy)
    head = [f"POST /{proxy._secret}/v1/messages HTTP/1.1", "Host: x", *lines]
    sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    if shutdown:
        sock.shutdown(socket.SHUT_WR)
    return sock


def _read_response(sock):
    resp = http.client.HTTPResponse(sock, method="POST")
    resp.begin()
    return resp, resp.read()


OAUTH_LINES = [f"Authorization: Bearer {CLIENT_TOKEN}", "anthropic-beta: oauth-2025-04-20"]


def test_content_length_body_is_forwarded_whole():
    h = Harness()
    h.proxy.bind(B)
    sock = _request(h.proxy, [*OAUTH_LINES, f"Content-Length: {len(MSG)}"], MSG)
    with sock:
        resp, _ = _read_response(sock)
    assert resp.status == 200 and h.t.sent[0]["body"] == MSG


def test_chunked_body_with_extensions_and_trailers_keeps_the_connection_usable():
    h = Harness()
    h.proxy.bind(B)
    half = len(MSG) // 2
    body = (f"{half:x};ext=1\r\n".encode() + MSG[:half] + b"\r\n"
            + f"{len(MSG) - half:X}\r\n".encode() + MSG[half:] + b"\r\n0\r\nTrailer: v\r\n\r\n")
    sock = _request(h.proxy, [*OAUTH_LINES, "Transfer-Encoding: chunked"], body)
    with sock:
        resp, _ = _read_response(sock)
        assert resp.status == 200 and h.t.sent[0]["body"] == MSG
        sock.sendall(("\r\n".join([f"POST /{h.proxy._secret}/v1/messages HTTP/1.1", "Host: x",
                                   *OAUTH_LINES, f"Content-Length: {len(MSG)}"]) + "\r\n\r\n").encode() + MSG)
        resp, _ = _read_response(sock)
        assert resp.status == 200 and h.t.sent[1]["body"] == MSG


def test_chunked_body_without_trailers_ends_at_the_blank_line():
    h = Harness()
    h.proxy.bind(B)
    sock = _request(h.proxy, [*OAUTH_LINES, "Transfer-Encoding: chunked"],
                    f"{len(MSG):x}\r\n".encode() + MSG + b"\r\n0\r\n\r\n")
    with sock:
        resp, _ = _read_response(sock)
    assert resp.status == 200 and h.t.sent[0]["body"] == MSG


def test_garbled_chunk_size_is_a_400_and_closes():
    h = Harness()
    sock = _request(h.proxy, [*OAUTH_LINES, "Transfer-Encoding: chunked"], b"zz\r\n")
    with sock:
        resp, body = _read_response(sock)
        assert resp.status == 400 and b"bad request body" in body
        assert sock.recv(1) == b""
    assert h.t.sent == []


def test_a_body_cut_short_is_a_400():
    h = Harness()
    sock = _request(h.proxy, [*OAUTH_LINES, "Content-Length: 10"], b"abc", shutdown=True)
    with sock:
        resp, _ = _read_response(sock)
    assert resp.status == 400 and h.t.sent == []


def test_body_limits_are_inclusive(monkeypatch):
    monkeypatch.setattr(SP, "MAX_BODY", 10)
    for lines, body in (([f"Content-Length: 10"], b"x" * 10),
                        (["Transfer-Encoding: chunked"], b"5\r\n" + b"x" * 5 + b"\r\n5\r\n" + b"x" * 5
                         + b"\r\n0\r\n\r\n")):
        h = Harness()
        h.proxy.bind(B)
        with _request(h.proxy, [*OAUTH_LINES, *lines], body) as sock:
            resp, _ = _read_response(sock)
        assert resp.status == 200 and h.t.sent[0]["body"] == b"x" * 10
    for lines, body in (([f"Content-Length: 11"], b"x" * 11),
                        (["Transfer-Encoding: chunked"], b"6\r\n" + b"x" * 6 + b"\r\n5\r\n" + b"x" * 5
                         + b"\r\n0\r\n\r\n")):
        h = Harness()
        with _request(h.proxy, [*OAUTH_LINES, *lines], body) as sock:
            resp, _ = _read_response(sock)
            assert resp.status == 413
            assert sock.recv(1) == b""
        assert h.t.sent == []


def test_the_connection_closes_when_handle_says_so(monkeypatch):
    h = Harness()
    monkeypatch.setattr(h.proxy, "handle", lambda *a, **k: False)
    with _request(h.proxy, [f"Content-Length: 0"]) as sock:
        assert sock.recv(1) == b""


def test_the_connection_stays_open_when_handle_says_so(monkeypatch):
    h = Harness()
    monkeypatch.setattr(h.proxy, "handle", lambda *a, **k: True)
    with _request(h.proxy, [f"Content-Length: 0"]) as sock:
        sock.settimeout(0.3)
        with pytest.raises(TimeoutError):
            sock.recv(1)


# --- SessionCA ---------------------------------------------------------------------------------

def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-openssl"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_join_terminates_every_pem_with_a_newline():
    assert SC._join("a", "b") == "a\nb\n"
    assert SC._join("a\n", "b") == "a\nb\n"


@pytest.mark.parametrize("hosts", [(), ("",), ("a b",), ("ok.example", "bad host"), ("a/b",), ("a_b",),
                                   ("ok.example", "")])
def test_invalid_host_lists_are_refused(tmp_path, hosts):
    with pytest.raises(SessionCAError, match="invalid host list"):
        SessionCA(hosts, tmp_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_openssl_failure_reports_the_first_stderr_line_only(tmp_path):
    fake = _script(tmp_path, 'echo "boom: bad thing" >&2\necho "second line" >&2\nexit 1\n')
    (tmp_path / "t").mkdir()
    with pytest.raises(SessionCAError) as e:
        SessionCA(openssl=fake, tmp_root=tmp_path / "t")
    assert str(e.value) == "openssl ecparam failed: boom: bad thing"


def test_openssl_failure_without_stderr_reports_the_exit_code(tmp_path):
    fake = _script(tmp_path, "exit 3\n")
    (tmp_path / "t").mkdir()
    with pytest.raises(SessionCAError, match=r"openssl ecparam failed: 3$"):
        SessionCA(openssl=fake, tmp_root=tmp_path / "t")


def test_openssl_gets_an_existing_empty_config(tmp_path):
    fake = _script(tmp_path, '[ -e "$OPENSSL_CONF" ] && [ ! -s "$OPENSSL_CONF" ] || exit 9\n'
                             'exec /usr/bin/openssl "$@"\n')
    (tmp_path / "t").mkdir()
    ca = SessionCA(openssl=fake, tmp_root=tmp_path / "t")
    ca.close()


def test_only_public_certificates_are_left_behind(tmp_path):
    ca = SessionCA(tmp_root=tmp_path)
    try:
        assert sorted(os.listdir(ca._dir)) == ["ca.pem", "leaf.crt"]    # certificates only, no key
        pem = ca._dir / "ca.pem"
        assert pem.read_text() == ca.ca_pem()
        assert stat.S_IMODE(pem.stat().st_mode) == 0o600
        assert isinstance(ca.server_context(), SC.ssl.SSLContext)
    finally:
        ca.close()


def test_construction_restores_the_umask(tmp_path):
    before = os.umask(0o022)
    try:
        ca = SessionCA(tmp_root=tmp_path)
        ca.close()
        assert os.umask(before) == 0o022
    finally:
        os.umask(before)


def test_close_unregisters_and_removes_the_directory(tmp_path, monkeypatch):
    calls = []
    fake_atexit = types.SimpleNamespace(register=lambda f: calls.append(("register", f)),
                                        unregister=lambda f: calls.append(("unregister", f)))
    monkeypatch.setattr(SC, "atexit", fake_atexit)
    ca = SessionCA(tmp_root=tmp_path)
    directory = ca._dir
    assert calls == [("register", ca.close)]
    ca.close()
    assert calls == [("register", ca.close), ("unregister", ca.close)]
    assert not directory.exists()
    ca.close()    # idempotent
    assert len(calls) == 2


def test_bundles_are_refused_after_close(tmp_path):
    ca = SessionCA(tmp_root=tmp_path)
    ca.close()
    with pytest.raises(SessionCAError, match="closed"):
        ca.bundle_paths({})


# --- second round: connection hygiene, diagnostics, lifecycle ----------------------------------

def test_connect_via_accepts_a_status_line_without_a_reason(monkeypatch):
    UpstreamProxy(monkeypatch, b"HTTP/1.1 200\r\n\r\n")
    assert _within(_via) is not None


def test_closing_the_upstream_closes_its_idle_connections():
    up = SP._Upstream("http://example.com")
    conns = [TS.FakeConn(None, True), TS.FakeConn(None, True)]
    up._idle += conns
    up.close()
    assert all(c.closed for c in conns) and up._idle == []


def test_a_closing_upstream_connection_is_closed(monkeypatch):
    real = TS.FakeConn.getresponse
    conns = []

    def closing(self):
        r = real(self)
        r.will_close = True
        conns.append(self)
        return r

    monkeypatch.setattr(TS.FakeConn, "getresponse", closing)
    h = Harness()
    h.proxy.bind(B)
    h.send()
    assert conns and all(c.closed for c in conns)


class _TlsError(SP.ssl.SSLError):
    pass


def test_preflight_names_the_tls_failure_and_closes_the_probe():
    h = Harness()
    verify = SP.ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] long text")
    verify.verify_message = "self-signed certificate"
    h.t.fail = [verify]
    assert h.proxy.preflight() == "tls: self-signed certificate"
    other = _TlsError(1, "[SSL] long text")
    other.reason = "WRONG_VERSION_NUMBER"
    h.t.fail = [other]
    assert h.proxy.preflight() == "tls: WRONG_VERSION_NUMBER"


def test_preflight_closes_its_connection():
    h = Harness()
    conns = []
    real = h.t.acquire

    def spy(fresh=False):
        conn, reused = real(fresh)
        conns.append(conn)
        return conn, reused

    h.t.acquire = spy
    assert h.proxy.preflight() is None
    assert conns[0].closed


def test_idle_for_counts_up_from_the_last_request():
    h = Harness()
    h.proxy._last -= 5
    assert 4.9 <= h.proxy.idle_for() < 60
    h.proxy._active = 1
    assert h.proxy.idle_for() == 0.0


def test_request_log_records_the_elapsed_milliseconds():
    h = Harness()
    h.proxy.bind(B)
    h.send()
    (fields,) = [f for e, f in h.logs if e == "request"]
    assert 0 <= fields["ms"] < 5000


class _RejectingCtx:
    def wrap_socket(self, sock, server_side):
        e = SP.ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] long text")
        e.reason = "TLSV1_ALERT_UNKNOWN_CA"
        raise e


def test_tls_reject_reports_the_short_reason():
    reasons = []
    ca = types.SimpleNamespace(server_context=_RejectingCtx)
    proxy = SeatProxy(lambda hit: None, ca=ca, on_tls_reject=reasons.append)
    sock, status, _ = TS._connect(proxy, "api.anthropic.com:443")
    sock.close()
    assert status == 200
    deadline = threading.Event()
    for _ in range(100):
        if reasons:
            break
        deadline.wait(0.05)
    assert reasons == ["TLSV1_ALERT_UNKNOWN_CA"]


def test_the_tunnel_closes_its_far_socket(monkeypatch):
    proxy = SeatProxy(lambda hit: None)
    far_ends = []

    def create_connection(address, *args, **kwargs):
        a, b = socket.socketpair()
        far_ends.append(a)
        b.close()    # the far side hangs up at once
        return a

    monkeypatch.setattr(socket, "create_connection", create_connection)
    sock, status, _ = TS._connect(proxy, "example.com:443")
    with sock:
        assert status == 200
        assert sock.recv(1) == b""
    for _ in range(100):
        if far_ends[0].fileno() == -1:
            break
        threading.Event().wait(0.05)
    assert far_ends[0].fileno() == -1


@TS.needs_bind
def test_start_and_stop_log_and_release_the_upstream(monkeypatch):
    for var in TS.PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    h = Harness()
    h.t.idle.append(TS.FakeConn(h.t, True))
    url = h.proxy.start()
    assert h.proxy.start() == url
    h.proxy.stop()
    h.proxy.stop()
    events = [e for e, _ in h.logs]
    assert events.count("proxy_start") == 1 and events.count("proxy_stop") == 1
    assert h.t.idle == []


def test_an_unbuffered_body_read_never_asks_for_more_than_a_chunk(monkeypatch):
    # An unbuffered read allocates the size it asks for: asking for the whole remaining body on every
    # call would allocate quadratically in the body size.
    sizes = []
    readinto = socket.SocketIO.readinto
    monkeypatch.setattr(socket.SocketIO, "readinto", lambda self, b: (sizes.append(len(b)), readinto(self, b))[1])
    body = json.dumps({"model": "m", "max_tokens": 5, "pad": "x" * (3 * SP.CHUNK),
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    h = Harness()
    h.proxy.bind(B)
    sock = _request(h.proxy, [*OAUTH_LINES, f"Content-Length: {len(body)}"], body)
    with sock:
        resp, _ = _read_response(sock)
    assert resp.status == 200 and h.t.sent[0]["body"] == body
    assert max(sizes) <= SP.CHUNK
