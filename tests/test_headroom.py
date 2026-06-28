"""Tests for the Headroom integration (global app-managed mode)."""
import json

from acctsw import accounts as acct
from acctsw import bridge
from acctsw import headroom
from acctsw import launcher as L
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get


def _two_codex(ctx):
    for em in ("a@x.com", "b@x.com"):
        ctx.cred["codex"].set_live(make_codex_blob(em))
        acct.add(ctx, ctx.load_state(), "codex", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "codex", "a@x.com")


class _Spawn:
    def __init__(self, status=0):
        self.calls = []
        self.status = status
    def __call__(self, argv, on_output):
        self.calls.append(list(argv))
        on_output(b"done\n")
        return self.status


def test_launcher_runs_plain_under_global_headroom(ctx, monkeypatch):
    """Headroom is global/app-managed now → cx/cl must NOT per-session-wrap (no double-route)."""
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    # don't let the launcher's self-heal shell out to / touch the real ~/.codex during this test
    monkeypatch.setattr(headroom, "headroom_path", lambda: None)
    spawn = _Spawn()
    L.run(ctx, "codex", ["--foo"], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][1:2] != ["wrap"]          # not wrapped
    assert "headroom" not in " ".join(spawn.calls[0][:2])


# --- global enable/disable (app-managed) ------------------------------------------------------

def _codex_cfg(tmp_path, monkeypatch, content='model = "gpt-5.5"\n'):
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    (tmp_path / "codex").mkdir(); (tmp_path / "claude").mkdir()
    cfg = tmp_path / "codex" / "config.toml"; cfg.write_text(content)
    return cfg


def _fakerun(rc_apply=0, running=True):
    import types
    def run(args, **k):
        out = "proxy running" if (running and "status" in args) else "ok"
        rc = rc_apply if "apply" in args else 0
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return run


def test_global_enable_snapshots_then_applies(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    ok, msg = headroom.global_enable(tmp_path / "store", run=_fakerun(rc_apply=0, running=True))
    assert ok, msg
    assert (tmp_path / "store" / "headroom-global-backup" / "manifest.json").exists()  # snapshot taken


def test_global_enable_rolls_back_when_proxy_not_running(tmp_path, monkeypatch):
    """apply succeeds but proxy isn't healthy → full rollback (never leave a dead-proxy route)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    ok, _ = headroom.global_enable(tmp_path / "store", run=_fakerun(rc_apply=0, running=False))
    assert ok is False
    assert cfg.read_text() == 'model = "orig"\n'                # config untouched
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_global_enable_rolls_back_on_apply_failure(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    ok, _ = headroom.global_enable(tmp_path / "store", run=_fakerun(rc_apply=1, running=False))
    assert ok is False
    assert cfg.read_text() == 'model = "orig"\n'
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_snapshot_global_is_idempotent(tmp_path, monkeypatch):
    """A 2nd enable must NOT re-snapshot a routed config (keeps the original backstop)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    headroom.snapshot_global(tmp_path / "store")
    cfg.write_text('model_provider = "headroom"\n')   # now routed
    headroom.snapshot_global(tmp_path / "store")       # idempotent: must keep the ORIGINAL
    ok, failures = headroom.restore_global(tmp_path / "store")
    assert ok and not failures
    assert cfg.read_text() == 'model = "orig"\n'


def test_global_disable_removes_and_restores(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    headroom.snapshot_global(tmp_path / "store")                # pretend we'd enabled
    cfg.write_text('model_provider = "headroom"\n')             # simulate injected routing
    calls = []
    def run(args, **k):
        calls.append(args)
        import types; return types.SimpleNamespace(returncode=0, stdout="removed", stderr="")
    ok, _ = headroom.global_disable(tmp_path / "store", run=run)
    assert ok
    assert calls[-1][1:3] == ["install", "remove"]
    assert cfg.read_text() == 'model = "orig"\n'                # backstop restored the original
    assert not (tmp_path / "store" / "headroom-global-backup").exists()  # backup cleared on success


def test_bridge_headroom_toggle_enables(ctx, monkeypatch):
    seen = {}
    def _on(store): seen["on"] = True; return (True, "ok")
    def _off(store): seen["off"] = True; return (True, "ok")
    monkeypatch.setattr(headroom, "global_enable", _on)
    monkeypatch.setattr(headroom, "global_disable", _off)
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] and seen.get("on") and ctx.load_state().settings()["headroom"] is True
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert r["ok"] and seen.get("off") and ctx.load_state().settings()["headroom"] is False


def test_bridge_headroom_toggle_reverts_on_enable_failure(ctx, monkeypatch):
    monkeypatch.setattr(headroom, "global_enable", lambda store: (False, "no headroom"))
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] is False
    assert ctx.load_state().settings()["headroom"] is False     # toggle reverted


