"""Identity resolution tests (reviewer gap #4) — codex via JWT, claude via mocked CLI."""
import json
import subprocess
import types

from acctsw import identity
from acctsw.identity import claude_status_email, live_email
from tests.conftest import make_codex_blob


def _fake_run(stdout="", returncode=0):
    def run(*a, **k):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return run


def test_codex_live_email_from_jwt(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    assert live_email(ctx, "codex") == "a@x.com"


def test_codex_live_email_none_without_creds(ctx):
    assert live_email(ctx, "codex") is None


def test_claude_status_email_parses_json(monkeypatch):
    monkeypatch.setattr(identity, "shutil", types.SimpleNamespace(which=lambda _: "/bin/claude"))
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(json.dumps({"loggedIn": True, "email": "c@x.com"})))
    assert claude_status_email("/bin/claude") == "c@x.com"


def test_claude_status_email_logged_out(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(json.dumps({"loggedIn": False})))
    assert claude_status_email("/bin/claude") is None


def test_claude_status_email_no_binary(monkeypatch):
    monkeypatch.setattr(identity, "shutil", types.SimpleNamespace(which=lambda _: None))
    assert claude_status_email(None) is None


def test_claude_status_email_bad_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("not json"))
    assert claude_status_email("/bin/claude") is None
