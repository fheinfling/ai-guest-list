"""The seat proxy: routing, token injection, replay rules, redaction and streaming.

Most tests drive ``SeatProxy.handle`` directly with an in-memory upstream (``FakeTransport``) and
parse what it wrote with ``http.client``, so they need no sockets. The tests at the end run the real
``ThreadingHTTPServer`` against the fake API on loopback and skip where binding a port is not allowed.
"""
from __future__ import annotations

import gzip
import http.client
import io
import json
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from acctsw import seatproxy as SP
from acctsw.seatproxy import Hit, Seat, SeatProxy, classify, tok

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import fake_anthropic as FA  # noqa: E402

_REAL_CREATE_CONNECTION = socket.create_connection   # captured before conftest disables it

HOME = Seat("home@example.com", None)
A = Seat("a@example.com", "sk-ant-oat01-seat-A-0123456789abcdef")
B = Seat("b@example.com", "sk-ant-oat01-seat-B-fedcba9876543210")
C = Seat("c@example.com", "sk-ant-oat01-seat-C-00000000000000cc")
CLIENT_TOKEN = "sk-ant-oat01-client-home-login-5555555555"
MSG = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}).encode()
STREAM = json.dumps({"model": "m", "max_tokens": 5, "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}).encode()


# --- in-memory upstream ------------------------------------------------------------------------

class Wire(io.BytesIO):
    """The client side of the connection: remembers how much had been flushed."""

    def __init__(self):
        super().__init__()
        self.flushed = 0

    def flush(self):
        self.flushed = len(self.getvalue())


class FakeResp:
    def __init__(self, ans: FA.Answer, transport: "FakeTransport"):
        self.status = ans.status
        self.reason = http.client.responses.get(ans.status, "")
        self.msg = http.client.HTTPMessage()
        for k, v in ans.headers:
            self.msg[k] = v
        if ans.chunked:
            self.msg["transfer-encoding"] = "chunked"
        self.headers = self.msg
        self.will_close = False
        self._chunks = list(ans.chunks)
        self._sent: list[bytes] = []
        self._t = transport

    def getheaders(self):
        return list(self.msg.items())

    def getheader(self, name, default=None):
        return self.msg.get(name, default)

    def read(self):
        data, self._chunks = b"".join(self._chunks), []
        return data

    def read1(self, n=-1):
        wire = self._t.wire
        if wire is not None:
            # Every chunk already handed out must have reached the client before the next read.
            seen = wire.getvalue()[:wire.flushed]
            self._t.unflushed += sum(1 for c in self._sent if c not in seen)
        if self._t.break_after is not None and len(self._sent) >= self._t.break_after:
            raise ConnectionResetError("upstream reset mid-stream")
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        self._sent.append(chunk)
        return chunk


class FakeConn:
    def __init__(self, transport: "FakeTransport", reused: bool):
        self.t = transport
        self.reused = reused
        self.closed = False
        self._pending = None

    def request(self, method, url, body=None, headers=None):
        self.t.sent.append({"method": method, "url": url, "headers": dict(headers or {}),
                            "body": body, "reused": self.reused})
        if self.t.fail:
            exc = self.t.fail.pop(0)
            if exc is not None:
                raise exc
        self._pending = (method, url, dict(headers or {}), body or b"")

    def getresponse(self):
        if self.t.fail_response:
            raise self.t.fail_response.pop(0)
        if self.t.before_response:
            self.t.before_response(self)
        method, url, headers, body = self._pending
        return FakeResp(self.t.fake.respond(method, url, headers, body), self.t)

    def close(self):
        self.closed = True


class FakeTransport:
    """Stands in for ``seatproxy._Upstream``: ``acquire``/``release``/``close`` plus a request log."""

    base = ""
    netloc = "api.anthropic.com"
    host = "api.anthropic.com"
    port = 443

    def __init__(self, fake: FA.FakeAnthropic):
        self.fake = fake
        self.sent: list[dict] = []
        self.idle: list[FakeConn] = []
        self.fail: list[BaseException | None] = []   # raised by the next request() calls, in order
        self.fail_response: list[BaseException] = []  # raised by the next getresponse() calls
        self.before_response = None
        self.break_after: int | None = None
        self.wire: Wire | None = None
        self.unflushed = 0
        self.acquired: list[bool] = []

    def acquire(self, fresh=False):
        if not fresh and self.idle:
            self.acquired.append(True)
            return self.idle.pop(), True
        self.acquired.append(False)
        return FakeConn(self, False), False

    def release(self, conn):
        conn.reused = True
        self.idle.append(conn)

    def close(self):
        self.idle.clear()


class Harness:
    def __init__(self, decide=None, *, timeout=2.0):
        self.hits: list[Hit] = []
        self.answers: list = []
        self.logs: list[tuple[str, dict]] = []
        self.fake = FA.FakeAnthropic()
        self._decide = decide

        def deciding(hit):
            self.hits.append(hit)
            if self._decide is not None:
                return self._decide(hit)
            return self.answers.pop(0) if self.answers else None

        self.proxy = SeatProxy(deciding, upstream="https://api.anthropic.com", decide_timeout=timeout,
                               log=lambda event, **f: self.logs.append((event, f)))
        self.t = FakeTransport(self.fake)
        self.proxy._transport = self.t

    def url(self, path: str) -> str:
        return f"/{self.proxy._secret}{path}"

    def send(self, path="/v1/messages?beta=true", *, method="POST", body=MSG, headers=None,
             raw_target=None, auth=f"Bearer {CLIENT_TOKEN}", beta="oauth-2025-04-20"):
        wire = Wire()
        self.t.wire = wire
        hdrs = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        hdrs += [("anthropic-beta", beta)] if beta is not None else []
        hdrs += [("Authorization", auth)] if auth is not None else []
        hdrs += list(headers or [])
        target = self.url(path) if raw_target is None else raw_target
        keep = self.proxy.handle(method, target, hdrs, body, wire)
        return parse(wire.getvalue(), method), keep


class Parsed:
    def __init__(self, resp: http.client.HTTPResponse, body: bytes, raw: bytes):
        self.status = resp.status
        self.headers = resp.headers
        self.body = body
        self.raw = raw

    def json(self):
        return json.loads(self.body)


class _Sock:
    def __init__(self, data: bytes):
        self.f = io.BytesIO(data)

    def makefile(self, *_args, **_kwargs):
        return self.f


def parse(raw: bytes, method: str = "POST") -> Parsed:
    resp = http.client.HTTPResponse(_Sock(raw), method=method)
    resp.begin()
    return Parsed(resp, resp.read(), raw)


def rules(h: Harness, **by_seat) -> None:
    """``rules(h, a=429, b=401)``: answer seat A's token with 429, seat B's with 401."""
    seats = {"a": A, "b": B, "c": C, "client": Seat("", CLIENT_TOKEN)}
    h.fake.update({"status_by_token": {tok(seats[k].token): v for k, v in by_seat.items()}})


def bearers(h: Harness) -> list[str | None]:
    return [r["bearer"] for r in h.fake.requests]


# --- classify ----------------------------------------------------------------------------------

@pytest.mark.parametrize("status,headers,expected", [
    (429, {"anthropic-ratelimit-unified-status": "rejected"}, "limit"),
    (429, {"Anthropic-RateLimit-Unified-Status": "Rejected"}, "limit"),
    (429, [("anthropic-ratelimit-unified-status", "rejected")], "limit"),
    (429, {"anthropic-ratelimit-unified-status": "allowed"}, None),
    (429, {"anthropic-ratelimit-unified-status": "allowed_warning"}, None),
    (429, {}, None),
    (529, {"anthropic-ratelimit-unified-status": "rejected"}, None),
    (401, {}, "auth"),
    (200, {"anthropic-ratelimit-unified-status": "rejected"}, None),
    (403, {}, None),
])
def test_classify(status, headers, expected):
    assert classify(status, headers) == expected


def test_classify_reads_an_http_message():
    msg = http.client.HTTPMessage()
    msg["Anthropic-Ratelimit-Unified-Status"] = "rejected"
    assert classify(429, msg) == "limit"


# --- the secret path ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["/v1/messages", "/", "", "/wrong/v1/messages",
                                    "/{secret}x/v1/messages", "/{short}/v1/messages",
                                    "/x{secret}/v1/messages", "{secret}/v1/messages"])
def test_requests_without_the_secret_are_forbidden(target):
    h = Harness()
    h.proxy.bind(B)
    secret = h.proxy._secret
    resp, keep = h.send(raw_target=target.format(secret=secret, short=secret[:-1]))
    assert resp.status == 403
    assert resp.json()["error"]["type"] == "permission_error"
    assert h.t.sent == [] and h.hits == []
    assert keep


@pytest.mark.parametrize("suffix,upstream", [("/v1/messages?beta=true", "/v1/messages?beta=true"),
                                             ("", "/"), ("?x=1", "/?x=1"), ("/", "/")])
def test_the_secret_is_stripped_before_forwarding(suffix, upstream):
    h = Harness()
    h.send(raw_target=f"/{h.proxy._secret}{suffix}")
    assert h.t.sent[0]["url"] == upstream


# --- routing and injection ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens",
                                  "/v1/messages/count_tokens?beta=true"])
def test_message_posts_carry_the_bound_seat_token(path):
    h = Harness()
    h.proxy.bind(B)
    # lower-case, as node sends it: the client's header must not survive next to the seat's
    resp, _ = h.send(path, auth=None, headers=[("authorization", f"Bearer {CLIENT_TOKEN}")])
    assert resp.status == 200
    sent = h.t.sent[0]["headers"]
    auth = [v for k, v in sent.items() if k.lower() == "authorization"]
    assert auth == [f"Bearer {B.token}"]
    assert sent["Host"] == "api.anthropic.com"
    assert sent["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.parametrize("method,path", [("GET", "/v1/messages"), ("POST", "/v1/messages/batches"),
                                         ("POST", "/v1/complete"), ("GET", "/api/oauth/profile"),
                                         ("POST", "/api/event_logging/batch"),
                                         ("POST", "/v1/messagesX"), ("PUT", "/v1/messages")])
