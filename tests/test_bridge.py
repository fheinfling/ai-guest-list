"""Unit tests for the UI↔engine bridge dispatch (no pyobjc)."""
import json
import os
from datetime import timedelta

import pytest

from acctsw import accounts as acct
from acctsw import bridge
from acctsw import install as inst
from acctsw.state import State
from tests.conftest import make_claude_blob, make_codex_blob
from acctsw.util import now, iso
from acctsw.switch import switch


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


def test_usage_reconciles_both_provider_identities_before_poll(ctx, monkeypatch):
    """A poll captures a changed Claude login, while ordinary ticks skip the slow identity CLI."""
    seen = []
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    acct.add(ctx, ctx.load_state(), "claude", email="c@x.com")
    ctx.cred["claude"].set_live(make_claude_blob("pro"))
    monkeypatch.setattr(acct, "reconcile_codex", lambda _ctx, _state: seen.append("codex"))
    monkeypatch.setattr(
        bridge.identity_mod, "claude_live_identity",
        lambda _ctx: bridge.identity_mod.ClaudeLiveIdentity(
            blob=ctx.cred["claude"].get_live(), email="c@x.com"),
    )
    monkeypatch.setattr(
        acct, "reconcile_claude",
        lambda _ctx, _state, **_kwargs: seen.append("claude"),
    )
    monkeypatch.setattr(bridge.usage_mod, "refresh_live",
                        lambda _ctx, _tool=None, **_kwargs: {"codex": {}, "claude": {}})
    result = bridge.handle(ctx, {"action": "usage"})
    assert result["ok"] is True
    assert seen == ["codex", "claude"]


def test_usage_auto_switches_a_confirmed_limited_codex_seat_to_a_fresh_spare(ctx, monkeypatch):
    """The desktop owns a no-session handoff only after it freshly verifies the landing seat."""
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    state = ctx.load_state()
    switch(ctx, state, "codex", "a@x.com")
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="usage")
    state.set_usage("codex", "a@x.com", {
        "credential_digest": bridge._blob_digest(ctx.snapshot_get("codex", "a@x.com")),
    })
    state.set_usage("codex", "b@x.com", {
        "error": None, "stale": False, "windows": {
            "5h": {"used_pct": 10.0}, "weekly": {"used_pct": 20.0},
        }, "ok": True, "fetched_at": iso(now()), "last_attempted_at": iso(now()),
        "credential_digest": bridge._blob_digest(ctx.snapshot_get("codex", "b@x.com")),
    })
    state.save()

    def fresh(_ctx, tool=None, *, only=None, **_kwargs):
        if only:
            return {tool: {only: "ok"}}
        return {"codex": {"a@x.com": "ok"}, "claude": {}}

    monkeypatch.setattr(bridge.usage_mod, "refresh_live", fresh)
    result = bridge.handle(ctx, {"action": "usage", "scope": "all"})

    assert result["auto_switch"] == {
        "status": "switched", "tool": "codex", "from": "a@x.com", "to": "b@x.com",
        "reason": "usage_limit",
    }
    assert ctx.load_state().active("codex") == "b@x.com"


def test_usage_poll_yields_a_confirmed_limit_to_an_active_supervised_session(ctx, monkeypatch):
    """The launcher must own the live child swap/resume; observer polling only records its rest."""
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    state = ctx.load_state()
    switch(ctx, state, "codex", "a@x.com")
    state.set_limited_until("codex", "a@x.com", iso(now() + timedelta(hours=2)), source="usage")
    state.save()
    monkeypatch.setattr(bridge.session_mod, "active_session", lambda *_args: {"email": "a@x.com"})
    monkeypatch.setattr(
        bridge.usage_mod, "refresh_live",
        lambda *_args, **_kwargs: {"codex": {"a@x.com": "ok"}, "claude": {}},
    )

    result = bridge.handle(ctx, {"action": "usage", "scope": "all"})

    assert result["auto_switch"] is None
    assert ctx.load_state().active("codex") == "a@x.com"


def test_usage_scope_and_target_are_forwarded_to_detached_refresh(ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        bridge.usage_mod, "refresh_live",
        lambda _ctx, tool=None, **kwargs: seen.update(tool=tool, **kwargs) or {"codex": {}},
    )
    result = bridge.handle(ctx, {
        "action": "usage", "scope": "active", "tool": "codex",
        "only": "a@x.com", "force": True,
    })
    assert result["ok"] is True and result["refresh"] == {"codex": {}}
    assert seen["tool"] == "codex" and seen["only"] == "a@x.com"
    assert seen["active_only"] is True and seen["force"] is True


def test_targeted_codex_usage_does_not_probe_claude(ctx, monkeypatch):
    ctx.cred["claude"].set_live(make_claude_blob())
    acct.add(ctx, ctx.load_state(), "claude", email="c@x.com")
    monkeypatch.setattr(
        bridge.identity_mod, "claude_live_identity",
        lambda _ctx: pytest.fail("targeted Codex refresh must not run Claude auth status"),
    )
    monkeypatch.setattr(
        bridge.usage_mod, "claude_user_agent",
        lambda _bin: pytest.fail("targeted Codex refresh must not run Claude --version"),
    )
    monkeypatch.setattr(bridge.usage_mod, "refresh_live", lambda *_args, **_kwargs: {})
    assert bridge.handle(ctx, {"action": "usage", "tool": "codex", "force": True})["ok"] is True


