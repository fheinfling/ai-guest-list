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


def test_scoped_restores_injected_files(tmp_path, monkeypatch):
    """headroom.scoped must restore files Headroom injects into, byte-for-byte (incl. deleting a
    file that didn't exist before)."""
    from acctsw import paths as P
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\n')
    agents = tmp_path / "AGENTS.md"  # doesn't exist initially
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path)
    store = tmp_path / "hr-backup"
    with headroom.scoped("codex", store):
        cfg.write_text('model_provider = "headroom"\n')  # simulate injection
        agents.write_text("rtk instructions\n")
    assert cfg.read_text() == 'model = "gpt-5.5"\n'  # restored exactly
    assert not agents.exists()                        # created-then-removed


def test_recover_stale_undoes_crashed_session(tmp_path, monkeypatch):
    """If a session is killed mid-flight (snapshot left on disk), recover_stale restores config."""
    from acctsw import paths as P
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\n')
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path)
    store = tmp_path / "hr-backup"
    # simulate a crash: write the snapshot, inject, but never restore
    headroom._write_snapshot(store, "codex")
    cfg.write_text('model_provider = "headroom"\n')
    # next launcher start:
    headroom.recover_stale(store)
    assert cfg.read_text() == 'model = "gpt-5.5"\n'  # crash injection undone


def test_scoped_does_not_rebaseline_dirty_config(tmp_path, monkeypatch):
    """A stale snapshot (prior crash) must be recovered before capturing a new baseline, so an
    already-injected config never becomes the 'pristine' baseline."""
    from acctsw import paths as P
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\n')
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path)
    store = tmp_path / "hr-backup"
    headroom._write_snapshot(store, "codex")          # prior session baseline (clean)
    cfg.write_text('model_provider = "headroom"\n')   # then it crashed, leaving injection
    # a new scoped() must restore the clean baseline first, not snapshot the dirty config
    with headroom.scoped("codex", store):
        pass
    assert cfg.read_text() == 'model = "gpt-5.5"\n'


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
