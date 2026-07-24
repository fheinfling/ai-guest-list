"""Unit tests for the UI↔engine bridge dispatch (no pyobjc)."""
import json

from acctsw import accounts as acct
from acctsw import bridge
from tests.conftest import make_codex_blob


def _add(ctx, email):
    ctx.cred["codex"].set_live(make_codex_blob(email))
    st = ctx.load_state()
    acct.add(ctx, st, "codex", email=email)


def test_status_returns_state(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "ready"})
    assert r["ok"] is True
    assert r["state"]["tools"]["codex"]["active"] == "a@x.com"


def test_shared_account_warning_rides_the_nested_state(ctx):
    """The shared-account warning must land at result['state']['warnings'] — the level the menubar
    reads. Two codex seats on ONE ChatGPT account (same account_id) → exactly one warning there."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="dup"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("a+codex@x.com", account_id="dup"))
    acct.add(ctx, ctx.load_state(), "codex", email="a+codex@x.com")
    r = bridge.handle(ctx, {"action": "usage"})
    assert "warnings" not in r                      # NOT at the top level (the bug we fixed)
    assert len(r["state"]["warnings"]) == 1 and "same account" in r["state"]["warnings"][0]


def test_state_carries_app_version_and_build(ctx):
    _add(ctx, "a@x.com")
    import acctsw
    app = bridge.handle(ctx, {"action": "ready"})["state"]["app"]
    assert app["version"] == acctsw.__version__
    assert app["build"] == "dev"           # source checkout → not a packaged build


def test_build_number_reads_bundle_info_plist(tmp_path, monkeypatch):
    """From inside a packaged *.app, build_number() reads CFBundleVersion from Info.plist."""
    import plistlib
    import acctsw
    appdir = tmp_path / "AI Guest List.app"
    fake_module_file = appdir / "Contents" / "Resources" / "lib" / "acctsw" / "__init__.py"
    fake_module_file.parent.mkdir(parents=True)
    (appdir / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "142"}))
    monkeypatch.setattr(acctsw, "_BUILD_CACHE", None)
    monkeypatch.setattr(acctsw, "__file__", str(fake_module_file))
    assert acctsw.build_number() == "142"


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


def test_add_action_is_gone(ctx):
    # `add` was the modal's round-trip for a login plan; the sub-view resolves everything client-side.
    r = bridge.handle(ctx, {"action": "add", "tool": "claude"})
    assert r["ok"] is False and "unknown action" in r["error"]


def test_login_command():
    # both tools' only Terminal path is the browser sign-in; method is reserved but unused
    assert bridge.login_command("codex") == "codex login"
    assert bridge.login_command("codex", "token") == "codex login"
    assert bridge.login_command("claude") == "claude auth login"
    assert bridge.login_command("claude", "token") == "claude auth login"


def test_snapshot_after_login_adds_seat(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("new@x.com"))
    r = bridge.handle(ctx, {"action": "snapshot", "tool": "codex", "email": "new@x.com"})
    assert r["ok"] and r["added"] == "new@x.com"
    assert "new@x.com" in ctx.load_state().accounts("codex")


def test_import_current_adds_the_live_codex_account(ctx):
    # one-tap "use the login you already have": no blob, engine reads ~/.codex/auth.json itself
    ctx.cred["codex"].set_live(make_codex_blob("live@x.com"))
    r = bridge.handle(ctx, {"action": "import_current", "tool": "codex", "name": "Work"})
    assert r["ok"] and r["added"] == "live@x.com"
    st = ctx.load_state()
    assert "live@x.com" in st.accounts("codex")
    assert st.get_seat("codex", "live@x.com")["name"] == "Work"


def test_import_current_rejects_when_not_signed_in(ctx):
    r = bridge.handle(ctx, {"action": "import_current", "tool": "codex"})
    assert r["ok"] is False and "signed in" in r["error"]


def test_import_current_rejects_already_a_seat(ctx):
    _add(ctx, "dup@x.com")                       # already on the list + live
    r = bridge.handle(ctx, {"action": "import_current", "tool": "codex"})
    assert r["ok"] is False and "already on the list" in r["error"]


def test_import_current_is_codex_only(ctx):
    r = bridge.handle(ctx, {"action": "import_current", "tool": "claude"})
    assert r["ok"] is False and "codex-only" in r["error"]


def test_snapshot_state_exposes_unregistered_live_codex(ctx):
    # signed in but NOT a seat → surfaced for the one-tap import affordance
    ctx.cred["codex"].set_live(make_codex_blob("live@x.com"))
    assert bridge.snapshot_state(ctx)["codex_live_unregistered"] == {"email": "live@x.com"}
    # once it's a seat, the affordance disappears
    _add(ctx, "live@x.com")
    assert bridge.snapshot_state(ctx)["codex_live_unregistered"] is None


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
    assert r["state"]["dot"] in {"green", "amber", "hello", "switched"}
    assert r["state"]["recently_switched"] is False


def test_set_strategy(ctx):
    assert bridge.handle(ctx, {"action": "set_strategy", "value": "most_headroom"})["state"]["settings"]["strategy"] == "most_headroom"
    assert bridge.handle(ctx, {"action": "set_strategy", "value": "bogus"})["ok"] is False


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


def test_no_headroom_surface_in_snapshot(ctx):
    """The retired 'save credit' feature leaves no fields in the UI snapshot and no toggle key."""
    _add(ctx, "a@x.com")
    state = bridge.snapshot_state(ctx)
    for k in ("headroom_available", "headroom_savings", "headroom_stats",
              "headroom_proxy_down", "headroom_event"):
        assert k not in state
    assert "headroom" not in bridge.TOGGLE_KEYS
    # the removed actions are unknown now
    assert bridge.handle(ctx, {"action": "set_savings_level", "value": "max"})["ok"] is False
    assert bridge.handle(ctx, {"action": "headroom_install"})["ok"] is False
    assert bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})["ok"] is False