def test_usage_rejects_unknown_scope(ctx):
    result = bridge.handle(ctx, {"action": "usage", "scope": "nearby"})
    assert result == {"ok": False, "error": "bad usage scope: nearby"}


def test_usage_skips_codex_reconciliation_during_supervised_session(ctx, monkeypatch):
    monkeypatch.setattr(bridge.session_mod, "active_session",
                        lambda _data_dir, _tool: {"email": "a@x.com"})
    monkeypatch.setattr(acct, "reconcile_codex",
                        lambda *_args, **_kwargs: pytest.fail("must not overwrite session home"))
    monkeypatch.setattr(bridge.usage_mod, "refresh_live", lambda *_args, **_kwargs: {})
    assert bridge.handle(ctx, {"action": "usage"})["ok"] is True


def test_state_carries_app_version_and_build(ctx):
    _add(ctx, "a@x.com")
    import acctsw
    app = bridge.handle(ctx, {"action": "ready"})["state"]["app"]
    assert app["version"] == acctsw.__version__
    assert app["build"] == "dev"           # source checkout → not a packaged build


def test_snapshot_carries_supervision_status(ctx):
    status = bridge.snapshot_state(ctx)["supervision"]
    assert set(("wrappers", "block", "rc_path", "on_path", "active")) <= status.keys()
    assert status["rc_path"] == str(inst.shell_rc_path())


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


def test_toggle_supervision_off_removes_block_and_on_restores_it(ctx):
    rc = inst.shell_rc_path()
    inst.ensure_shell_setup(rc_path=rc)
    assert inst.BLOCK_BEGIN in rc.read_text()

    off = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": False})
    assert off["ok"] is True and off["message"] == "terminal supervision is off"
    assert ctx.load_state().settings()["supervise_shell"] is False
    assert inst.BLOCK_BEGIN not in rc.read_text()

    on = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": True})
    assert on["ok"] is True and on["message"] == "terminal supervision is on"
    assert ctx.load_state().settings()["supervise_shell"] is True
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1
    assert on["state"]["supervision"]["active"] is True  # missing wrappers were repaired too


@pytest.mark.parametrize("value", [False, True], ids=["off", "on"])
def test_toggle_supervision_write_failure_is_clear_and_does_not_change_state(
    ctx, monkeypatch, value,
):
    state = ctx.load_state()
    state.set_setting("supervise_shell", not value)
    state.save()
    operation = "remove_shell_setup" if value is False else "ensure_shell_setup"

    def fail(*_args, **_kwargs):
        raise PermissionError("rc is read-only")

    monkeypatch.setattr(inst, operation, fail)
    result = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": value})
    assert result["ok"] is False
    assert "terminal supervision" in result["error"] and "rc is read-only" in result["error"]
    assert result["state"]["settings"]["supervise_shell"] is (not value)
    assert ctx.load_state().settings()["supervise_shell"] is (not value)


@pytest.mark.parametrize("value", [False, True], ids=["off", "on"])
def test_toggle_supervision_state_save_failure_rolls_back_shell_setup(ctx, monkeypatch, value):
    state = ctx.load_state()
    state.set_setting("supervise_shell", not value)
    state.save()
    if not value:
        inst.ensure_shell_setup()
    else:
        inst.remove_shell_setup()

    def fail_save(_self):
        raise OSError("state disk is full")

    monkeypatch.setattr(State, "save", fail_save)
    result = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": value})

    assert result["ok"] is False and "state disk is full" in result["error"]
    assert ctx.load_state().settings()["supervise_shell"] is (not value)
    assert inst.supervision_status()["block"] is (not value)


def _raiser(exc):
    """A stand-in that always fails with ``exc`` — for the failure-on-top-of-failure paths below."""
    def fail(*_args, **_kwargs):
        raise exc
    return fail


@pytest.mark.parametrize("value", [False, True], ids=["off", "on"])
def test_toggle_supervision_failure_reports_an_unrefreshable_status(ctx, monkeypatch, value):
    """Worst case: the rc edit fails AND the status snapshot that would explain it fails too.
    The user must be told both halves, and must not be handed a half-built result to render."""
    state = ctx.load_state()
    state.set_setting("supervise_shell", not value)
    state.save()
    operation = "remove_shell_setup" if value is False else "ensure_shell_setup"
    monkeypatch.setattr(inst, operation, _raiser(PermissionError("rc is read-only")))
    monkeypatch.setattr(bridge, "snapshot_state", _raiser(OSError("the store is gone")))

    result = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": value})

    direction = "on" if value else "off"
    assert result == {
        "ok": False,
        "error": (f"couldn't turn terminal supervision {direction}: rc is read-only"
                  "; couldn't refresh status: the store is gone"),
    }
    assert "state" not in result                 # never a partial snapshot the UI would apply
    assert ctx.load_state().settings()["supervise_shell"] is (not value)


