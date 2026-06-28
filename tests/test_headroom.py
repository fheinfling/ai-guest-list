"""Tests for the Headroom save-credit integration."""
from acctsw import accounts as acct
from acctsw import bridge
from acctsw import headroom
from acctsw import launcher as L
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get


def test_wrap_uses_per_tool_subcommand():
    assert L  # ensure import
    out = headroom.wrap("codex", ["resume", "--last"], enabled=True, is_available=True, exe="headroom")
    assert out == ["headroom", "wrap", "codex", "--", "resume", "--last"]


def test_wrap_no_args_omits_separator():
    assert headroom.wrap("claude", [], enabled=True, is_available=True) == ["headroom", "wrap", "claude"]


def test_no_wrap_when_disabled():
    assert headroom.wrap("codex", ["x"], enabled=False, is_available=True) is None


def test_no_wrap_when_unavailable():
    assert headroom.wrap("codex", ["x"], enabled=True, is_available=False) is None


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


def test_launcher_wraps_with_headroom_when_enabled(ctx, monkeypatch):
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    monkeypatch.setattr(L.headroom_mod, "headroom_path", lambda: "/fake/bin/headroom")
    spawn = _Spawn()
    L.run(ctx, "codex", ["--foo"], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][0].endswith("headroom")
    assert spawn.calls[0][1] == "wrap"
    assert spawn.calls[0][-1] == "--foo"


def test_launcher_skips_headroom_when_unavailable(ctx, monkeypatch):
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    monkeypatch.setattr(L.headroom_mod, "headroom_path", lambda: None)
    msgs = []
    spawn = _Spawn()
    L.run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert spawn.calls[0][1:2] != ["wrap"]
    assert any("headroom isn't installed" in m for m in msgs)


def test_launcher_no_headroom_when_toggle_off(ctx, monkeypatch):
    _two_codex(ctx)  # headroom defaults off
    monkeypatch.setattr(L.headroom_mod, "headroom_path", lambda: "/fake/bin/headroom")
    spawn = _Spawn()
    L.run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][1:2] != ["wrap"]


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
