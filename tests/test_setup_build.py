"""Packaging configuration can be exercised without invoking setuptools or py2app."""
import os
from pathlib import Path
import runpy
import ssl
import subprocess
import sys
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("names", "empty"), [
    ((), False),
    (("SSL_CERT_FILE",), False),
    (("SSL_CERT_DIR",), False),
    (("SSL_CERT_FILE", "SSL_CERT_DIR"), False),
    (("SSL_CERT_FILE",), True),
    (("SSL_CERT_DIR",), True),
], ids=["clean", "file", "directory", "both", "empty-file", "empty-directory"])
def test_setup_scrubs_ssl_overrides_before_packaging(tmp_path, monkeypatch, capsys, names, empty):
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    defaults = ssl.get_default_verify_paths()
    session_ca = tmp_path / "acctsw-ca-session" / "full.pem"
    session_ca.parent.mkdir()
    session_ca.write_text("session CA fixture")
    overrides = {"SSL_CERT_FILE": str(session_ca), "SSL_CERT_DIR": str(session_ca.parent)}
    for name in names:
        monkeypatch.setenv(name, "" if empty else overrides[name])
    unrelated = {"REQUESTS_CA_BUNDLE": str(session_ca), "NODE_EXTRA_CA_CERTS": str(session_ca),
                 "HTTPS_PROXY": "http://127.0.0.1:9999"}
    for name, value in unrelated.items():
        monkeypatch.setenv(name, value)
    if names and not empty:
        assert ssl.get_default_verify_paths() != defaults

    captured = []

    def fake_setup(**kwargs):
        captured.append((dict(os.environ), ssl.get_default_verify_paths()))

    setuptools = ModuleType("setuptools")
    setuptools.setup = fake_setup
    monkeypatch.setitem(sys.modules, "setuptools", setuptools)
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "42\n")
    monkeypatch.chdir(REPO)
    runpy.run_path(str(REPO / "setup.py"))

    assert len(captured) == 1
    environment, paths = captured[0]
    assert "SSL_CERT_FILE" not in environment
    assert "SSL_CERT_DIR" not in environment
    assert paths == defaults
    assert paths.cafile == (paths.openssl_cafile if os.path.isfile(paths.openssl_cafile) else None)
    assert paths.capath == (paths.openssl_capath if os.path.isdir(paths.openssl_capath) else None)
    assert {name: environment[name] for name in unrelated} == unrelated
    output = capsys.readouterr().out.splitlines()
    assert len(output) == bool(names)
    if names:
        assert all(name in output[0] for name in names)
        assert "OpenSSL defaults" in output[0]