def test_only_message_posts_carry_the_seat_token(method, path):
    h = Harness()
    h.proxy.bind(B)
    h.send(path, method=method, body=b"{}" if method != "GET" else b"")
    sent = h.t.sent[0]["headers"]
    assert [v for k, v in sent.items() if k.lower() == "authorization"] == [f"Bearer {CLIENT_TOKEN}"]
    assert h.hits == []


def test_home_seat_is_pure_passthrough():
    h = Harness()
    h.proxy.bind(HOME)
    h.send(headers=[("x-custom", "1")])
    sent = h.t.sent[0]["headers"]
    assert sent["Authorization"] == f"Bearer {CLIENT_TOKEN}"
    assert sent["x-custom"] == "1"


@pytest.mark.parametrize("auth,extra", [
    (None, [("x-api-key", "sk-ant-api03-user-script")]),             # an SDK script with an API key
    (f"Bearer {CLIENT_TOKEN}", [("x-api-key", "sk-ant-api03-both")]),  # bearer AND key: not Claude's login
    ("Basic dXNlcjpwYXNz", []),
    ("sk-ant-oat01-no-scheme", []),
    (None, []),                                                     # no credentials at all
])
def test_only_oauth_bearer_clients_get_a_seat_token(auth, extra):
    h = Harness()
    h.proxy.bind(B)
    rules(h, client=429)
    h.fake.update({"next": [429]})    # even a limit on such a request is not the proxy's business
    h.answers = [C]
    resp, _ = h.send(auth=auth, headers=extra)
    assert resp.status == 429
    sent = h.t.sent[0]["headers"]
    assert [v for k, v in sent.items() if k.lower() == "authorization"] == ([auth] if auth else [])
    assert [v for k, v in sent.items() if k.lower() == "x-api-key"] == [v for _, v in extra]
    assert h.hits == [] and len(h.t.sent) == 1


@pytest.mark.parametrize("beta", [None, "", "claude-code-20250219", "interleaved-thinking-2025-05-14",
                                  "oauth", "prompt-caching-2024-07-31,oauthy"])
def test_bearer_without_the_oauth_beta_is_not_claude_code(beta):
    """A bearer token alone could be any gateway client; Claude Code's login opts into oauth-*."""
    h = Harness()
    h.proxy.bind(B)
    h.fake.update({"next": [429]})
    h.answers = [C]
    resp, _ = h.send(beta=beta)
    assert resp.status == 429
    assert h.t.sent[0]["headers"]["Authorization"] == f"Bearer {CLIENT_TOKEN}"
    assert h.hits == [] and len(h.t.sent) == 1
    assert h.proxy.messages_seen() == 0


@pytest.mark.parametrize("beta,extra", [
    ("claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14", []),
    ("OAuth-2025-04-20", []),
    ("claude-code-20250219", [("anthropic-beta", "oauth-2025-04-20")]),   # split over two headers
])
def test_oauth_beta_anywhere_in_anthropic_beta_counts(beta, extra):
    h = Harness()
    h.proxy.bind(B)
    h.send(beta=beta, headers=extra)
    assert h.t.sent[0]["headers"]["Authorization"] == f"Bearer {B.token}"
    assert h.proxy.messages_seen() == 1


def test_bearer_scheme_is_case_insensitive():
    h = Harness()
    h.proxy.bind(B)
    h.send(auth=f"bearer {CLIENT_TOKEN}")
    assert h.t.sent[0]["headers"]["Authorization"] == f"Bearer {B.token}"


@pytest.mark.parametrize("path", ["//x/v1/messages", "//api.anthropic.com/v1/messages", "/v1/messages/",
                                  "/V1/messages", "/v1/messages;x", "/v1//messages",
                                  "/v1/messages/count_tokens/", "/x/v1/messages"])
def test_inject_path_matches_exactly(path):
    h = Harness()
    h.proxy.bind(B)
    h.send(path)
    assert h.t.sent[0]["headers"]["Authorization"] == f"Bearer {CLIENT_TOKEN}"


def test_messages_seen_counts_client_message_posts():
    h = Harness()
    h.proxy.bind(A)
    assert h.proxy.messages_seen() == 0
    h.send("/v1/messages?beta=true")
    rules(h, a=429)
    h.send("/v1/messages")                          # a passed-through limit still counts
    h.proxy.bind(HOME)
    h.send("/v1/messages")                          # eligible but not injected (home): counts
    h.send("/v1/messages/count_tokens")
    h.send("/v1/messages", method="GET", body=b"")
    h.send(raw_target="/v1/messages")               # forbidden: never reached the proxy proper
    h.send("/api/oauth/profile")
    assert h.proxy.messages_seen() == 3


@pytest.mark.parametrize("auth,extra", [
    (None, [("x-api-key", "sk-ant-api03-user-script")]),
    (f"Bearer {CLIENT_TOKEN}", [("x-api-key", "sk-ant-api03-both")]),
    ("Basic dXNlcjpwYXNz", []),
    (None, []),
])
def test_messages_seen_ignores_non_oauth_clients(auth, extra):
    """An SDK script in the child must not prove that Claude Code talks through the proxy."""
    h = Harness()
    h.proxy.bind(B)
    for _ in range(3):
        resp, _ = h.send("/v1/messages", auth=auth, headers=extra)
        assert resp.status == 200
    assert h.proxy.messages_seen() == 0


def test_unbound_proxy_is_pure_passthrough():
    h = Harness()
    assert h.proxy.bound() == Seat("", None)
    h.send()
    assert bearers(h) == [tok(CLIENT_TOKEN)]


def test_hop_by_hop_headers_are_dropped_and_length_recomputed():
    h = Harness()
    h.proxy.bind(B)
    h.send(headers=[("Connection", "keep-alive, X-Secret-Hop"), ("Keep-Alive", "timeout=5"),
                    ("Proxy-Authorization", "Basic abc"), ("TE", "trailers"), ("Upgrade", "h2c"),
                    ("X-Secret-Hop", "1"), ("Host", "127.0.0.1:1234"), ("Accept-Encoding", "gzip, br"),
                    ("Content-Length", "999")])
    sent = {k.lower(): v for k, v in h.t.sent[0]["headers"].items()}
    for gone in ("connection", "keep-alive", "proxy-authorization", "te", "upgrade", "x-secret-hop"):
        assert gone not in sent
    assert sent["host"] == "api.anthropic.com"
    assert sent["accept-encoding"] == "gzip, br"
    assert sent["content-length"] == str(len(MSG))


def test_content_encoding_passes_through_untouched():
    h = Harness()
    packed = gzip.compress(b'{"ok": true}')
    h.fake.respond = lambda *a: FA.Answer(200, [("content-type", "application/json"),
                                               ("content-encoding", "gzip"),
                                               ("content-length", str(len(packed)))], [packed])
    resp, _ = h.send(headers=[("Accept-Encoding", "gzip")])
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.body == packed


# --- limits ------------------------------------------------------------------------------------

def test_limit_replays_on_the_decided_seat_and_binds_it():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    h.answers = [B]
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(A.token), tok(B.token)]
    assert h.proxy.bound() == B
    [hit] = h.hits
    assert (hit.kind, hit.seat, hit.claim) == ("limit", A.email, "five_hour")
    assert datetime.fromisoformat(hit.reset_at).timestamp() == h.fake.reset
    assert "rate limit" in hit.detail
    h.send()    # the next request goes straight to B
    assert bearers(h)[-1] == tok(B.token) and len(h.hits) == 1


@pytest.mark.parametrize("claim", ["five_hour", "seven_day", "seven_day_opus", None])
def test_limit_hit_carries_the_representative_claim(claim):
    h = Harness()
    h.proxy.bind(A)
    respond = h.fake.respond

    def with_claim(*args):
        ans = respond(*args)
        ans.headers = [(k, v) for k, v in ans.headers
                       if k != "anthropic-ratelimit-unified-representative-claim"]
        if claim is not None and ans.status == 429:
            ans.headers.append(("anthropic-ratelimit-unified-representative-claim", claim))
        return ans
    h.fake.respond = with_claim
    rules(h, a=429)
    h.answers = [B]
    h.send()
    assert [x.claim for x in h.hits] == [claim]


def test_limit_on_home_replays_on_a_seat():
    h = Harness()
    h.proxy.bind(HOME)
    rules(h, client=429)
    h.answers = [B]
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(CLIENT_TOKEN), tok(B.token)]
    assert h.hits[0].seat == HOME.email


def test_limit_back_to_home_uses_the_client_login():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    h.answers = [HOME]
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(A.token), tok(CLIENT_TOKEN)]
    assert h.proxy.bound() == HOME


@pytest.mark.parametrize("status", [FA.TRANSIENT_429, 529])
def test_transient_429_and_529_pass_through(status):
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=status)
    h.answers = [B]
    resp, _ = h.send()
    assert resp.status == (429 if status == FA.TRANSIENT_429 else 529)
    assert h.hits == [] and bearers(h) == [tok(A.token)]
    assert h.proxy.bound() == A


def test_limit_without_an_answer_passes_the_429_through_verbatim():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    resp, keep = h.send()
    assert resp.status == 429 and keep
    assert resp.headers["anthropic-ratelimit-unified-status"] == "rejected"
    assert resp.headers["anthropic-ratelimit-unified-reset"] == str(h.fake.reset)
    assert resp.json()["error"]["type"] == "rate_limit_error"
    assert bearers(h) == [tok(A.token)] and h.proxy.bound() == A


