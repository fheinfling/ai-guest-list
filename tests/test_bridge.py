"""Unit tests for the UI↔engine bridge dispatch (no pyobjc)."""
from acctsw import accounts as acct
from acctsw import bridge
from tests.conftest import make_codex_blob


def _add(ctx, email):
    ctx.cred["codex"].set_live(make_codex_blob(email))
    st = ctx.load_state()
    acct.add(ctx, st, "codex", email=email)


def test_status_returns_state_with_headroom_flag(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "ready"})
    assert r["ok"] is True
    assert "headroom_available" in r["state"]
    assert r["state"]["tools"]["codex"]["active"] == "a@x.com"


def test_toggle_setting_persists(ctx):
    r = bridge.handle(ctx, {"action": "toggle", "key": "auto_switch", "value": False})
    assert r["ok"] and r["state"]["settings"]["auto_switch"] is False
    assert ctx.load_state().settings()["auto_switch"] is False


def test_switch_action(ctx):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "a@x.com"})
    assert r["ok"] and r["celebrate"] is True
    assert r["state"]["tools"]["codex"]["active"] == "a@x.com"


def test_switch_unknown_seat_is_friendly_error(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "ghost@x.com"})
    assert r["ok"] is False and "ghost@x.com" in r["error"]


def test_remove_action(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "remove", "tool": "codex", "email": "a@x.com"})
    assert r["ok"] and r["state"]["tools"]["codex"]["seats"] == []


def test_add_returns_login_plan(ctx):
    r = bridge.handle(ctx, {"action": "add", "tool": "claude"})
    assert r["ok"] and r["login"]["tool"] == "claude"
    ids = {m["id"] for m in r["login"]["methods"]}
    assert ids == {"browser", "token"}


def test_snapshot_after_login_adds_seat(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("new@x.com"))
    r = bridge.handle(ctx, {"action": "snapshot", "tool": "codex", "email": "new@x.com"})
    assert r["ok"] and r["added"] == "new@x.com"
    assert "new@x.com" in ctx.load_state().accounts("codex")


def test_missing_field_error(ctx):
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex"})  # no email
    assert r["ok"] is False and "email" in r["error"]


def test_unknown_action(ctx):
    r = bridge.handle(ctx, {"action": "frobnicate"})
    assert r["ok"] is False and "unknown action" in r["error"]


def test_toggle_rejects_non_whitelisted_key(ctx):
    r = bridge.handle(ctx, {"action": "toggle", "key": "theme", "value": True})
    assert r["ok"] is False and "not a toggle" in r["error"]
    # theme remains its default string, not clobbered to a bool
    assert ctx.load_state().settings()["theme"] == "light"


def test_set_theme(ctx):
    assert bridge.handle(ctx, {"action": "set_theme", "value": "dark"})["state"]["settings"]["theme"] == "dark"
    assert bridge.handle(ctx, {"action": "set_theme", "value": "bogus"})["ok"] is False


def test_state_includes_dot_and_recently_switched(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "status"})
    assert r["state"]["dot"] in {"fresh", "resting", "hello", "switched"}
    assert r["state"]["recently_switched"] is False


def test_switch_sets_recently_switched_dot(ctx):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "a@x.com"})
    assert r["state"]["recently_switched"] is True
    assert r["state"]["dot"] == "switched"


def test_paste_installs_and_registers_codex(ctx):
    blob = make_codex_blob("pasted@x.com")
    r = bridge.handle(ctx, {"action": "paste", "tool": "codex", "blob": blob})
    assert r["ok"] and r["added"] == "pasted@x.com"
    assert "pasted@x.com" in ctx.load_state().accounts("codex")
    import json
    assert json.loads(ctx.cred["codex"].get_live())  # live creds installed


def test_is_native_routing():
    assert bridge.is_native("quit") and bridge.is_native("login") and bridge.is_native("settings")
    assert not bridge.is_native("switch") and not bridge.is_native("status")
    assert not bridge.is_native("headroom_install")  # engine-routed (returns command)
