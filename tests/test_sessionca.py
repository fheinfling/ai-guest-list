import os
import socket
import ssl
import stat
import subprocess
import threading

import pytest

from acctsw.sessionca import SessionCA, SessionCAError


@pytest.fixture
def ca(tmp_path):
    c = SessionCA(tmp_root=tmp_path)
    yield c
    c.close()


def _walk(root):
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            yield os.path.join(dp, n)


def _handshake(ca, hostname):
    client_ctx = ssl.create_default_context(cafile=None, cadata=ca.ca_pem())
    client_ctx.set_alpn_protocols(["h2", "http/1.1"])
    a, b = socket.socketpair()
    res = {}

    def serve():
        try:
            with ca.server_context().wrap_socket(b, server_side=True) as s:
                res["alpn"] = s.selected_alpn_protocol()
        except (ssl.SSLError, OSError) as e:
            res["server_err"] = e

    t = threading.Thread(target=serve)
    t.start()
    try:
        with client_ctx.wrap_socket(a, server_hostname=hostname) as c:
            return c.selected_alpn_protocol(), None
    except ssl.SSLError as e:
        return None, e
    finally:
        a.close()
        t.join(10)
        b.close()


def _text(ca, *args):
    pem = ca.ca_pem()
    return subprocess.run(["/usr/bin/openssl", "x509", "-noout", *args], input=pem, text=True,
                          capture_output=True, check=True).stdout


def test_handshake_ok_with_http11_alpn(ca):
    alpn, err = _handshake(ca, "api.anthropic.com")
    assert err is None
    assert alpn == "http/1.1"


def test_handshake_fails_for_other_host(ca):
    alpn, err = _handshake(ca, "example.com")
    assert err is not None