def test_decide_timeout_passes_the_limit_through():
    gate = threading.Event()    # decide answers only once the test lets it: never, in time
    h = Harness(lambda hit: (gate.wait(60), B)[1], timeout=0.2)
    h.proxy.bind(A)
    rules(h, a=429)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=h.send()), daemon=True)
    try:
        worker.start()
        worker.join(10)    # a hang guard, not a measurement: the request ends after 0.2s
        assert not worker.is_alive(), "a decide that never answers must not hold the request"
    finally:
        gate.set()
        worker.join(5)
    assert out["r"][0].status == 429
    assert bearers(h) == [tok(A.token)]
    assert any(e == "decide_timeout" for e, _ in h.logs)


def test_a_burst_waits_for_one_decide_timeout_only(monkeypatch):
    # The proxy's clock stands still, so the stall window after the first timeout cannot lapse
    # however slowly the other requests get scheduled; decide's own wait is real time.
    import types
    monkeypatch.setattr(SP, "time", types.SimpleNamespace(monotonic=lambda: 1000.0))
    gate = threading.Event()
    h = Harness(lambda hit: (gate.wait(60), B)[1], timeout=0.2)
    h.proxy.bind(A)
    rules(h, a=429)
    results = []
    workers = [threading.Thread(target=lambda: results.append(h.send()[0].status), daemon=True)
               for _ in range(3)]
    try:
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)    # hang guard only
    finally:
        gate.set()
    assert results == [429, 429, 429]
    assert len(h.hits) == 1, "the rest of the burst must not queue behind another timeout"


def test_decide_exception_passes_the_limit_through():
    def boom(hit):
        raise RuntimeError(f"keychain locked for {A.token}")
    h = Harness(boom)
    h.proxy.bind(A)
    rules(h, a=429)
    resp, _ = h.send()
    assert resp.status == 429
    assert A.token not in json.dumps(h.logs)
    assert any(e == "decide_error" for e, _ in h.logs)


def test_each_credential_is_tried_once():
    h = Harness(lambda hit: B)    # keeps answering B, which is limited too
    h.proxy.bind(A)
    rules(h, a=429, b=429)
    resp, _ = h.send()
    assert resp.status == 429
    assert bearers(h) == [tok(A.token), tok(B.token)]


def test_answering_the_seat_that_hit_is_not_a_replay():
    h = Harness(lambda hit: A)
    h.proxy.bind(A)
    rules(h, a=429)
    resp, _ = h.send()
    assert resp.status == 429 and bearers(h) == [tok(A.token)]


def test_upstream_sends_are_capped():
    n = iter(range(1000))

    def endless(hit):
        i = next(n)
        return Seat(f"s{i}@example.com", f"sk-ant-oat01-endless-{i:04d}") if i < 50 else None
    h = Harness(endless)
    h.fake.update({"next": [429] * 60})
    h.proxy.bind(A)
    resp, _ = h.send()
    assert resp.status == 429
    assert len(h.fake.requests) == SP.MAX_TRIES


def test_concurrent_rebind_is_followed_without_asking():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)

    def another_request_moved_on(conn):
        if h.t.sent[-1]["headers"].get("Authorization") == f"Bearer {A.token}":
            h.proxy.bind(B)
    h.t.before_response = another_request_moved_on
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(A.token), tok(B.token)]
    assert h.hits == []


def test_no_replay_once_the_response_started():
    """A 200 stream that breaks halfway is never resent, even on a pooled connection."""
    h = Harness()
    h.proxy.bind(A)
    h.send()                       # leaves a pooled connection behind
    assert h.t.idle
    h.t.break_after = 2
    resp_raw = Wire()
    h.t.wire = resp_raw
    keep = h.proxy.handle("POST", h.url("/v1/messages"), [("Authorization", f"Bearer {CLIENT_TOKEN}")],
                          STREAM, resp_raw)
    assert keep is False
    assert len(h.t.sent) == 2      # the first request, plus this one exactly once
    assert h.t.sent[1]["reused"]
    assert resp_raw.getvalue().count(b"HTTP/1.1 ") == 1
    assert b"api_error" not in resp_raw.getvalue()
    assert any(e == "upstream_broken" for e, _ in h.logs)


# --- 401s --------------------------------------------------------------------------------------

def test_home_401_passes_through_without_decide():
    h = Harness()
    h.proxy.bind(HOME)
    rules(h, client=401)
    h.answers = [B]
    resp, _ = h.send()
    assert resp.status == 401
    assert h.hits == [] and bearers(h) == [tok(CLIENT_TOKEN)]
    assert h.proxy.bound() == HOME


def test_injected_401_replays_on_a_reread_token():
    fresh = Seat(B.email, "sk-ant-oat01-seat-B-reread-111111")
    h = Harness()
    h.proxy.bind(B)
    rules(h, b=401)
    h.answers = [fresh]
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(B.token), tok(fresh.token)]
    assert h.hits[0].kind == "auth" and h.hits[0].seat == B.email
    assert h.hits[0].reset_at is None and h.hits[0].detail == "OAuth token has expired."
    assert h.proxy.bound() == fresh


def test_injected_401_without_answer_fails_open():
    h = Harness()
    h.proxy.bind(B)
    rules(h, b=401)
    resp, _ = h.send()
    assert resp.status == 200
    assert bearers(h) == [tok(B.token), tok(CLIENT_TOKEN)]
    assert h.proxy.bound() == B    # no answer, no rebind


def test_injected_401_then_client_401_passes_through():
    h = Harness()
    h.proxy.bind(B)
    rules(h, b=401, client=401)
    resp, _ = h.send()
    assert resp.status == 401
    assert bearers(h) == [tok(B.token), tok(CLIENT_TOKEN)]
    assert len(h.hits) == 1


# --- streaming and relay -----------------------------------------------------------------------

def test_stream_is_flushed_per_chunk():
    h = Harness()
    h.proxy.bind(B)
    resp, keep = h.send(body=STREAM)
    assert resp.status == 200 and keep
    assert resp.headers["transfer-encoding"] == "chunked"
    assert resp.headers["content-type"] == "text/event-stream"
    events = [line for line in resp.body.split(b"\n") if line.startswith(b"event: ")]
    assert events[0] == b"event: message_start" and events[-1] == b"event: message_stop"
    assert b"hello from fake upstream" not in resp.body    # text arrives as deltas
    assert h.t.unflushed == 0


def test_buffered_answer_keeps_its_length():
    h = Harness()
    resp, _ = h.send()
    assert resp.status == 200
    assert int(resp.headers["content-length"]) == len(resp.body)
    assert resp.json()["type"] == "message"
    assert "transfer-encoding" not in resp.headers


def test_head_request_sends_no_body():
    h = Harness()
    resp, _ = h.send("/v1/models", method="HEAD", body=b"")
    assert resp.status == 404 and resp.body == b""


# --- upstream faults ---------------------------------------------------------------------------

def test_stale_pooled_connection_is_retried_once_on_a_fresh_one():
    h = Harness()
    h.send()
    h.t.fail = [http.client.RemoteDisconnected("closed")]
    resp, _ = h.send()
    assert resp.status == 200
    assert h.t.acquired == [False, True, False]


def test_reused_connection_failing_after_send_is_not_resent():
    h = Harness()
    h.proxy.bind(B)
    h.send()
    assert h.t.idle
    h.t.fail_response = [http.client.RemoteDisconnected("closed after the request went out")]
    resp, keep = h.send()
    assert resp.status == 502 and keep
    assert h.t.acquired == [False, True]
    assert len(h.fake.requests) == 1    # upstream answered the first request only


def test_connect_failure_answers_502_without_retry():
    h = Harness()
    h.proxy.bind(B)
    h.t.fail = [ConnectionRefusedError(f"refused with Bearer {B.token}")]
    resp, keep = h.send()
    assert resp.status == 502 and keep
    assert resp.json() == {"type": "error", "error": {"type": "api_error",
                                                      "message": "seat proxy: upstream unreachable"}}
    assert h.t.acquired == [False]
    assert B.token not in json.dumps(h.logs)


def test_handler_exception_answers_502_and_the_proxy_keeps_serving():
    h = Harness()
    h.proxy.bind(B)
    real = h.t.acquire
    h.t.acquire = lambda fresh=False: (_ for _ in ()).throw(ValueError(f"bad header Bearer {B.token}"))
    resp, keep = h.send()
    assert resp.status == 502 and not keep
    assert resp.json()["error"]["type"] == "api_error"
    assert B.token not in json.dumps(h.logs)
    h.t.acquire = real
    assert h.send()[0].status == 200
    assert h.proxy.in_flight() == 0


# --- logs --------------------------------------------------------------------------------------

def test_logs_never_carry_token_text():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429, b=401)
    h.answers = [B]
    h.send()                                       # A limit -> B 401 -> fail open on the client
    h.t.fail = [OSError(f"connect failed for Bearer {B.token}")] * 2    # pooled, then fresh
    h.send()
    text = json.dumps(h.logs)
    for secret in (A.token, B.token, CLIENT_TOKEN, h.proxy._secret):
        assert secret not in text
    assert tok(A.token) in text and tok(B.token) in text
    events = [e for e, _ in h.logs]
    assert {"bind", "hit", "replay", "request", "upstream_error"} <= set(events)


def test_seat_repr_hides_the_token():
    assert A.token not in repr(A) and A.token not in str(Hit("limit", A.email, None, None, None))


# --- activity ----------------------------------------------------------------------------------

def test_in_flight_and_idle_for():
    h = Harness()
    h.proxy.bind(A)
    rules(h, a=429)
    seen = {}

    def decide(hit):
        seen.update(active=h.proxy.in_flight(), idle=h.proxy.idle_for())
        return B
    h._decide = decide
    h.send()
    assert seen == {"active": 1, "idle": 0.0}
    assert h.proxy.in_flight() == 0
    time.sleep(0.02)
    assert h.proxy.idle_for() >= 0.02


# --- upstream connection setup (no connect happens) --------------------------------------------

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
              "NO_PROXY", "no_proxy")


