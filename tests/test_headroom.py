"""Unit tests for the legacy-Headroom cleanup migration (acctsw/headroom.py).

The "save credit" compression proxy was removed; this module only tears down what older builds left
behind. Tests are hermetic: paths point at tmp dirs, no real ~/.codex/~/.claude, no real process.
"""
import json

import pytest

from acctsw import headroom as hr
from acctsw import paths as P


@pytest.fixture
def routed(ctx, tmp_path, monkeypatch):
    """A ctx whose codex/claude config dirs are tmp dirs, with old Headroom routing injected."""
    codex = tmp_path / "codex"; claude = tmp_path / "claude"
    codex.mkdir(); claude.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", claude)
    (codex / "config.toml").write_text(
        'model_provider = "headroom"\n'
        'openai_base_url = "http://127.0.0.1:8787/v1"\n\n'
        'model = "gpt-5"\n\n'
        '# --- acctsw headroom routing ---\n[model_providers.headroom]\n'
        'base_url = "http://127.0.0.1:8787/v1"\n# --- end acctsw headroom routing ---\n')
    (claude / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787", "ENABLE_TOOL_SEARCH": "true",
                 "KEEP_ME": "1"}, "other": 2}))
    return ctx


def test_legacy_present_false_on_clean(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    assert hr.legacy_present(ctx) is False
    assert hr.cleanup_legacy(ctx) == (False, "clean")


def test_strip_codex_routing_removes_block_and_top_keys():
    src = ('model_provider = "headroom"\nopenai_base_url = "http://127.0.0.1:8787/v1"\n\n'
           'model = "gpt-5"\n\n# --- acctsw headroom routing ---\n[model_providers.headroom]\n'
           'base_url = "x"\n# --- end acctsw headroom routing ---\n')
    out = hr._strip_codex_routing(src)
    assert "headroom" not in out
    assert 'model = "gpt-5"' in out                 # the user's real config is preserved


def test_legacy_present_detects_injection(routed):
    assert hr._any_injected() is True
    assert hr.legacy_present(routed) is True


def test_cleanup_strips_routing_and_preserves_user_config(routed):
    did, _ = hr.cleanup_legacy(routed)
    assert did is True
    codex = (P.CODEX_HOME / "config.toml").read_text()
    assert "headroom" not in codex and 'model = "gpt-5"' in codex
    claude = json.loads((P.CLAUDE_CONFIG_DIR / "settings.json").read_text())
    assert "ANTHROPIC_BASE_URL" not in claude["env"] and "ENABLE_TOOL_SEARCH" not in claude["env"]
    assert claude["env"]["KEEP_ME"] == "1" and claude["other"] == 2   # unrelated keys untouched
    assert hr.legacy_present(routed) is False        # idempotent: nothing left to clean


def test_cleanup_restores_from_backup_when_present(ctx, tmp_path, monkeypatch):
    """A snapshot backup (the old enable path saved one) is restored byte-exact over a surgical strip."""
    codex = tmp_path / "codex"; codex.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    cfg = codex / "config.toml"
    cfg.write_text('model_provider = "headroom"\nopenai_base_url = "http://127.0.0.1:8787/v1"\n')
    import base64
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    orig = 'model_provider = "openai"\n'
    (bdir / "0.json").write_text(json.dumps(
        {"kind": "file", "b64": base64.b64encode(orig.encode()).decode(), "mode": 0o600,
         "path": str(cfg)}))
    (bdir / "manifest.json").write_text(json.dumps({"0": str(cfg)}))
    hr.cleanup_legacy(ctx)
    assert cfg.read_text() == orig                    # exact restore, not a surgical strip
    assert not bdir.exists()


def test_cleanup_strips_claude_settings_local_json(ctx, tmp_path, monkeypatch):
    """settings.local.json overrides settings.json, so routing there would keep Claude on the dead
    proxy — detection scans it, and cleanup must strip it too (not just settings.json)."""
    codex = tmp_path / "codex"; claude = tmp_path / "claude"
    codex.mkdir(); claude.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", claude)
    (claude / "settings.local.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787", "KEEP": "1"}}))
    assert hr.legacy_present(ctx) is True
    did, _ = hr.cleanup_legacy(ctx)
    assert did is True
    env = json.loads((claude / "settings.local.json").read_text())["env"]
    assert "ANTHROPIC_BASE_URL" not in env and env["KEEP"] == "1"
    assert hr.legacy_present(ctx) is False        # not stuck true every launch


def test_cleanup_keeps_backup_when_restore_fails(ctx, tmp_path, monkeypatch):
    """A failed restore must NOT delete the snapshot — it's the only exact copy of the user's original
    provider config (routing overwrote it). Keep it so a later launch can retry; report 'partial'."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(tmp_path / "codex" / "config.toml")}))
    monkeypatch.setattr(hr, "restore_global", lambda store=None: (False, ["boom"]))
    did, msg = hr.cleanup_legacy(ctx)
    assert did is True and "will retry" in msg
    assert bdir.exists() and (bdir / "manifest.json").exists()   # backup RETAINED for a retry
    assert hr.legacy_present(ctx) is True                        # so the next launch tries again


def test_cleanup_removes_venv_and_leftovers_and_settings(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    d = ctx.data_dir
    hr.hr_venv_dir(d).mkdir(parents=True)
    (d / "headroom-proxy.pid").write_text("999999")   # stale pid; _pid_is_proxy(ps) won't match → just unlinked
    (d / "headroom.log").write_text("x")
    (d / "rtk.sha256").write_text("abc")
    st = ctx.load_state(); st.data["headroom_event"] = {"reason": "x"}
    st.settings()["headroom"] = True; st.settings()["savings_level"] = "aggressive"; st.save()

    assert hr.legacy_present(ctx) is True
    did, _ = hr.cleanup_legacy(ctx)
    assert did is True
    assert not hr.hr_venv_dir(d).exists()
    assert not (d / "headroom-proxy.pid").exists()
    assert not (d / "headroom.log").exists()
    s = ctx.load_state().settings()
    assert "headroom" not in s and "savings_level" not in s
    assert ctx.load_state().data.get("headroom_event") is None
    assert hr.legacy_present(ctx) is False
