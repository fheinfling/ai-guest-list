"""Socket tests must skip when the sandbox permits bind but denies connect."""
import socket
import threading
from types import SimpleNamespace

import pytest

from tests import test_codexhome as homes
from tests import test_seatproxy as proxy_tests


@pytest.mark.parametrize("family", ["tcp", "unix"])
@pytest.mark.parametrize("denied", [None, "socket", "bind", "listen", "connect", "accept"])
def test_socket_probe_checks_connections_and_closes_every_socket(monkeypatch, tmp_path, family, denied):
    calls, opened, closed = [], [], []

    class FakeSocket:
        def __init__(self, *_args):
            self.step("socket")
            opened.append(self)
        def step(self, operation):
            calls.append(operation)
            if operation == denied:
                raise PermissionError("sandbox denies operation")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()
        def close(self):
            closed.append(self)
        def settimeout(self, value):
            assert value > 0
        def bind(self, address):
            self.step("bind")
        def listen(self, backlog):
            self.step("listen")
        def connect(self, address):
            self.step("connect")
        def getsockname(self):
            return ("127.0.0.1", 12345)
        def accept(self):
            self.step("accept")
            return FakeSocket(), None

    monkeypatch.setattr(socket, "socket", FakeSocket)
    if family == "tcp":
        assert (proxy_tests._can_bind() is not None) is (denied is not None)
    elif denied:
        with pytest.raises(pytest.skip.Exception):
            homes._require_unix_connections(tmp_path / "probe")
    else:
        homes._require_unix_connections(tmp_path / "probe")
    if denied is None:
        assert calls == ["socket", "socket", "bind", "listen", "connect", "accept", "socket"]
    else:
        assert calls[-1] == denied
    assert set(closed) == set(opened)
    assert len(closed) == len(opened)


def test_short_home_checks_unix_connections(monkeypatch):
    class ProbeCalled(Exception):
        pass
    def probe(target):
        assert target.name == "probe"
        raise ProbeCalled
    monkeypatch.setattr(homes, "_require_unix_connections", probe)
    fixture = homes.short_home.__wrapped__(monkeypatch)
    try:
        with pytest.raises(ProbeCalled):
            next(fixture)
    finally:
        fixture.close()


def test_blind_tunnel_joins_echo_thread_on_early_connection_failure(monkeypatch):
    closed, accepting = threading.Event(), threading.Event()
    errors, threads, joined = [], [], []
    real_thread = threading.Thread

    class Echo:
        def bind(self, address):
            pass
        def listen(self, backlog):
            pass
        def settimeout(self, value):
            pass
        def accept(self):
            accepting.set()
            assert closed.wait(2)
            raise ConnectionAbortedError("listener closed")
        def close(self):
            closed.set()

    def thread(*, target, **kwargs):
        def run():
            try:
                target()
            except BaseException as exc:
                errors.append(exc)
        worker = real_thread(target=run, **kwargs)
        real_join = worker.join
        def join(*args, **kwargs):
            joined.append(worker)
            return real_join(*args, **kwargs)
        worker.join = join
        threads.append(worker)
        return worker

    def denied(*_args, **_kwargs):
        assert accepting.wait(2)
        raise PermissionError("connect denied")

    monkeypatch.setattr(proxy_tests.socket, "socket", Echo)
    monkeypatch.setattr(proxy_tests.threading, "Thread", thread)
    monkeypatch.setattr(proxy_tests, "_REAL_CREATE_CONNECTION", denied)
    monkeypatch.setattr(proxy_tests, "SeatProxy", lambda *_args, **_kwargs: SimpleNamespace(
        bind=lambda _: None, start=lambda: "http://key:secret@127.0.0.1:12345",
        stop=lambda: None))
    try:
        with pytest.raises(PermissionError, match="connect denied"):
            proxy_tests.test_tcp_blind_tunnel_to_another_host(None, None)
    finally:
        closed.set()
        for worker in threads:
            real_thread.join(worker, timeout=2)
    assert len(threads) == 1 and not threads[0].is_alive()
    assert joined == threads
    assert errors == []