def test_toggle_supervision_failure_names_a_wordless_exception(ctx, monkeypatch):
    """An exception with no message — or only whitespace — must still name something reportable,
    so the sheet never shows a bare 'couldn't turn terminal supervision on:' with nothing after it."""
    monkeypatch.setattr(inst, "ensure_shell_setup", _raiser(RuntimeError()))
    monkeypatch.setattr(bridge, "snapshot_state", _raiser(TimeoutError("   ")))

    result = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": True})

    assert result == {
        "ok": False,
        "error": ("couldn't turn terminal supervision on: RuntimeError"
                  "; couldn't refresh status: TimeoutError"),
    }


@pytest.mark.parametrize("value", [False, True], ids=["off", "on"])
def test_toggle_supervision_save_failure_reports_a_failed_rollback(ctx, monkeypatch, value):
    """The rc edit landed, the state write failed, and the rollback that would have re-aligned the
    two sources of truth ALSO failed. The shell and the setting now genuinely disagree, so the
    message has to admit it rather than blaming the save alone."""
    state = ctx.load_state()
    state.set_setting("supervise_shell", not value)
    state.save()
    # previous == (not value), so the rollback attempts that direction — break exactly that call
    # and leave the forward toggle working.
    rollback_op = "ensure_shell_setup" if not value else "remove_shell_setup"
    monkeypatch.setattr(inst, rollback_op, _raiser(PermissionError("rc is read-only")))
    monkeypatch.setattr(State, "save", _raiser(OSError("state disk is full")))

    result = bridge.handle(ctx, {"action": "toggle", "key": "supervise_shell", "value": value})

    assert result["ok"] is False
    assert result["error"] == ("couldn't save that setting: state disk is full"
                               "; shell rollback also failed: rc is read-only")
    assert result["state"]["settings"]["supervise_shell"] is (not value)
    assert ctx.load_state().settings()["supervise_shell"] is (not value)


@pytest.mark.parametrize("key", ["auto_switch", "supervise_shell"])
def test_toggle_save_failure_reports_an_unrefreshable_status(ctx, monkeypatch, key):
    """Save failed and the follow-up snapshot failed too — for a plain toggle as well as the
    supervision one, which additionally rolled the shell back successfully (no rollback clause)."""
    monkeypatch.setattr(State, "save", _raiser(OSError("state disk is full")))
    monkeypatch.setattr(bridge, "snapshot_state", _raiser(RuntimeError()))

    result = bridge.handle(ctx, {"action": "toggle", "key": key, "value": False})

    assert result == {
        "ok": False,
        "error": ("couldn't save that setting: state disk is full"
                  "; couldn't refresh status: RuntimeError"),
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_snapshot_surfaces_an_unreadable_rc_to_the_ui(ctx):
    """The install-layer read failure has to travel all the way into the popover payload — and only
    that one field: an rc we cannot read must not take the rest of the snapshot down with it."""
    rc = inst.shell_rc_path()
    inst.ensure_shell_setup(rc_path=rc)
    rc.chmod(0o000)
    try:
        state = bridge.snapshot_state(ctx)
    finally:
        rc.chmod(0o600)

    supervision = state["supervision"]
    assert supervision["error"].startswith(f"couldn't read {rc}: ")
    assert "Permission denied" in supervision["error"]
    assert supervision["block"] is False and supervision["active"] is False
    assert state["dot"] and "tools" in state          # the rest of the payload is intact


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


def test_add_snapshots_passed_blob_not_a_second_live_read(ctx):
    # TOCTOU guard: acct.add must snapshot the EXACT blob passed, even if a second get_live() would now
    # see a DIFFERENT account (an out-of-band `codex login` mid-flow). Otherwise a seat labeled A could
    # silently store account C's credentials.
    ctx.cred["codex"].set_live(make_codex_blob("live-now@x.com"))     # what a bare get_live() would see
    passed = make_codex_blob("validated@x.com")
    seat = acct.add(ctx, ctx.load_state(), "codex", email="validated@x.com", blob=passed)
    assert seat["email"] == "validated@x.com"
    stored = ctx.snapshot_get("codex", "validated@x.com")
    assert ctx.cred["codex"].email_of(stored) == "validated@x.com"   # the passed blob, not live-now@


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
    seat = next(s for s in r["state"]["tools"]["codex"]["seats"] if s["email"] == "a@x.com")
    assert seat["last_on_floor"] is not None


def test_paste_installs_and_registers_codex(ctx):
    blob = make_codex_blob("pasted@x.com")
    r = bridge.handle(ctx, {"action": "paste", "tool": "codex", "blob": blob})
    assert r["ok"] and r["added"] == "pasted@x.com"
    assert "pasted@x.com" in ctx.load_state().accounts("codex")
    import json
    assert json.loads(ctx.cred["codex"].get_live())  # live creds installed


def test_paste_rejects_claude_explicitly(ctx):
    r = bridge.handle(
        ctx, {"action": "paste", "tool": "claude", "blob": make_claude_blob()}
    )
    assert r["ok"] is False and "codex-only" in r["error"]


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
