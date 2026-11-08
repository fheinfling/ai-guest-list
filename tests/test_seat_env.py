"""Engine entry points undo only the proxy/CA environment installed by SeatSwap."""
import json
from types import SimpleNamespace

import pytest

from acctsw import cli, procenv, seatswap
from app import menubar

PROXY = "http://acctsw:seat-secret@127.0.0.1:50024"
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def seat_env(base):
    swap = object.__new__(seatswap.SeatSwap)
    swap.proxy_url = PROXY
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