@pytest.fixture
def no_proxy_env(monkeypatch):
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_upstream_tunnels_through_https_proxy(no_proxy_env):
    up = SP._Upstream("https://api.anthropic.com", proxy="http://user:p%40ss@proxy.corp:3128")
    conn, reused = up.acquire()
    assert not reused
    assert isinstance(conn, http.client.HTTPSConnection)
    assert (conn.host, conn.port) == ("proxy.corp", 3128)
    assert (conn._tunnel_host, conn._tunnel_port) == ("api.anthropic.com", 443)
    assert conn._tunnel_headers["Proxy-Authorization"] == "Basic dXNlcjpwQHNz"


def test_no_proxy_bypasses_the_tunnel(no_proxy_env):
    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    no_proxy_env.setenv("HTTP_PROXY", "http://proxy.corp:3128")
    no_proxy_env.setenv("NO_PROXY", "127.0.0.1,localhost")
    up = SP._Upstream("http://127.0.0.1:9999/base", proxy="http://proxy.corp:3128")
    conn, _ = up.acquire()
    assert type(conn) is http.client.HTTPConnection
    assert (conn.host, conn.port, conn._tunnel_host) == ("127.0.0.1", 9999, None)
    assert up.base == "/base" and up.netloc == "127.0.0.1:9999"


def test_no_proxy_configured_connects_directly(no_proxy_env):
    # The child's HTTPS_PROXY names this proxy; the launcher's environment is never a source.
    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    conn, _ = SP._Upstream("https://api.anthropic.com").acquire()
    assert (conn.host, conn.port, conn._tunnel_host) == ("api.anthropic.com", 443, None)


def test_upstream_must_be_http():
    with pytest.raises(ValueError):
        SeatProxy(lambda hit: None, upstream="file:///etc/passwd")


def test_pool_reuses_released_connections():
    up = SP._Upstream("https://api.anthropic.com")
    conn, _ = up.acquire()
    up.release(conn)
    assert up.acquire() == (conn, True)
    up.release(conn)
    assert up.acquire(fresh=True)[0] is not conn


