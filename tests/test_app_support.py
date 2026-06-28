"""Tests for the native shell's pure helpers: dot glyph selection + sync-back-before-login."""
import json

from acctsw import accounts as acct
from app import terminal, web_state
from tests.conftest import make_codex_blob


def _state(seats_codex):
    return {"tools": {"codex": {"seats": seats_codex}, "claude": {"seats": []}}}


def test_dot_for_fresh():
    assert web_state.dot_for(_state([{"active": True, "limited": False}])) == "fresh"


def test_dot_for_resting():
    assert web_state.dot_for(_state([{"active": True, "limited": True}])) == "resting"


def test_dot_for_hello_on_unauthorized():
    assert web_state.dot_for(_state([{"usage": {"error": "unauthorized"}}])) == "hello"


def test_dot_for_switched_precedence():
    s = _state([{"active": True, "limited": True}])
    s["recently_switched"] = True
    assert web_state.dot_for(s) == "switched"


def test_prepare_then_login_syncs_back_active_before_login(ctx, monkeypatch):
    """The invariant: the active seat's (rotated) live creds are snapshotted BEFORE login runs."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    st = ctx.load_state()
    acct.add(ctx, st, "codex", email="a@x.com")
    # rotate live token (as a session would) but DON'T snapshot it yet
    rotated = make_codex_blob("a@x.com").replace('"refresh_token": "r"', '"refresh_token": "ROT"')
    ctx.cred["codex"].set_live(rotated)

    opened = {}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: opened.setdefault("cmd", cmd))
    terminal.prepare_then_login(ctx, "codex", "codex login")

    # sync-back happened before the (mocked) login
    snap = json.loads(ctx.keychain.get(ctx.keychain_service, "codex:a@x.com"))
    assert snap["tokens"]["refresh_token"] == "ROT"
    assert opened["cmd"] == "codex login"


def test_prepare_then_login_no_command_is_noop(ctx, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: called.__setitem__("n", called["n"] + 1))
    terminal.prepare_then_login(ctx, "codex", None)  # paste flow → no terminal
    assert called["n"] == 0


def test_json_escape():
    assert terminal.json_escape('a "b" c') == '"a \\"b\\" c"'