def test_bridge_headroom_disable_failure_keeps_setting_on(ctx, monkeypatch):
    """A FAILED disable must leave the setting ON so launch/health recovery keeps trying — never
    abandon a still-routed config pointed at a dying proxy with the setting flipped off."""
    monkeypatch.setattr(headroom, "global_enable", lambda store: (True, "ok"))
    monkeypatch.setattr(headroom, "global_disable", lambda store: (False, "restore incomplete"))
    bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert r["ok"] is False
    assert ctx.load_state().settings()["headroom"] is True      # stays ON → recovery keeps trying


# --- heal(): serialized self-heal keyed off ACTUAL state (not the setting) ---------------------

def test_heal_noop_when_proxy_running(tmp_path, monkeypatch):
    _codex_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    changed, msg = headroom.heal(tmp_path / "store", run=_fakerun(running=True))
    assert changed is False and msg == "healthy"               # healthy proxy → never torn down


def test_heal_strips_orphaned_injection_when_proxy_dead(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    headroom.snapshot_global(tmp_path / "store")               # original captured
    cfg.write_text('model_provider = "headroom"\n')            # routing injected, proxy dead
    changed, _ = headroom.heal(tmp_path / "store", run=_fakerun(running=False))
    assert changed is True
    assert cfg.read_text() == 'model = "orig"\n'               # restored from backup backstop
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_heal_reports_failure_when_restore_incomplete(tmp_path, monkeypatch):
    """heal() must NOT claim success when the restore failed — else reconcile clears the setting and
    the UI shows 'restored' over a still-injected, dead-proxy config."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    headroom.snapshot_global(tmp_path / "store")
    cfg.write_text('model_provider = "headroom"\n')
    monkeypatch.setattr(headroom, "_remove_and_restore",
                        lambda *a, **k: (False, "config restore incomplete"))
    healed, msg = headroom.heal(tmp_path / "store", run=_fakerun(running=False))
    assert healed is False and "incomplete" in msg


def test_reconcile_keeps_setting_when_heal_fails(ctx, monkeypatch):
    monkeypatch.setattr(headroom, "heal", lambda store, **k: (False, "config restore incomplete"))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    healed, _ = headroom.reconcile(ctx)
    assert healed is False and ctx.load_state().settings()["headroom"] is True   # stays ON to retry


def test_global_enable_aborts_on_dirty_baseline(tmp_path, monkeypatch):
    """If config is already routed with no backup and the strip can't clean it, enable must abort —
    never baseline a routed config as the user's 'original'."""
    _codex_cfg(tmp_path, monkeypatch, 'model_provider = "headroom"\n')   # routed, no backup yet
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")

    def run(args, **k):                                    # `install remove` that doesn't clean
        import types; return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    ok, msg = headroom.global_enable(tmp_path / "store", run=run)
    assert ok is False and "baseline" in msg
    assert not (tmp_path / "store" / "headroom-global-backup" / "manifest.json").exists()


def test_disable_reports_failure_when_still_injected_no_backup(tmp_path, monkeypatch):
    """install remove fails and there's no backup → config still routed. Must NOT report success
    (else reconcile clears the setting over a broken config)."""
    _codex_cfg(tmp_path, monkeypatch, 'model_provider = "headroom"\n')   # injected, no backup
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")

    def run(args, **k):
        import types; return types.SimpleNamespace(returncode=1, stdout="not installed", stderr="")
    ok, msg = headroom.global_disable(tmp_path / "store", run=run)
    assert ok is False and "still present" in msg


def test_spawn_detached_remove(tmp_path, monkeypatch):
    """Quit fires a detached `install remove` that outlives the app (no blocking)."""
    calls = {}
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")

    class _Popen:
        def __init__(self, argv, **kw):
            calls["argv"] = argv; calls["kw"] = kw
    monkeypatch.setattr(headroom.subprocess, "Popen", _Popen)
    assert headroom.spawn_detached_remove() is True
    assert calls["argv"][1:3] == ["install", "remove"]
    assert calls["kw"].get("start_new_session") is True       # detached → survives app exit


def test_spawn_detached_remove_no_binary(monkeypatch):
    monkeypatch.setattr(headroom, "headroom_path", lambda: None)
    assert headroom.spawn_detached_remove() is False          # nothing to do without the binary


def test_is_injected_handles_non_utf8(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch)
    cfg.write_bytes(b'\xff\xfe model_provider = "headroom"')   # invalid UTF-8 + a real marker
    assert headroom._is_injected("codex") is True             # doesn't raise; still finds the marker


def test_bridge_headroom_toggle_survives_exception(ctx, monkeypatch):
    """A raised exception in global_enable must still return a result (toggle never hangs)."""
    def boom(store): raise OSError("disk full")
    monkeypatch.setattr(headroom, "global_enable", boom)
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] is False and "OSError" in r["error"]
    assert ctx.load_state().settings()["headroom"] is False   # enable failed → setting OFF