def test_bind_failure_raises_and_stop_is_safe(monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(SP, "_Server", refuse)
    p = SeatProxy(lambda hit: None)
    with pytest.raises(OSError):
        p.start()
    p.stop()
    p.stop()


# --- pooled connections the upstream closed --------------------------------------------------

def test_dropped_pooled_connection_is_discarded_before_use(no_proxy_env):
    up = SP._Upstream("http://127.0.0.1:9")
    gone, alive = http.client.HTTPConnection("127.0.0.1", 9), http.client.HTTPConnection("127.0.0.1", 9)
    gone.sock, far = socket.socketpair()
    far.close()                                 # the upstream closed the idle keep-alive connection
    alive.sock, peer = socket.socketpair()
    try:
        up.release(gone)
        conn, reused = up.acquire()
        assert conn is not gone and not reused and gone.sock is None
        up.release(alive)
        assert up.acquire() == (alive, True)
    finally:
        peer.close()
        alive.close()


# --- TLS trust and preflight -------------------------------------------------------------------

class _Ctx:
    def __init__(self, fail=()):
        self.loaded = []
        self.fail = fail

    def load_verify_locations(self, cafile=None, capath=None):
        if (cafile or capath) in self.fail:
            raise FileNotFoundError(cafile or capath)
        self.loaded.append(("cafile", cafile) if cafile else ("capath", capath))


def test_tls_context_adds_the_users_ca_files(no_proxy_env):
    ctx = _Ctx(fail=("/missing.pem",))
    no_proxy_env.setattr(SP.ssl, "create_default_context", lambda: ctx)
    no_proxy_env.setenv("SSL_CERT_FILE", "/missing.pem")
    no_proxy_env.setenv("SSL_CERT_DIR", "/etc/corp-certs")
    no_proxy_env.setenv("NODE_EXTRA_CA_CERTS", "/etc/corp-ca.pem")
    up = SP._Upstream("https://api.anthropic.com")
    assert up.ssl is ctx
    assert ctx.loaded == [("capath", "/etc/corp-certs"), ("cafile", "/etc/corp-ca.pem")]


def test_tls_context_without_extras_is_the_default(no_proxy_env):
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
        no_proxy_env.delenv(var, raising=False)
    ctx = SP._tls_context()
    assert ctx.verify_mode == SP.ssl.CERT_REQUIRED and ctx.check_hostname


def test_preflight_sends_a_tokenless_head_on_a_fresh_connection():
    h = Harness()
    h.proxy.bind(B)
    h.send()                                     # leaves a pooled connection that must not be used
    assert h.proxy.preflight(timeout=1.5) is None
    probe = h.t.sent[-1]
    assert (probe["method"], probe["url"], probe["reused"]) == ("HEAD", "/", False)
    assert probe["headers"] == {"Host": "api.anthropic.com"}
    assert h.t.acquired[-1] is False
    assert h.proxy.messages_seen() == 1


@pytest.mark.parametrize("exc,reason", [
    (SP.ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls: "),
    (SP.ssl.SSLError(1, "wrong version"), "tls: "),
    (ConnectionRefusedError(61, "Connection refused"), "unreachable: ConnectionRefusedError"),
    (TimeoutError("timed out"), "unreachable: TimeoutError"),
])
def test_preflight_reports_why_upstream_is_unusable(exc, reason):
    h = Harness()
    h.t.fail = [exc]
    got = h.proxy.preflight()
    assert got is not None and got.startswith(reason) and len(got) <= 200


def test_preflight_real_tls_needs_the_users_ca(tmp_path, no_proxy_env):
    """A TLS handshake over a socketpair against a certificate from a private CA: it fails with the
    default trust and passes once NODE_EXTRA_CA_CERTS names the CA."""
    certs = _make_certs(tmp_path)
    server_ctx = SP.ssl.SSLContext(SP.ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certs["leaf"], certs["key"])
    fake = FA.FakeAnthropic()
    handler = FA.make_handler(fake)

    def serve(sock):
        try:
            with server_ctx.wrap_socket(sock, server_side=True) as tls:
                handler(tls, ("127.0.0.1", 0), None)
        except (OSError, SP.ssl.SSLError):
            sock.close()

    def connect(*_args, **_kwargs):
        a, b = socket.socketpair()
        threading.Thread(target=serve, args=(b,), daemon=True).start()
        return _NoDelaySocket(fileno=a.detach())

    no_proxy_env.setattr(socket, "create_connection", connect)
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
        no_proxy_env.delenv(var, raising=False)
    untrusted = SeatProxy(lambda hit: None).preflight(timeout=5)
    assert untrusted is not None and untrusted.startswith("tls: "), untrusted
    no_proxy_env.setenv("NODE_EXTRA_CA_CERTS", str(certs["ca"]))
    assert SeatProxy(lambda hit: None).preflight(timeout=5) is None
    assert [r["method"] for r in fake.requests] == ["HEAD"]
    assert fake.requests[0]["bearer"] is None


class _NoDelaySocket(socket.socket):
    def setsockopt(self, *_args):
        pass


def _make_certs(tmp: Path) -> dict:
    import shutil
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("openssl not available to mint a throwaway CA")
    run = lambda *a: subprocess.run(["openssl", *a], cwd=tmp, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1",
        "-subj", "/CN=seatproxy test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
        "-subj", "/CN=api.anthropic.com")
    (tmp / "leaf.ext").write_text("subjectAltName=DNS:api.anthropic.com\nbasicConstraints=CA:FALSE\n"
                                  "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
                                  "authorityKeyIdentifier=keyid,issuer\n")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "leaf.pem", "-days", "1", "-extfile", "leaf.ext")
    return {"ca": tmp / "ca.pem", "leaf": tmp / "leaf.pem", "key": tmp / "leaf.key"}


# --- over the wire: socketpairs always, TCP on 127.0.0.1 where binding is allowed ---------------
#
# The socketpair variant runs the real request handler and the real http.client upstream against the
# fake API's real handler, connected by socketpairs instead of a listening port. The TCP variant
# runs the same through ``start()``; it skips where binding a port is not allowed.

def _can_bind() -> str | None:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return None
    except OSError as e:
        return f"binding 127.0.0.1 is not allowed here ({e})"
    finally:
        s.close()


_NO_BIND = _can_bind()
needs_bind = pytest.mark.skipif(_NO_BIND is not None, reason=_NO_BIND or "")


class _PairSock:
    """A socketpair end that ignores TCP options (http.client sets TCP_NODELAY)."""

    def __init__(self, sock):
        self._sock = sock

    def setsockopt(self, *_args):
        pass

    def __getattr__(self, name):
        return getattr(self._sock, name)


class PairServer:
    """Runs ``handler_cls`` on the server end of a fresh socketpair for every ``connect()``."""

    def __init__(self, handler_cls):
        self.handler_cls = handler_cls
        self.ends: list[socket.socket] = []

    def connect(self, *_args, **_kwargs):
        client, server = socket.socketpair()
        self.ends.append(server)
        threading.Thread(target=self._serve, args=(server,), daemon=True).start()
        return _PairSock(client)

    def _serve(self, sock):
        try:
            self.handler_cls(sock, ("127.0.0.1", 0), self)
        except OSError:
            pass
        finally:
            sock.close()

    def drop_all(self):
        """Close every server end, as an upstream does with idle keep-alive connections."""
        for end in self.ends:
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            end.close()


class Live:
    def __init__(self, proxy, base, fake, client, upstream=None):
        self.proxy, self.base, self.fake, self.client, self.upstream = proxy, base, fake, client, upstream
        self.hits: list[Hit] = []
        self.answers: list = []
        self.logs: list = []

    def post(self, path, body=MSG, headers=None, method="POST"):
        from urllib.parse import urlsplit
        conn = self.client()
        conn.request(method, urlsplit(self.base).path + path, body=body,
                     headers={"content-type": "application/json", "anthropic-beta": "oauth-2025-04-20",
                              "authorization": f"Bearer {CLIENT_TOKEN}", **(headers or {})})
        return conn, conn.getresponse()


@pytest.fixture(params=["socketpair", pytest.param("tcp", marks=needs_bind)])
def live(request, monkeypatch):
    from urllib.parse import urlsplit
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    fake = FA.FakeAnthropic()
    box: dict = {}

    def decide(hit):
        box["live"].hits.append(hit)
        return box["live"].answers.pop(0) if box["live"].answers else None

    def log(event, **fields):
        box["live"].logs.append((event, fields))

    if request.param == "tcp":
        def local_only(address, *args, **kwargs):
            assert address[0] in ("127.0.0.1", "localhost"), address
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        monkeypatch.setattr(socket, "create_connection", local_only)
        srv = FA.serve(fake)
        proxy = SeatProxy(decide, upstream=f"http://127.0.0.1:{srv.server_address[1]}",
                          decide_timeout=2, log=log)
        u = urlsplit(proxy.start())
        base = f"http://127.0.0.1:{u.port}/{proxy._secret}"    # the secret-path transport
        box["live"] = Live(proxy, base, fake, lambda: http.client.HTTPConnection(u.hostname, u.port, timeout=10))
        try:
            yield box["live"]
        finally:
            proxy.stop()
            srv.shutdown()
            srv.server_close()
        return
    upstream = PairServer(FA.make_handler(fake))
    monkeypatch.setattr(socket, "create_connection", upstream.connect)
    proxy = SeatProxy(decide, upstream="http://127.0.0.1:9", decide_timeout=2, log=log)
    downstream = PairServer(SP._make_handler(proxy))

    def client():
        conn = http.client.HTTPConnection("127.0.0.1", 9, timeout=10)
        conn.sock = downstream.connect()
        return conn
    box["live"] = Live(proxy, f"http://127.0.0.1:9/{proxy._secret}", fake, client, upstream)
    try:
        yield box["live"]
    finally:
        downstream.drop_all()
        upstream.drop_all()


def test_wire_limit_failover_and_secret(live):
    live.proxy.bind(A)
    live.fake.update({"status_by_token": {tok(A.token): 429}})
    live.answers.append(B)
    _, resp = live.post("/v1/messages?beta=true")
    assert resp.status == 200 and json.loads(resp.read())["type"] == "message"
    assert [r["bearer"] for r in live.fake.requests] == [tok(A.token), tok(B.token)]
    assert live.proxy.bound() == B
    conn = live.client()
    conn.request("POST", "/v1/messages", body=MSG)
    assert conn.getresponse().status == 403
    assert len(live.fake.requests) == 2


def test_wire_limit_passes_through_verbatim(live):
    live.proxy.bind(A)
    live.fake.update({"status_by_token": {tok(A.token): 429}})
    _, resp = live.post("/v1/messages")
    assert resp.status == 429
    assert resp.headers["anthropic-ratelimit-unified-status"] == "rejected"
    assert json.loads(resp.read())["error"]["type"] == "rate_limit_error"


def test_wire_stream_arrives_incrementally(live):
    live.proxy.bind(B)
    live.fake.update({"chunk_delay": 0.15})
    t0 = time.monotonic()
    _, resp = live.post("/v1/messages", STREAM)
    stamps = [time.monotonic() - t0 for line in resp if line.startswith(b"event: content_block_delta")]
    assert resp.status == 200 and len(stamps) >= 4
    assert stamps[-1] - stamps[0] > 0.3, stamps


def test_wire_chunked_request_body_and_keep_alive(live):
    from urllib.parse import urlsplit
    conn = live.client()
    for _ in range(2):
        conn.request("POST", urlsplit(live.base).path + "/v1/messages", body=iter([MSG[:10], MSG[10:]]),
                     headers={"content-type": "application/json", "transfer-encoding": "chunked"},
                     encode_chunked=True)
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
    assert [r["body_bytes"] for r in live.fake.requests] == [len(MSG), len(MSG)]


def test_wire_handler_exception_leaves_the_proxy_up(live, monkeypatch):
    real = live.proxy._transport.acquire
    monkeypatch.setattr(live.proxy._transport, "acquire",
                        lambda fresh=False: (_ for _ in ()).throw(RuntimeError("boom")))
    _, resp = live.post("/v1/messages")
    assert resp.status == 502 and json.loads(resp.read())["error"]["type"] == "api_error"
    monkeypatch.setattr(live.proxy._transport, "acquire", real)
    _, resp = live.post("/v1/messages")
    assert resp.status == 200


def test_wire_oversized_body_is_refused(live, monkeypatch):
    monkeypatch.setattr(SP, "MAX_BODY", 16)
    _, resp = live.post("/v1/messages")
    assert resp.status == 413 and live.fake.requests == []


def test_wire_stale_upstream_connection_is_replaced(live, monkeypatch):
    if live.upstream is None:
        pytest.skip("needs the socketpair upstream to drop its connections")
    # The proxy pools the connection only after the client has its last byte: wait for that.
    pooled = threading.Event()
    release = live.proxy._transport.release
    monkeypatch.setattr(live.proxy._transport, "release", lambda conn: (release(conn), pooled.set()))
    live.post("/v1/messages")[1].read()
    assert pooled.wait(30)
    assert len(live.proxy._transport._idle) == 1
    live.upstream.drop_all()          # the upstream closed the idle keep-alive connection
    _, resp = live.post("/v1/messages")
    assert resp.status == 200
    assert len(live.fake.requests) == 2


@needs_bind
def test_tcp_stop_closes_the_listener(monkeypatch):
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    proxy = SeatProxy(lambda hit: None, upstream="http://127.0.0.1:9")
    base = proxy.start()
    assert proxy.start() == base
    proxy.stop()
    from urllib.parse import urlsplit
    with pytest.raises(OSError):
        _REAL_CREATE_CONNECTION((urlsplit(base).hostname, urlsplit(base).port), timeout=2)


# --- CONNECT: the HTTPS_PROXY transport (socketpairs only) -------------------------------------

class FakeCA:
    """Stands in for ``sessionca.SessionCA``: a server context for api.anthropic.com from a
    throwaway CA, ``ca_path`` for the client side to trust."""

    def __init__(self, certs: dict):
        self.certs = certs
        self.ca_path = str(certs["ca"])

    def server_context(self):
        ctx = SP.ssl.SSLContext(SP.ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.certs["leaf"], self.certs["key"])
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx


@pytest.fixture(scope="module")
def session_ca(tmp_path_factory):
    return FakeCA(_make_certs(tmp_path_factory.mktemp("session-ca")))


def _proxy_auth(proxy, user=SP.PROXY_USER, secret=None) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{user}:{secret or proxy._secret}".encode()).decode()


def _connect(proxy, target: str, auth: str | None = "ok"):
    """CONNECT ``target`` on a socketpair to the proxy's listener handler: (socket, status, head)."""
    sock = PairServer(SP._make_handler(proxy)).connect()._sock
    sock.settimeout(10)
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if auth is not None:
        lines.append(f"Proxy-Authorization: {_proxy_auth(proxy) if auth == 'ok' else auth}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        data = sock.recv(1)
        if not data:
            break
        head += data
    status = int(head.split()[1]) if head else 0
    return sock, status, head.decode("latin-1")


class FarEnd:
    """Whatever ``socket.create_connection`` reaches: an echo server that records what it got,
    optionally speaking the CONNECT handshake of an upstream proxy first."""

    def __init__(self, monkeypatch, *, proxy_reply: bytes | None = None):
        self.addresses: list = []
        self.connects: list[str] = []
        self.received = b""
        self.proxy_reply = proxy_reply
        monkeypatch.setattr(socket, "create_connection", self.create_connection)

    def create_connection(self, address, *args, **kwargs):
        self.addresses.append(tuple(address))
        a, b = socket.socketpair()
        threading.Thread(target=self._serve, args=(b,), daemon=True).start()
        return a

    def _serve(self, sock):
        with sock:
            if self.proxy_reply is not None:
                head = b""
                while b"\r\n\r\n" not in head:
                    data = sock.recv(1)
                    if not data:
                        return
                    head += data
                self.connects.append(head.decode("latin-1"))
                sock.sendall(self.proxy_reply)
                if b" 200 " not in self.proxy_reply:
                    return
            while data := sock.recv(65536):
                self.received += data
                sock.sendall(data)


def _echo(sock, payload: bytes) -> bytes:
    sock.sendall(payload)
    got = b""
    while len(got) < len(payload):
        data = sock.recv(65536)
        if not data:
            break
        got += data
    return got


@pytest.mark.parametrize("auth", [None, "Basic", "Basic !!notbase64!!", "Bearer whatever",
                                  "wrong-secret", "wrong-user"])
def test_connect_requires_the_proxy_credentials(auth, monkeypatch, session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    far = FarEnd(monkeypatch)
    if auth == "wrong-secret":
        auth = _proxy_auth(proxy, secret="x" + proxy._secret[1:])
    elif auth == "wrong-user":
        auth = _proxy_auth(proxy, user="someone")
    sock, status, head = _connect(proxy, "example.com:443", auth=auth)
    with sock:
        assert status == 407
        assert 'proxy-authenticate: Basic realm="acctsw"' in head
        assert sock.recv(1) == b""        # and the connection is closed
    sock, status, _ = _connect(proxy, "api.anthropic.com:443", auth=auth)
    sock.close()
    assert status == 407
    assert far.addresses == []


def test_start_url_carries_the_proxy_credentials(monkeypatch):
    class Listener:
        server_address = ("127.0.0.1", 4242)

        def __init__(self, *_args):
            pass

        def serve_forever(self, **_kwargs):
            pass

        shutdown = server_close = lambda self: None
    monkeypatch.setattr(SP, "_Server", Listener)
    proxy = SeatProxy(lambda hit: None)
    from urllib.parse import unquote, urlsplit
    u = urlsplit(proxy.start())
    proxy.stop()
    assert (u.scheme, u.hostname, u.port, u.path) == ("http", "127.0.0.1", 4242, "")
    assert proxy._proxy_auth_ok(_proxy_auth(proxy, user=unquote(u.username), secret=unquote(u.password)))


def _tls(sock, ca: FakeCA | None):
    ctx = SP.ssl.create_default_context(cafile=ca.ca_path if ca else None)
    return ctx.wrap_socket(sock, server_hostname="api.anthropic.com")


def _oauth_post(conn, path="/v1/messages?beta=true"):
    conn.request("POST", path, body=MSG, headers={
        "content-type": "application/json", "authorization": f"Bearer {CLIENT_TOKEN}",
        "anthropic-beta": "oauth-2025-04-20"})
    resp = conn.getresponse()
    return resp, resp.read()


def test_connect_to_the_api_is_decrypted_and_injected(session_ca):
    h = Harness()
    h.proxy._ca = session_ca
    h.proxy.bind(B)
    sock, status, _ = _connect(h.proxy, "api.anthropic.com:443")
    assert status == 200
    tls = _tls(sock, session_ca)
    assert tls.selected_alpn_protocol() in (None, "http/1.1")
    conn = http.client.HTTPConnection("api.anthropic.com", 443, timeout=10)
    conn.sock = tls
    for _ in range(2):                           # keep-alive inside the tunnel
        resp, body = _oauth_post(conn)
        assert resp.status == 200 and json.loads(body)["type"] == "message"
    conn.close()
    assert bearers(h) == [tok(B.token), tok(B.token)]
    assert h.t.sent[0]["url"] == "/v1/messages?beta=true"
    assert h.proxy.messages_seen() == 2


def test_connect_to_the_api_limit_replays_inside_the_tunnel(session_ca):
    h = Harness()
    h.proxy._ca = session_ca
    h.proxy.bind(A)
    rules(h, a=429)
    h.answers = [B]
    sock, status, _ = _connect(h.proxy, "API.anthropic.com:443")
    conn = http.client.HTTPConnection("api.anthropic.com", 443, timeout=10)
    conn.sock = _tls(sock, session_ca)
    resp, _ = _oauth_post(conn)
    conn.close()
    assert (status, resp.status) == (200, 200)
    assert bearers(h) == [tok(A.token), tok(B.token)]


@pytest.mark.parametrize("target,address", [("example.com:443", ("example.com", 443)),
                                            ("statsig.anthropic.com:443", ("statsig.anthropic.com", 443)),
                                            ("api.anthropic.com:8443", ("api.anthropic.com", 8443)),
                                            ("[::1]:443", ("::1", 443))])
def test_other_hosts_are_blind_tunnels_even_with_a_ca(target, address, monkeypatch, session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    proxy.bind(B)
    far = FarEnd(monkeypatch)
    sock, status, _ = _connect(proxy, target)
    payload = b"\x16\x03\x01\x00\x05hello, opaque bytes " + CLIENT_TOKEN.encode()
    with sock:
        assert status == 200
        assert _echo(sock, payload) == payload
    assert far.addresses == [address]
    assert far.received == payload             # byte for byte: nothing decrypted, nothing injected


def test_without_a_ca_the_api_is_tunnelled_too(monkeypatch):
    proxy = SeatProxy(lambda hit: None)
    far = FarEnd(monkeypatch)
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    with sock:
        assert status == 200 and _echo(sock, b"opaque") == b"opaque"
    assert far.addresses == [("api.anthropic.com", 443)]


def test_tunnel_chains_through_the_upstream_proxy(monkeypatch, no_proxy_env):
    proxy = SeatProxy(lambda hit: None, upstream_proxy="http://corp%40user:pw@corp.proxy:3128")
    far = FarEnd(monkeypatch, proxy_reply=b"HTTP/1.1 200 Connection established\r\n\r\n")
    sock, status, _ = _connect(proxy, "example.com:443")
    with sock:
        assert status == 200 and _echo(sock, b"through two proxies") == b"through two proxies"
    assert far.addresses == [("corp.proxy", 3128)]
    [connect] = far.connects
    assert connect.startswith("CONNECT example.com:443 HTTP/1.1\r\n")
    assert "Proxy-Authorization: Basic Y29ycEB1c2VyOnB3" in connect    # corp@user:pw


def test_tunnel_honours_the_launchers_no_proxy(monkeypatch, no_proxy_env):
    no_proxy_env.setenv("NO_PROXY", "example.com")
    proxy = SeatProxy(lambda hit: None, upstream_proxy="http://corp.proxy:3128")
    far = FarEnd(monkeypatch)
    sock, status, _ = _connect(proxy, "example.com:443")
    sock.close()
    assert status == 200 and far.addresses == [("example.com", 443)]


def test_upstream_proxy_refusal_answers_502(monkeypatch, no_proxy_env):
    proxy = SeatProxy(lambda hit: None, upstream_proxy="http://corp.proxy:3128")
    far = FarEnd(monkeypatch, proxy_reply=b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n")
    sock, status, _ = _connect(proxy, "example.com:443")
    sock.close()
    assert status == 502 and len(far.connects) == 1


def test_tls_reject_is_reported_once(session_ca):
    rejects = []
    h = Harness()
    h.proxy._ca = session_ca
    h.proxy._on_tls_reject = rejects.append
    seen = threading.Semaphore(0)    # one release per rejected handshake the proxy has handled
    log = h.proxy._log_fn
    h.proxy._log_fn = lambda event, **f: (log(event, **f), event == "tls_reject" and seen.release())
    for _ in range(2):
        sock, status, _ = _connect(h.proxy, "api.anthropic.com:443")
        assert status == 200
        with pytest.raises(SP.ssl.SSLError):
            _tls(sock, None)                     # the client does not trust the session CA
        sock.close()
    assert seen.acquire(timeout=30) and seen.acquire(timeout=30)
    assert len(rejects) == 1 and rejects[0]
    assert h.t.sent == []


def test_decrypted_request_for_another_origin_is_refused():
    h = Harness()
    h.proxy.bind(B)
    wire = Wire()
    keep = h.proxy.handle("POST", "/v1/messages", [
        ("Authorization", f"Bearer {CLIENT_TOKEN}"), ("anthropic-beta", "oauth-2025-04-20")],
        MSG, wire, authority="example.com:443")
    resp = parse(wire.getvalue())
    assert resp.status == 421 and not keep
    assert h.t.sent == [] and h.proxy.messages_seen() == 0


def test_decrypted_request_needs_no_secret_path():
    h = Harness()
    h.proxy.bind(B)
    wire = Wire()
    h.proxy.handle("POST", "/v1/messages", [
        ("Authorization", f"Bearer {CLIENT_TOKEN}"), ("anthropic-beta", "oauth-2025-04-20")],
        MSG, wire, authority="api.anthropic.com:443")
    assert parse(wire.getvalue()).status == 200
    assert bearers(h) == [tok(B.token)]
    wire = Wire()
    h.proxy.handle("GET", "http://api.anthropic.com/v1/models", [], b"", wire,
                   authority="api.anthropic.com:443")
    assert parse(wire.getvalue(), "GET").status == 403


# --- CONNECT over a real listener (skips where binding 127.0.0.1 is not allowed) ---------------

@pytest.fixture
def loopback_only(monkeypatch):
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)

    def local_only(address, *args, **kwargs):
        assert address[0] in ("127.0.0.1", "localhost"), address
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", local_only)


def _tcp_client(url: str, ca: FakeCA | None, *, auth: bool = True):
    """An ``http.client`` HTTPS connection to api.anthropic.com through the proxy at ``url``,
    the way any HTTPS_PROXY-aware client tunnels (CONNECT, then TLS inside)."""
    import base64
    from urllib.parse import unquote, urlsplit
    u = urlsplit(url)
    ctx = SP.ssl.create_default_context(cafile=ca.ca_path if ca else None)
    conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=10, context=ctx)
    headers = {}
    if auth:
        cred = f"{unquote(u.username)}:{unquote(u.password)}".encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    conn.set_tunnel("api.anthropic.com", 443, headers=headers)
    return conn


@needs_bind
def test_tcp_connect_is_decrypted_and_injected(loopback_only, session_ca):
    h = Harness()
    h.proxy._ca = session_ca
    h.proxy.bind(B)
    url = h.proxy.start()
    try:
        conn = _tcp_client(url, session_ca)
        for _ in range(2):
            resp, body = _oauth_post(conn)
            assert resp.status == 200 and json.loads(body)["type"] == "message"
        conn.close()
    finally:
        h.proxy.stop()
    assert bearers(h) == [tok(B.token), tok(B.token)]
    assert h.proxy.messages_seen() == 2


@needs_bind
def test_tcp_connect_without_credentials_gets_407(loopback_only, session_ca):
    h = Harness()
    h.proxy._ca = session_ca
    url = h.proxy.start()
    try:
        conn = _tcp_client(url, session_ca, auth=False)
        with pytest.raises(OSError, match="407"):
            conn.request("GET", "/")
    finally:
        h.proxy.stop()
    assert h.t.sent == []


@needs_bind
def test_tcp_tls_reject_is_reported(loopback_only, session_ca):
    rejects = []
    rejected = threading.Event()
    h = Harness()
    h.proxy._ca = session_ca
    h.proxy._on_tls_reject = lambda reason: (rejects.append(reason), rejected.set())
    url = h.proxy.start()
    try:
        conn = _tcp_client(url, None)    # trusts only the system roots, not the session CA
        with pytest.raises(SP.ssl.SSLError):
            conn.request("GET", "/")
        assert rejected.wait(30)
    finally:
        h.proxy.stop()
    assert len(rejects) == 1 and h.t.sent == []


@needs_bind
def test_tcp_blind_tunnel_to_another_host(loopback_only, session_ca):
    """CONNECT to a real local echo server: bytes pass through untouched, nothing is decrypted."""
    import base64
    from urllib.parse import unquote, urlsplit
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)
    received = []

    def serve():
        c, _ = echo.accept()
        with c:
            while data := c.recv(65536):
                received.append(data)
                c.sendall(data)
    threading.Thread(target=serve, daemon=True).start()
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    proxy.bind(B)
    u = urlsplit(proxy.start())
    try:
        sock = _REAL_CREATE_CONNECTION((u.hostname, u.port), timeout=10)
        cred = base64.b64encode(f"{unquote(u.username)}:{unquote(u.password)}".encode()).decode()
        target = f"127.0.0.1:{echo.getsockname()[1]}"
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                     f"Proxy-Authorization: Basic {cred}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        assert head.startswith(b"HTTP/1.1 200 ")
        payload = b"\x16\x03\x01 not decrypted " + CLIENT_TOKEN.encode()
        assert _echo(sock, payload) == payload
        sock.close()
    finally:
        proxy.stop()
        echo.close()
    assert b"".join(received) == payload


# --- round 3: what a TLS failure means, counters -----------------------------------------------

class _FailingCtx:
    """A server context whose handshake fails with ``exc``."""

    def __init__(self, exc):
        self.exc = exc

    def wrap_socket(self, sock, server_side):
        raise self.exc


def _ssl_error(reason):
    e = SP.ssl.SSLError(1, f"[SSL: {reason}] handshake failed")
    e.reason = reason
    return e


def _handshake_outcome(proxy, events=("tls_reject", "tls_handshake_failed")):
    """CONNECT to the API and wait until the proxy has logged how the handshake ended."""
    done = threading.Semaphore(0)
    log = proxy._log_fn
    proxy._log_fn = lambda event, **f: (log(event, **f), event in events and done.release())
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    sock.close()
    assert status == 200 and done.acquire(timeout=10)


@pytest.mark.parametrize("reason", ["TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_BAD_CERTIFICATE",
                                    "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
                                    "SSLV3_ALERT_UNSUPPORTED_CERTIFICATE", "TLSV1_ALERT_ACCESS_DENIED"])
def test_certificate_alerts_are_a_tls_reject(reason):
    rejects = []
    ca = SimpleCA(_FailingCtx(_ssl_error(reason)))
    proxy = SeatProxy(lambda hit: None, ca=ca, on_tls_reject=rejects.append)
    _handshake_outcome(proxy)
    assert rejects == [reason] and proxy.intercepts_ok() == 0


@pytest.mark.parametrize("exc", [SP.ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                                 _ssl_error("HTTP_REQUEST"), _ssl_error("WRONG_VERSION_NUMBER")])
def test_other_handshake_failures_are_not_a_tls_reject(exc):
    rejects, logs = [], []
    proxy = SeatProxy(lambda hit: None, ca=SimpleCA(_FailingCtx(exc)), on_tls_reject=rejects.append,
                      log=lambda event, **f: logs.append(event))
    _handshake_outcome(proxy)
    assert rejects == [] and "tls_handshake_failed" in logs and "tls_reject" not in logs


@pytest.mark.parametrize("first", [b"GET / HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n", b""])
def test_plain_http_or_a_hang_up_after_connect_is_not_a_tls_reject(first, session_ca):
    rejects = []
    proxy = SeatProxy(lambda hit: None, ca=session_ca, on_tls_reject=rejects.append)
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    with sock:
        assert status == 200
        if first:
            sock.sendall(first)
        else:
            sock.shutdown(socket.SHUT_WR)
        while sock.recv(65536):    # the proxy gives up on the handshake and closes
            pass
    assert rejects == [] and proxy.intercepts_ok() == 0


class SimpleCA:
    def __init__(self, ctx):
        self.ctx = ctx

    def server_context(self):
        return self.ctx


def test_intercepts_ok_counts_completed_handshakes_only(session_ca):
    h = Harness()
    h.proxy._ca = session_ca
    for _ in range(2):
        sock, status, _ = _connect(h.proxy, "api.anthropic.com:443")
        conn = http.client.HTTPConnection("api.anthropic.com", 443, timeout=10)
        conn.sock = _tls(sock, session_ca)
        resp, _ = _oauth_post(conn)
        conn.close()
        assert (status, resp.status) == (200, 200)
    sock, _, _ = _connect(h.proxy, "api.anthropic.com:443")
    with pytest.raises(SP.ssl.SSLError):
        _tls(sock, None)
    sock.close()
    assert h.proxy.intercepts_ok() == 2


def test_upstream_failures_count_in_a_row_and_reset_on_an_answer():
    h = Harness()
    h.t.fail = [OSError("connection refused"), SP.ssl.SSLError(1, "certificate verify failed")]
    assert [h.send()[0].status for _ in range(2)] == [502, 502]
    assert h.proxy.upstream_failures() == 2
    h.send()
    assert h.proxy.upstream_failures() == 0


def test_upstream_failures_ignore_a_stale_pooled_retry_and_failures_after_the_send():
    h = Harness()
    h.t.idle.append(FakeConn(h.t, True))
    h.t.fail = [OSError("stale"), OSError("refused")]    # the pooled socket, then the fresh one
    assert h.send()[0].status == 502
    assert h.proxy.upstream_failures() == 1
    h.send()
    h.t.fail_response = [ConnectionResetError("reset while waiting for the answer")]
    assert h.send()[0].status == 502
    assert h.proxy.upstream_failures() == 0    # it was reached: not a connect failure


# --- round 3: WebSocket upgrades inside the intercepted tunnel --------------------------------

WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="


class WsUpstream:
    """api.anthropic.com for an upgrade, over TLS from the session CA: reads one request head byte
    by byte (bytes sent with it stay unread), answers ``reply`` in one write (one TLS record), then
    echoes with an ``echo:`` prefix until the far side closes."""

    def __init__(self, monkeypatch, ca, reply: bytes):
        self.ca = ca
        self.reply = reply
        self.heads: list[str] = []
        self.received = b""
        self.addresses: list = []
        monkeypatch.setattr(socket, "create_connection", self.create_connection)

    def create_connection(self, address, *args, **kwargs):
        self.addresses.append(tuple(address))
        a, b = socket.socketpair()
        threading.Thread(target=self._serve, args=(b,), daemon=True).start()
        return a

    def _serve(self, raw):
        try:
            with self.ca.server_context().wrap_socket(raw, server_side=True) as sock:
                head = b""
                while b"\r\n\r\n" not in head:
                    data = sock.recv(1)
                    if not data:
                        return
                    head += data
                self.heads.append(head.decode("latin-1"))
                sock.sendall(self.reply)
                while data := sock.recv(65536):
                    self.received += data
                    sock.sendall(b"echo:" + data)
        except OSError:
            pass


def _ws_proxy(session_ca, **kwargs):
    proxy = SeatProxy(lambda hit: None, ca=session_ca, **kwargs)
    proxy._transport.ssl = SP.ssl.create_default_context(cafile=session_ca.ca_path)
    proxy.bind(B)
    return proxy


def _ws_open(proxy, session_ca, target="/api/ws/speech_to_text/voice_stream?language=en", early=b""):
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    assert status == 200
    tls = _tls(sock, session_ca)
    head = [f"GET {target} HTTP/1.1", "Host: api.anthropic.com", "Connection: Upgrade",
            "Upgrade: websocket", "Sec-WebSocket-Version: 13", f"Sec-WebSocket-Key: {WS_KEY}",
            f"Authorization: Bearer {CLIENT_TOKEN}", "anthropic-beta: oauth-2025-04-20",
            "Proxy-Authorization: Basic c3RyYXk6aGVhZGVy"]
    tls.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + early)    # one TLS record
    return tls


def _read_until(sock, marker: bytes) -> bytes:
    """What arrives until ``marker`` or the end of the stream (a TLS read on a socketpair the far end
    closed can end in EPIPE instead of EOF)."""
    got = b""
    while marker not in got:
        try:
            data = sock.recv(65536)
        except (BrokenPipeError, ConnectionResetError):
            break
        if not data:
            break
        got += data
    return got


SWITCH = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
          b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")


def test_websocket_upgrade_is_relayed_verbatim_and_spliced(monkeypatch, no_proxy_env, session_ca):
    up = WsUpstream(monkeypatch, session_ca, SWITCH + b"first-frame")
    proxy = _ws_proxy(session_ca)
    proxy._upstream_failures = 2                   # an answered upgrade resets the count
    with _ws_open(proxy, session_ca, early=b"early-frame") as tls:
        got = _read_until(tls, b"echo:early-frame")
        head, _, rest = got.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101 ")
        for line in (b"Upgrade: websocket", b"Connection: Upgrade", b"Sec-WebSocket-Accept: "):
            assert line in head
        # Bytes the TLS layer had already decrypted on either side: copied before select() waits.
        assert rest == b"first-frame" + b"echo:early-frame"
        tls.sendall(b"later")
        assert _read_until(tls, b"echo:later").endswith(b"echo:later")
    [sent] = up.heads
    assert sent.startswith("GET /api/ws/speech_to_text/voice_stream?language=en HTTP/1.1\r\n")
    for line in ("Host: api.anthropic.com", "Connection: Upgrade", "Upgrade: websocket",
                 f"Sec-WebSocket-Key: {WS_KEY}", f"Authorization: Bearer {CLIENT_TOKEN}"):
        assert line + "\r\n" in sent
    assert "proxy-authorization" not in sent.lower()
    assert B.token not in sent                     # an upgrade never carries a seat token
    assert up.addresses == [("api.anthropic.com", 443)]
    assert proxy.messages_seen() == 0 and proxy.upstream_failures() == 0
    assert proxy.upstream_successes() == 1


def test_a_refused_upgrade_is_relayed_and_the_connection_closes(monkeypatch, no_proxy_env, session_ca):
    body = b'{"type":"error","error":{"type":"permission_error"}}'
    up = WsUpstream(monkeypatch, session_ca, b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
                    b"Connection: keep-alive\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                    + body)
    proxy = _ws_proxy(session_ca)
    with _ws_open(proxy, session_ca) as tls:
        got = _read_until(tls, b"\x00never")      # until the proxy closes: the upstream does not
    head, _, rest = got.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403 ") and rest == body
    assert b"keep-alive" not in head.lower()
    assert len(up.heads) == 1


@pytest.mark.parametrize("failure", ["refused", "untrusted"])
def test_an_unreachable_upgrade_upstream_answers_502_and_counts(failure, monkeypatch, no_proxy_env,
                                                                 session_ca):
    up = WsUpstream(monkeypatch, session_ca, SWITCH)
    proxy = _ws_proxy(session_ca)
    if failure == "refused":
        def refuse(*_args, **_kwargs):
            raise ConnectionRefusedError("refused")
        monkeypatch.setattr(socket, "create_connection", refuse)
    else:
        proxy._transport.ssl = SP.ssl.create_default_context()    # does not trust the fake API
    with _ws_open(proxy, session_ca) as tls:
        got = _read_until(tls, b"\x00never")
    assert got.startswith(b"HTTP/1.1 502 ")
    assert proxy.upstream_failures() == 1 and up.heads == []


def test_an_upgrade_needs_an_origin_form_target(monkeypatch, no_proxy_env, session_ca):
    up = WsUpstream(monkeypatch, session_ca, SWITCH)
    proxy = _ws_proxy(session_ca)
    with _ws_open(proxy, session_ca, target="http://api.anthropic.com/api/ws") as tls:
        got = _read_until(tls, b"\x00never")
    assert got.startswith(b"HTTP/1.1 403 ") and up.addresses == []


# --- round 3: tunnels ----------------------------------------------------------------------------

def test_connect_200_has_no_content_length(monkeypatch, session_ca):
    FarEnd(monkeypatch)
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    for target in ("example.com:443", "api.anthropic.com:443"):
        sock, status, head = _connect(proxy, target)
        sock.close()
        assert status == 200 and "content-length" not in head.lower()


def test_a_quiet_tunnel_is_not_cut(monkeypatch):
    monkeypatch.setattr(SP, "CLIENT_IDLE_TIMEOUT", 0.1)
    FarEnd(monkeypatch)
    proxy = SeatProxy(lambda hit: None)
    sock, status, _ = _connect(proxy, "example.com:443")
    with sock:
        assert status == 200
        time.sleep(0.6)                   # several idle timeouts of a keep-alive client connection
        assert _echo(sock, b"still here") == b"still here"


def test_a_tunnel_peer_that_stops_reading_ends_the_tunnel(monkeypatch):
    monkeypatch.setattr(SP, "TUNNEL_SEND_TIMEOUT", 0.3)
    far_ends = []

    def create_connection(address, *args, **kwargs):
        a, b = socket.socketpair()
        far_ends.append(b)
        return a
    monkeypatch.setattr(socket, "create_connection", create_connection)
    proxy = SeatProxy(lambda hit: None)
    sock, status, _ = _connect(proxy, "example.com:443")    # and never reads again
    with sock:
        assert status == 200
        far = far_ends[0]
        far.settimeout(10)
        with pytest.raises((BrokenPipeError, ConnectionResetError)):
            for _ in range(100000):
                far.sendall(b"x" * 65536)
        far.close()


def test_upstream_proxy_connect_brackets_ipv6_literals(monkeypatch, no_proxy_env):
    proxy = SeatProxy(lambda hit: None, upstream_proxy="http://corp.proxy:3128")
    far = FarEnd(monkeypatch, proxy_reply=b"HTTP/1.1 200 Connection established\r\n\r\n")
    sock, status, _ = _connect(proxy, "[2001:db8::1]:443")
    with sock:
        assert status == 200 and _echo(sock, b"v6") == b"v6"
    [connect] = far.connects
    assert connect.startswith("CONNECT [2001:db8::1]:443 HTTP/1.1\r\nHost: [2001:db8::1]:443\r\n")


def test_the_listener_queues_a_burst_of_connections():
    assert SP._Server.request_queue_size >= 128


@pytest.mark.parametrize("closing", ["tls", "plain"])
def test_a_tls_splice_ends_both_ways_when_either_side_ends(closing, session_ca):
    """TLS cannot be half-closed here (SSLSocket.shutdown drops the TLS layer), so the end of either
    side ends the splice, even while the other side stays open."""
    c_user, c_proxy = socket.socketpair()
    f_proxy, f_user = socket.socketpair()
    box = {}
    server = threading.Thread(target=lambda: box.update(
        tls=session_ca.server_context().wrap_socket(c_proxy, server_side=True)), daemon=True)
    server.start()
    client = _tls(c_user, session_ca)
    server.join(10)
    pump = threading.Thread(target=SP._pump, args=(box["tls"], f_proxy), daemon=True)
    pump.start()
    try:
        client.sendall(b"ping")
        f_user.settimeout(10)
        assert f_user.recv(4) == b"ping"
        if closing == "tls":
            client.close()
        else:
            f_user.shutdown(socket.SHUT_WR)
        pump.join(5)
        assert not pump.is_alive()
    finally:
        for s in (client, box["tls"], f_proxy, f_user):
            s.close()


# --- a client that rejects the CA by hanging up (Claude Code's Bun sends no alert) -------------

def _watch(proxy):
    """A semaphore released once per handshake outcome the proxy logs, and the rejects it reports."""
    outcomes, rejects = threading.Semaphore(0), []
    proxy._on_tls_reject = rejects.append
    log = proxy._log_fn
    proxy._log_fn = lambda event, **f: (log(event, **f),
                                        event in ("tls_handshake_failed", "intercept") and outcomes.release())
    return outcomes, rejects


def _hello_then_hang_up(proxy, outcomes):
    """Read the server's first flight (ServerHello onwards), then close without an alert."""
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    with sock:
        assert status == 200
        incoming, outgoing = SP.ssl.MemoryBIO(), SP.ssl.MemoryBIO()
        tls = SP.ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname="api.anthropic.com")
        with pytest.raises(SP.ssl.SSLWantReadError):
            tls.do_handshake()
        sock.sendall(outgoing.read())
        assert sock.recv(65536)[:1] == b"\x16"     # a TLS handshake record: the ServerHello
    assert outcomes.acquire(timeout=10)


def _complete_a_handshake(proxy, outcomes, session_ca):
    sock, status, _ = _connect(proxy, "api.anthropic.com:443")
    _tls(sock, session_ca).close()
    assert status == 200 and outcomes.acquire(timeout=10)


@pytest.mark.parametrize("hang_ups,fired", [(2, False), (3, True), (5, True)])
def test_hang_ups_mid_handshake_count_as_a_reject(hang_ups, fired, session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    outcomes, rejects = _watch(proxy)
    for _ in range(hang_ups):
        _hello_then_hang_up(proxy, outcomes)
    assert rejects == (["eof x3"] if fired else [])


def test_hang_ups_spread_beyond_the_window_do_not_count(monkeypatch, session_ca):
    monkeypatch.setattr(SP, "EOF_REJECT_WINDOW", 0.3)
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    outcomes, rejects = _watch(proxy)
    for _ in range(2):
        _hello_then_hang_up(proxy, outcomes)
    time.sleep(0.5)
    _hello_then_hang_up(proxy, outcomes)
    assert rejects == []
    _hello_then_hang_up(proxy, outcomes)
    _hello_then_hang_up(proxy, outcomes)
    assert rejects == ["eof x3"]


def test_a_completed_handshake_breaks_the_hang_up_streak(session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    outcomes, rejects = _watch(proxy)
    for _ in range(2):
        _hello_then_hang_up(proxy, outcomes)
    _complete_a_handshake(proxy, outcomes, session_ca)
    _hello_then_hang_up(proxy, outcomes)
    assert rejects == [] and proxy.intercepts_ok() == 1


def test_after_a_completed_handshake_hang_ups_never_count(session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    outcomes, rejects = _watch(proxy)
    _complete_a_handshake(proxy, outcomes, session_ca)
    for _ in range(4):
        _hello_then_hang_up(proxy, outcomes)
    assert rejects == []


def test_repeated_plain_http_is_never_a_reject(session_ca):
    proxy = SeatProxy(lambda hit: None, ca=session_ca)
    outcomes, rejects = _watch(proxy)
    for _ in range(4):
        sock, status, _ = _connect(proxy, "api.anthropic.com:443")
        with sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n")
            assert outcomes.acquire(timeout=10)
    assert rejects == []


def test_upstream_successes_count_every_answer_and_nothing_else():
    h = Harness()
    h.t.fail = [OSError("connection refused")]
    assert h.send()[0].status == 502
    assert h.proxy.upstream_successes() == 0
    rules(h, client=429)                                  # an error status is still an answer
    statuses = [h.send()[0].status for _ in range(2)]
    assert statuses == [429, 429] and h.proxy.upstream_successes() == 2
    h.t.fail_response = [ConnectionResetError("reset while waiting for the answer")]
    assert h.send()[0].status == 502
    assert h.proxy.upstream_successes() == 2


@pytest.mark.parametrize("resets,fired", [(2, False), (3, True)])
def test_resets_mid_handshake_count_like_hang_ups(resets, fired):
    rejects, outcomes = [], threading.Semaphore(0)
    ca = SimpleCA(_FailingCtx(ConnectionResetError(54, "Connection reset by peer")))
    proxy = SeatProxy(lambda hit: None, ca=ca, on_tls_reject=rejects.append,
                      log=lambda event, **f: event == "tls_handshake_failed" and outcomes.release())
    for _ in range(resets):
        sock, status, _ = _connect(proxy, "api.anthropic.com:443")
        sock.close()
        assert status == 200 and outcomes.acquire(timeout=10)
    assert rejects == (["eof x3"] if fired else [])