def test_leaf_san_and_validity(ca):
    a, b = socket.socketpair()

    def serve():
        try:
            with ca.server_context().wrap_socket(b, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    t = threading.Thread(target=serve)
    t.start()
    ctx = ssl.create_default_context(cadata=ca.ca_pem())
    with ctx.wrap_socket(a, server_hostname="api.anthropic.com") as c:
        cert = c.getpeercert()
    t.join(10)
    b.close()
    assert ("DNS", "api.anthropic.com") in cert["subjectAltName"]
    days = (ssl.cert_time_to_seconds(cert["notAfter"]) - ssl.cert_time_to_seconds(cert["notBefore"])) / 86400
    assert days <= 2.01


def test_ca_properties(ca):
    out = _text(ca, "-text")
    assert "CA:TRUE" in out and "pathlen:0" in out
    assert "Certificate Sign" in out and "CRL Sign" in out
    assert "prime256v1" in out or "P-256" in out
    start = ssl.cert_time_to_seconds(_text(ca, "-startdate").split("=")[1].strip())
    end = ssl.cert_time_to_seconds(_text(ca, "-enddate").split("=")[1].strip())
    assert (end - start) / 86400 <= 2.01


def test_name_constraints_present(ca):
    assert "api.anthropic.com" in _text(ca, "-text").split("Name Constraints")[1]


def test_no_key_files_remain(tmp_path):
    c = SessionCA(tmp_root=tmp_path)
    c.bundle_paths({})
    for p in _walk(tmp_path):
        if os.path.isfile(p):
            data = open(p, "rb").read()
            assert b"PRIVATE KEY" not in data, p
            assert not p.endswith(".key")
    c.close()


def test_permissions(tmp_path):
    c = SessionCA(tmp_root=tmp_path)
    c.bundle_paths({})
    [d] = list(tmp_path.iterdir())
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = [p for p in _walk(tmp_path) if os.path.isfile(p)]
    assert files
    for p in files:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p
    c.close()


def test_bundle_paths_merge(tmp_path, ca):
    (tmp_path / "x").mkdir()
    user_node = SessionCA(tmp_root=tmp_path / "x")
    user_req = SessionCA(tmp_root=tmp_path / "x")
    base = SessionCA(tmp_root=tmp_path / "x")
    n = tmp_path / "node.pem"; n.write_text(user_node.ca_pem())
    r = tmp_path / "req.pem"; r.write_text(user_req.ca_pem())
    s = tmp_path / "ssl.pem"; s.write_text(base.ca_pem())
    out = ca.bundle_paths({"NODE_EXTRA_CA_CERTS": str(n), "SSL_CERT_FILE": str(s),
                           "REQUESTS_CA_BUNDLE": str(r)})
    assert set(out) == {"NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
    node = open(out["NODE_EXTRA_CA_CERTS"]).read()
    full = open(out["SSL_CERT_FILE"]).read()
    assert user_node.ca_pem() in node and ca.ca_pem() in node
    assert base.ca_pem() not in node
    assert base.ca_pem() in full and user_req.ca_pem() in full and ca.ca_pem() in full
    assert user_node.ca_pem() not in full
    assert out["REQUESTS_CA_BUNDLE"] == out["SSL_CERT_FILE"] == out["CURL_CA_BUNDLE"]
    for text in (node, full):
        assert text.count("BEGIN CERTIFICATE") == text.count("END CERTIFICATE")
        assert text.startswith("-----BEGIN CERTIFICATE-----")
    for c in (user_node, user_req, base):
        c.close()


def test_bundle_paths_default_roots(tmp_path):
    ca = SessionCA(tmp_root=tmp_path, security=str(tmp_path / "nope"))
    out = ca.bundle_paths({"NODE_EXTRA_CA_CERTS": "/nonexistent"})
    node = open(out["NODE_EXTRA_CA_CERTS"]).read()
    assert node.strip() == ca.ca_pem().strip()
    full = open(out["SSL_CERT_FILE"]).read()
    assert full.count("BEGIN CERTIFICATE") > 10
    assert ca.ca_pem() in full
    ctx = ssl.create_default_context(cafile=out["SSL_CERT_FILE"])
    assert ctx.cert_store_stats()["x509_ca"] > 10
    ca.close()


def test_close_idempotent(tmp_path):
    c = SessionCA(tmp_root=tmp_path)
    assert list(tmp_path.iterdir())
    c.close()
    c.close()
    assert not list(tmp_path.iterdir())


def test_missing_openssl_raises(tmp_path):
    with pytest.raises(SessionCAError):
        SessionCA(openssl="/nonexistent/openssl", tmp_root=tmp_path)
    assert not list(tmp_path.iterdir())


def _fake_security(tmp_path, body, code=0):
    p = tmp_path / "security"
    p.write_text(f"#!/bin/sh\n{body}\nexit {code}\n")
    p.chmod(0o755)
    return str(p)


def test_keychain_roots_merged_and_deduped(tmp_path):
    (tmp_path / "x").mkdir()
    a, b = SessionCA(tmp_root=tmp_path / "x"), SessionCA(tmp_root=tmp_path / "x")
    key = "-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----\n"
    (tmp_path / "out.txt").write_text(a.ca_pem() + b.ca_pem() + key + a.ca_pem().replace("\n", "\r\n") + b.ca_pem())
    sec = _fake_security(tmp_path, f"cat {tmp_path}/out.txt")
    ssl_file = tmp_path / "ssl.pem"; ssl_file.write_text(b.ca_pem())
    c = SessionCA(tmp_root=tmp_path, security=sec)
    full = open(c.bundle_paths({"SSL_CERT_FILE": str(ssl_file)})["SSL_CERT_FILE"]).read()
    assert a.ca_pem() in full and b.ca_pem() in full and c.ca_pem() in full
    assert full.count("BEGIN CERTIFICATE") == 3
    assert "PRIVATE KEY" not in full
    for x in (a, b, c):
        x.close()


@pytest.mark.parametrize("script", ["echo boom >&2", "cat /nonexistent/x", "missing"])
def test_keychain_failure_skipped(tmp_path, script):
    sec = str(tmp_path / "nope") if script == "missing" else _fake_security(tmp_path, script, code=1)
    c = SessionCA(tmp_root=tmp_path, security=sec)
    full = open(c.bundle_paths({})["SSL_CERT_FILE"]).read()
    assert c.ca_pem() in full and full.count("BEGIN CERTIFICATE") > 10
    c.close()
