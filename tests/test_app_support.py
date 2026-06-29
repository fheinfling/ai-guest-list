"""Tests for the native shell's pure helpers: dot selection + sync-back-before-login."""
import json
from pathlib import Path

from acctsw import accounts as acct
from acctsw.web_dot import dot_for, door_for
from app import terminal
from tests.conftest import make_codex_blob

FIXTURE = Path(__file__).parent / "fixtures" / "dot_cases.json"
DOOR_FIXTURE = Path(__file__).parent / "fixtures" / "door_cases.json"


def test_dot_for_golden_fixture():
    """The SAME fixture is asserted by the node UI tests → python/JS dot logic can't drift."""
    cases = json.loads(FIXTURE.read_text())
    for c in cases:
        assert dot_for(c["state"]) == c["expected"], c["name"]


def test_door_for_golden_fixture():
    """Door open/shut — same fixture asserted by node UI tests so python/JS can't drift."""
    cases = json.loads(DOOR_FIXTURE.read_text())
    for c in cases:
        assert door_for(c["state"]) == c["expected"], c["name"]


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
    snap = json.loads(ctx.snapshot_get("codex", "a@x.com"))
    assert snap["tokens"]["refresh_token"] == "ROT"
    assert opened["cmd"] == "codex login"


def test_prepare_then_login_no_command_is_noop(ctx, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: called.__setitem__("n", called["n"] + 1))
    terminal.prepare_then_login(ctx, "codex", None)  # paste flow → no terminal
    assert called["n"] == 0


def test_json_escape():
    assert terminal.json_escape('a "b" c') == '"a \\"b\\" c"'