def test_heal_noop_when_clean(tmp_path, monkeypatch):
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    changed, msg = headroom.heal(tmp_path / "store", run=_fakerun(running=False))
    assert changed is False and msg == "clean"                # nothing injected → nothing to do


def test_is_injected_detects_claude_settings_json(tmp_path, monkeypatch):
    """Claude routing lands in settings.json, NOT CLAUDE.md — _is_injected must scan every touched
    file or global_disable could delete the only backup while config is still routed."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    (tmp_path / "codex").mkdir(); (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text('{"env": {"X": "headroom:rtk-instructions"}}')
    assert headroom._is_injected("claude") is True            # detected though CLAUDE.md is absent


def test_verify_rtk_migrates_legacy_digest(tmp_path, monkeypatch):
    """An upgrade that changes the digest algorithm must NOT read as a supply-chain tamper: a
    recorded legacy (hex-of-hex) digest is silently migrated, not rejected."""
    from acctsw.util import sha256_text
    fake = tmp_path / "rtk"; fake.write_bytes(b"rtk-binary-v1")
    monkeypatch.setattr(headroom, "rtk_path", lambda: fake)
    store = tmp_path / "store"; store.mkdir()
    (store / "rtk.sha256").write_text(sha256_text(fake.read_bytes().hex()))   # legacy format
    ok, msg = headroom.verify_rtk(store)
    assert ok and "migrated" in msg
    ok2, msg2 = headroom.verify_rtk(store)                                    # now verifies clean
    assert ok2 and "verified" in msg2


def test_is_injected_ignores_prose_mentions(tmp_path, monkeypatch):
    """A user's own config that merely mentions Headroom/headroomlabs in prose must NOT count as
    injected — only the actual config-syntax routing directives do — else the restore backstop would
    overwrite their edits (data loss)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, "# I love Headroom and headroomlabs is great\n")
    assert headroom._is_injected("codex") is False        # prose mentions, no real directive
    cfg.write_text('model_provider = "headroom"\n')
    assert headroom._is_injected("codex") is True         # actual routing directive → injected


def test_reconcile_clears_setting_when_healed(ctx, monkeypatch):
    """reconcile() unifies policy: if dead routing was stripped, the save-credit setting is cleared
    so cx/cl and the GUI agree on state."""
    monkeypatch.setattr(headroom, "heal", lambda store, **k: (True, "removed"))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    changed, _ = headroom.reconcile(ctx)
    assert changed and ctx.load_state().settings()["headroom"] is False


def test_needs_reconcile_false_when_unused(ctx, monkeypatch):
    """The cx/cl hot path must skip the status subprocess when save-credit was never used."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", ctx.data_dir / "nope-codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", ctx.data_dir / "nope-claude")
    assert headroom.needs_reconcile(ctx) is False   # no setting, no backup, no injection


def test_op_lock_nonblocking_returns_false_when_held(tmp_path):
    store = tmp_path / "store"
    with headroom.op_lock(store) as a:
        assert a is True
        with headroom.op_lock(store, blocking=False) as b:
            assert b is False   # already held → non-blocking acquisition fails fast (no freeze)


def test_op_lock_file_lives_outside_backup_dir(tmp_path):
    """The op-lock must not live inside the backup dir, or rmtree-ing the backup mid-hold swaps the
    lock's inode and silently breaks serialization."""
    store = tmp_path / "store"
    with headroom.op_lock(store):
        pass
    assert (store / ".headroom-oplock").exists()
    assert not (store / "headroom-global-backup" / ".oplock").exists()


def test_harden_env_sets_telemetry_off():
    e = headroom.harden_env({})
    assert e["HEADROOM_TELEMETRY"] == "off"
    assert e["LITELLM_TELEMETRY"] == "False"
    assert e["DO_NOT_TRACK"] == "1"


def test_package_pinned_to_audited_version():
    assert headroom.PACKAGE.endswith("==" + headroom.PINNED_VERSION)


def test_verify_rtk_tofu(tmp_path, monkeypatch):
    fake = tmp_path / "rtk"
    fake.write_bytes(b"rtk-binary-v1")
    monkeypatch.setattr(headroom, "rtk_path", lambda: fake)
    store = tmp_path / "store"
    assert headroom.verify_rtk(store)[0] is True       # records on first sight
    assert headroom.verify_rtk(store)[0] is True       # verifies unchanged
    fake.write_bytes(b"rtk-binary-TAMPERED")
    ok, msg = headroom.verify_rtk(store)
    assert ok is False and "changed" in msg            # tamper detected


def test_bridge_headroom_install(ctx):
    # headroom is already in the venv, so ensure_installed is a fast no-op returning available
    r = bridge.handle(ctx, {"action": "headroom_install"})
    assert r["ok"] is True and r["installed"] is True
    assert r["state"]["headroom_available"] is True
