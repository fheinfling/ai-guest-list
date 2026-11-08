"""Engine entry points undo only the proxy/CA environment installed by SeatSwap."""
import json
from types import SimpleNamespace

import pytest

from acctsw import cli, procenv, seatswap
from app import menubar

PROXY = "http://acctsw:seat-secret@127.0.0.1:50024"
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def seat_env(base, *, proxy=PROXY):
    swap = object.__new__(seatswap.SeatSwap)
    swap.proxy_url = proxy
    swap.ca = SimpleNamespace(bundle_paths=lambda env: {
        key: f"/tmp/acctsw-ca-session/{key}.pem" for key in CA_VARS})
    return swap.env(base)


@pytest.mark.parametrize("base", [{"PATH": "/bin"}, {
    "PATH": "/bin", "HTTPS_PROXY": "http://user:pw@proxy.corp:8080",
    "https_proxy": "", "NO_PROXY": "api.anthropic.com,internal.corp",
    "no_proxy": "lower.corp", **{key: f"/corp/{key}.pem" for key in CA_VARS},
}])
def test_marker_round_trip_restores_exact_user_environment(base):
    inherited = seat_env(base)
    assert "ACCTSW_SEAT_ENV" in inherited
    assert "seat-secret" not in inherited["ACCTSW_SEAT_ENV"]
    assert procenv.shed_seat_env(inherited) is True
    assert inherited == base
    assert procenv.shed_seat_env(inherited) is False


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "NO_PROXY", *CA_VARS])
def test_changed_current_values_survive_marker_cleanup(key):
    inherited = seat_env({})
    inherited[key] = "/bundle/openssl.ca/cert.pem" if key in CA_VARS else "user-changed"
    procenv.shed_seat_env(inherited)
    assert inherited == {key: "/bundle/openssl.ca/cert.pem" if key in CA_VARS else "user-changed"}


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_legacy_cleanup_is_structural_and_preserves_user_ca_and_no_proxy(host):
    proxy = f"http://acctsw:legacy-secret@{host}:50024"
    inherited = {key: proxy for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")}
    inherited.update({"NO_PROXY": "127.0.0.1,localhost", "SSL_CERT_FILE": "/bundle/openssl.ca/cert.pem",
                      "NODE_EXTRA_CA_CERTS": "/tmp/acctsw-ca-old/node.pem",
                      "REQUESTS_CA_BUNDLE": "/tmp/acctsw-ca-old/full.pem",
                      "CURL_CA_BUNDLE": "/tmp/acctsw-ca-old/full.pem"})
    assert procenv.shed_seat_env(inherited) is True
    assert inherited == {"NO_PROXY": "127.0.0.1,localhost", "SSL_CERT_FILE": "/bundle/openssl.ca/cert.pem"}


@pytest.mark.parametrize("proxy", ["http://proxy.corp:8080", "http://user:pw@10.0.0.5:3128",
    "http://acctsw:pw@proxy.corp:8080", "https://acctsw:pw@127.0.0.1:50024",
    "http://user:pw@127.0.0.1:50024", "http://acctsw:pw@127.0.0.1.evil.test:50024",
    "http://acctsw@[::1", "not a URL"])
def test_legacy_cleanup_keeps_other_proxies_and_user_certificates(proxy):
    original = {"HTTPS_PROXY": proxy, "SSL_CERT_FILE": "/corp/cert.pem"}
    inherited = dict(original)
    assert procenv.shed_seat_env(inherited) is False
    assert inherited == original


def test_legacy_cleanup_keeps_unrelated_all_proxy():
    inherited = {"HTTPS_PROXY": PROXY, "ALL_PROXY": "socks5://user:pw@localhost:9000"}
    procenv.shed_seat_env(inherited)
    assert inherited == {"ALL_PROXY": "socks5://user:pw@localhost:9000"}


@pytest.mark.parametrize("marker", ["{", "null", "[]", json.dumps({"PATH": {"original": None}})])
def test_malformed_marker_never_breaks_startup_or_changes_unrelated_variables(marker):
    inherited = {"ACCTSW_SEAT_ENV": marker, "PATH": "/bin", "HTTPS_PROXY": PROXY}
    procenv.shed_seat_env(inherited)
    assert inherited == {"PATH": "/bin"}


def test_cli_sheds_before_context_creation(ctx, monkeypatch):
    inherited = seat_env({"PATH": "/bin", "HTTPS_PROXY": "http://proxy.corp:8080"})
    monkeypatch.setattr(cli.os, "environ", inherited)

    def default():
        assert inherited == {"PATH": "/bin", "HTTPS_PROXY": "http://proxy.corp:8080"}
        return ctx

    monkeypatch.setattr(cli.Context, "default", default)
    assert cli.main(["list"]) == 0


def test_app_sheds_before_even_checking_native_dependencies(monkeypatch):
    inherited = seat_env({"PATH": "/bin"})
    monkeypatch.setattr(menubar.os, "environ", inherited)
    monkeypatch.setattr(menubar, "objc", None)
    assert menubar.main() == 1
    assert inherited == {"PATH": "/bin"}


@pytest.mark.parametrize("legacy", [False, True])
def test_upstream_proxy_never_chains_to_an_inherited_seat_proxy(legacy):
    inherited = ({"HTTPS_PROXY": PROXY} if legacy
                 else seat_env({"HTTPS_PROXY": "http://proxy.corp:8080"}))
    before = dict(inherited)
    assert seatswap._upstream_proxy(inherited) == (None if legacy else "http://proxy.corp:8080")
    assert inherited == before


def test_update_helper_gets_explicitly_shed_environment(monkeypatch):
    inherited = seat_env({"HTTPS_PROXY": "http://proxy.corp:8080", "PYTHONHOME": "/bundle/python"})
    monkeypatch.setattr(menubar.os, "environ", inherited)
    seen = []
    monkeypatch.setattr(menubar.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    log = object()
    menubar.spawn_update_helper(["/bin/sh", "helper.sh"], log)
    (argv, kw), = seen
    assert argv == ["/bin/sh", "helper.sh"]
    assert kw["env"] == {"HTTPS_PROXY": "http://proxy.corp:8080", "DO_NOT_TRACK": "1"}
    assert kw["start_new_session"] is True and kw["close_fds"] is True
    assert kw["stdout"] is kw["stderr"] is log
    assert inherited["HTTPS_PROXY"] == PROXY


def test_nested_child_sheds_back_to_the_users_proxy():
    base = {"PATH": "/bin", "HTTPS_PROXY": "http://proxy.corp:8080",
            "NO_PROXY": "internal.corp", "SSL_CERT_FILE": "/corp/ca.pem"}
    outer = seat_env(base)
    before = dict(outer)
    inner_proxy = "http://acctsw:inner-secret@127.0.0.1:50025"
    child = seat_env(outer, proxy=inner_proxy)
    assert child["HTTPS_PROXY"] == child["https_proxy"] == inner_proxy
    assert procenv.shed_seat_env(child) is True
    assert child == base
    assert outer == before


def test_child_no_proxy_keeps_user_hosts_and_loopback_but_routes_anthropic():
    child = seat_env({"NO_PROXY": "internal.corp,api.anthropic.com,.anthropic.com",
                      "no_proxy": "lower.corp,*.anthropic.com,API.Anthropic.COM:443"})
    expected = {"internal.corp", "lower.corp", "127.0.0.1", "localhost"}
    for key in ("NO_PROXY", "no_proxy"):
        assert set(child[key].split(",")) == expected


def test_nested_swap_reuses_preflight_verdict_for_the_users_environment(ctx):
    base = {"HTTPS_PROXY": "http://proxy.corp:8080", "NO_PROXY": "internal.corp",
            "no_proxy": "lower.corp", "SSL_CERT_FILE": "/corp/full.pem",
            "NODE_EXTRA_CA_CERTS": "/corp/node.pem"}
    calls = []

    def preflight():
        calls.append(True)
        assert len(calls) == 1, "nested sessions must reuse the clean environment's verdict"
        return None

    def proxy():
        return SimpleNamespace(start=lambda: PROXY, stop=lambda: None, preflight=preflight)

    clean = seatswap.SeatSwap(ctx, proxy=proxy(), environ=base)
    try:
        assert clean.preflight() is None
        inherited = seat_env(base)
        before = dict(inherited)
        nested = seatswap.SeatSwap(ctx, proxy=proxy(), environ=inherited)
        try:
            assert nested.upstream_proxy == base["HTTPS_PROXY"]
            assert nested.preflight() is None
            assert calls == [True]
            assert inherited == before
        finally:
            nested.stop()
    finally:
        clean.stop()


@pytest.mark.parametrize("environ, expected", [
    ({}, False),
    ({"PATH": "/bin", "HTTPS_PROXY": "http://proxy.corp:8080"}, False),
    ({"ACCTSW_SEAT_ENV": "{}"}, True),
    ({"HTTPS_PROXY": PROXY}, True),
    ({"HTTPS_PROXY": PROXY, "ALL_PROXY": PROXY}, True),
    ({"https_proxy": PROXY, "all_proxy": PROXY}, True),
    *[({key: f"/tmp/acctsw-ca-legacy/{key}.pem"}, True) for key in CA_VARS],
])
def test_shed_return_value_reports_whether_the_environment_changed(environ, expected):
    before = dict(environ)
    assert procenv.shed_seat_env(environ) is expected
    assert (environ != before) is expected
    assert procenv.shed_seat_env(environ) is False


@pytest.mark.parametrize("warning,key", [
    ({"kind": "sessions", "severity": "soft", "text": "older but safe", "pids": [7]}, None),
    ({"kind": "sessions", "severity": "hard", "text": "started 19:23", "pids": [7, 9]},
     ("sessions", (7, 9))),
    ({"kind": "sessions", "text": "no severity from an older bridge"},
     ("sessions", "no severity from an older bridge")),
    ({"kind": "app", "severity": "hard", "text": "out of date"}, ("app", "out of date")),
    ({"kind": "wrapper", "severity": "hard", "text": ""}, None),
    (None, None),
])
def test_only_hard_drift_warnings_toast_and_sessions_dedupe_on_their_terminals(warning, key):
    assert menubar.engine_warning_toast_key(warning) == key


def test_a_hopping_session_does_not_retoast_when_its_start_time_moves():
    first = {"kind": "sessions", "severity": "hard", "pids": [7], "text": "started 19:23"}
    later = {**first, "text": "started 19:41"}
    assert menubar.engine_warning_toast_key(first) == menubar.engine_warning_toast_key(later)
