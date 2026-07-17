"""Unit tests for the legacy-Headroom cleanup migration (acctsw/headroom.py).

The "save credit" compression proxy was removed; this module only tears down what older builds left
behind. Tests are hermetic: paths point at tmp dirs, no real ~/.codex/~/.claude, no real process.
"""
import json
import os

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


def test_strip_codex_routing_keeps_a_users_own_provider_keys():
    """The strip is keyed on OUR values, not the key names. A user's own model_provider /
    openai_base_url must survive — cleanup runs for everyone with any leftover state, and
    `_unroute_all` fires when EITHER tool is injected, so a Claude-only leftover reaches this."""
    src = 'model_provider = "openai"\nopenai_base_url = "https://my.corp/v1"\n\n[foo]\nbar = 1\n'
    assert hr._strip_codex_routing(src) == src


def test_strip_codex_routing_survives_out_of_order_markers():
    """A hand-edited/corrupted config can hold the markers reversed. `index(END, start)` would raise
    ValueError straight through cleanup_legacy; an unpaired marker must simply be left alone."""
    src = ('# --- end acctsw headroom routing ---\nmodel = "gpt-5"\n'
           '# --- acctsw headroom routing ---\n')
    assert hr._strip_codex_routing(src) == src        # no raise, nothing destroyed


def test_strip_codex_routing_removes_our_line_with_a_trailing_comment():
    """The line is detected as injected either way, so a pattern that can't strip what it detects
    leaves cleanup permanently 'partial' — re-running the whole migration on every launch forever."""
    for src in ('model_provider = "headroom" # routing\n',
                'openai_base_url = "http://127.0.0.1:8787/v1"  # ours\n'):
        out = hr._strip_codex_routing(src)
        assert not any(m in out for m in hr.INJECT_MARKERS), f"would retry forever: {src!r}"


def test_unroute_codex_leaves_an_unrelated_config_untouched(ctx, tmp_path, monkeypatch):
    """Claude-side leftover + a Codex config that never routed → Codex config must not be rewritten."""
    codex = tmp_path / "codex"; claude = tmp_path / "claude"
    codex.mkdir(); claude.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", claude)
    cfg = codex / "config.toml"
    original = 'model_provider = "openai"\nopenai_base_url = "https://my.corp/v1"\n'
    cfg.write_text(original)
    (claude / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}}))
    did, _ = hr.cleanup_legacy(ctx)
    assert did is True
    assert cfg.read_text() == original               # the user's real provider config is intact


def test_restore_global_records_corrupt_entry_as_failure(ctx, tmp_path):
    """A truncated backup entry must degrade to a recorded failure, never an exception: this runs on
    the cx/cl path, where escaping would stop the user launching codex/claude at all."""
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    target = tmp_path / "config.toml"
    (bdir / "manifest.json").write_text(json.dumps({"0": str(target)}))
    (bdir / "0.json").write_text("{truncated-not-json")
    ok, failures = hr.restore_global(ctx.data_dir)
    assert ok is False and len(failures) == 1
    assert bdir.exists()                             # kept for a retry, not silently dropped


def test_cleanup_survives_a_corrupt_backup(ctx, tmp_path, monkeypatch):
    """cleanup_legacy must not raise through to its callers when the snapshot is unreadable."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(tmp_path / "codex" / "config.toml")}))
    (bdir / "0.json").write_text("}not json{")
    did, msg = hr.cleanup_legacy(ctx)
    assert did is True and "will retry" in msg
    assert hr.legacy_present(ctx) is True            # backup retained → next launch retries


def test_restore_never_deletes_a_target_it_cannot_restore(ctx, tmp_path):
    """An entry we can't fully understand must leave the file ALONE. Unlinking first and discovering
    the entry is unusable afterwards destroys the only copy — the snapshot IS the backup."""
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    target = tmp_path / "config.toml"
    original = 'model = "gpt-5"\n'
    target.write_text(original)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(target)}))
    (bdir / "0.json").write_text(json.dumps({"kind": "bogus"}))     # unknown kind
    ok, failures = hr.restore_global(ctx.data_dir)
    assert ok is False and len(failures) == 1
    assert target.read_text() == original          # NOT deleted
    assert bdir.exists()                           # backup kept, not dropped as a "success"


def test_restore_rejects_a_corrupt_base64_payload_instead_of_writing_empty(ctx, tmp_path):
    """b64decode's default silently DISCARDS non-alphabet chars, so a corrupt payload decodes to b''
    and would overwrite the user's real config with an empty file."""
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    target = tmp_path / "config.toml"
    target.write_text('model = "gpt-5"\n')
    (bdir / "manifest.json").write_text(json.dumps({"0": str(target)}))
    (bdir / "0.json").write_text(json.dumps({"kind": "file", "b64": "!!!!", "mode": 0o600}))
    ok, failures = hr.restore_global(ctx.data_dir)
    assert ok is False and len(failures) == 1
    assert target.read_text() == 'model = "gpt-5"\n'   # not clobbered with b""


def test_partial_restore_does_not_replay_succeeded_entries(ctx, tmp_path):
    """A retained backup is re-read every launch. Replaying an already-restored entry would overwrite
    whatever the user edited since — forever. Only failed entries may be retried."""
    import base64
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    good = tmp_path / "config.toml"; bad = tmp_path / "settings.json"
    (bdir / "manifest.json").write_text(json.dumps({"0": str(good), "1": str(bad)}))
    (bdir / "0.json").write_text(json.dumps(
        {"kind": "file", "b64": base64.b64encode(b"restored\n").decode(), "mode": 0o600}))
    (bdir / "1.json").write_text("{corrupt")
    ok, _ = hr.restore_global(ctx.data_dir)
    assert ok is False and good.read_text() == "restored\n"
    good.write_text("user edited this later\n")       # user moves on with their life
    hr.restore_global(ctx.data_dir)                   # next launch retries the failed entry
    assert good.read_text() == "user edited this later\n"   # the edit SURVIVES


def test_restore_never_touches_files_routing_did_not_write(ctx, tmp_path):
    """The old snapshot captured the wider 'files headroom wrap MAY touch' set (AGENTS.md, CLAUDE.md,
    .mcp.json, settings.local.json), but _route_all only ever WROTE config.toml + settings.json.
    Replaying a months-old snapshot over the rest silently reverts the user's work — and an entry
    recorded as 'absent' DELETES a CLAUDE.md they have written since. Restore only what we broke."""
    import base64
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    agents = tmp_path / "AGENTS.md"; claude_md = tmp_path / "CLAUDE.md"; mcp = tmp_path / ".mcp.json"
    entries = [
        (agents, {"kind": "file", "b64": base64.b64encode(b"# old\n").decode(), "mode": 0o600}),
        (claude_md, {"kind": "absent"}),           # did not exist when save-credit was enabled
        (mcp, {"kind": "file", "b64": base64.b64encode(b"{}\n").decode(), "mode": 0o600}),
    ]
    mf = {}
    for i, (p, e) in enumerate(entries):
        (bdir / f"{i}.json").write_text(json.dumps(e)); mf[str(i)] = str(p)
    (bdir / "manifest.json").write_text(json.dumps(mf))
    # The user has since done months of work on all three.
    agents.write_text("# months of work\n"); claude_md.write_text("# months of work\n")
    mcp.write_text('{"servers": {}}\n')
    ok, failures = hr.restore_global(ctx.data_dir)
    assert ok is True and failures == []
    assert agents.read_text() == "# months of work\n"      # NOT reverted
    assert claude_md.read_text() == "# months of work\n"   # NOT deleted
    assert mcp.read_text() == '{"servers": {}}\n'          # NOT reverted


def test_strip_codex_routing_ignores_a_table_scoped_key():
    """`model_provider` under [profiles.work] is profiles.work.model_provider — a different key we
    never wrote. _route_codex always wrote its keys as the first lines of the file."""
    src = '[profiles.work]\nmodel_provider = "headroom"\n'
    assert hr._strip_codex_routing(src) == src


def test_unroute_preserves_a_symlinked_config(ctx, tmp_path, monkeypatch):
    """A dotfiles setup symlinks ~/.codex/config.toml. atomic_write via os.replace would swap the
    LINK for a regular file and leave the real file still routed."""
    codex = tmp_path / "codex"; codex.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    real = tmp_path / "dotfiles" / "codex.toml"
    real.parent.mkdir()
    real.write_text('model_provider = "headroom"\nmodel = "gpt-5"\n')
    (codex / "config.toml").symlink_to(real)
    hr._unroute_codex()
    assert (codex / "config.toml").is_symlink()        # the link survives
    assert "headroom" not in real.read_text()          # and the real file is actually cleaned
    assert 'model = "gpt-5"' in real.read_text()


def test_pid_identity_rejects_an_innocent_bystander(monkeypatch):
    """`"headroom" in cmd and "proxy" in cmd` also matches `tail -f headroom-proxy.log`. Killing a
    recycled PID because its argv mentions our log file would be catastrophic."""
    class _R:
        stdout = "tail -f /Users/x/.account-switcher/headroom-proxy.log"
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _R())
    assert hr._pid_is_proxy(4242) is False


def test_pid_identity_accepts_the_real_proxy(monkeypatch):
    class _R:
        stdout = "/Users/x/.account-switcher/hr-venv/bin/headroom proxy --host 127.0.0.1 --port 8787"
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _R())
    assert hr._pid_is_proxy(4242) is True


def test_pid_identity_unknown_when_ps_unavailable(monkeypatch):
    """None (not False) so stop_proxy can tell 'not ours' from 'couldn't tell' and keep the pidfile."""
    def _boom(*a, **k):
        raise OSError("no ps")
    monkeypatch.setattr(hr.subprocess, "run", _boom)
    assert hr._pid_is_proxy(4242) is None


def test_stop_proxy_keeps_pidfile_when_identity_unverifiable(ctx, monkeypatch):
    """Deleting the pidfile after failing to verify loses track of a possibly-live proxy forever."""
    pidfile = hr._proxy_pidfile(ctx.data_dir)
    pidfile.write_text(str(os.getpid()))               # a real, live pid
    monkeypatch.setattr(hr, "_pid_is_proxy", lambda pid: None)
    killed = []
    hr.stop_proxy(ctx.data_dir, kill=lambda p, s: killed.append(s), sleep=lambda _: None)
    assert killed == []                                # never signalled
    assert pidfile.exists()                            # and still tracked for a retry


def test_cleanup_does_not_sweep_away_a_retained_pidfile(ctx, tmp_path, monkeypatch):
    """stop_proxy keeps the pidfile when it can't prove the PID is ours. The leftover-files sweep
    must not then delete it — that loses the only handle on a possibly-live proxy, and
    legacy_present() would report clean so the reap could never be retried."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    pidfile = hr._proxy_pidfile(ctx.data_dir)
    pidfile.write_text(str(os.getpid()))                  # a real, live pid
    monkeypatch.setattr(hr, "_pid_is_proxy", lambda pid: None)   # ps can't tell us
    hr.cleanup_legacy(ctx)
    assert pidfile.exists()                               # still tracked
    assert hr.legacy_present(ctx) is True                 # so a later launch retries the reap


def test_cleanup_keeps_proxy_and_venv_while_routing_remains(ctx, tmp_path, monkeypatch):
    """Stopping the proxy while routing survives points the tools at a DEAD port — strictly worse
    than leaving it up until a later launch finishes unrouting."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    venv = hr.hr_venv_dir(ctx.data_dir); venv.mkdir(parents=True)
    monkeypatch.setattr(hr, "_any_injected", lambda: True)      # unroute can't clean it
    stopped = []
    monkeypatch.setattr(hr, "stop_proxy", lambda *a, **k: stopped.append(1))
    did, msg = hr.cleanup_legacy(ctx)
    assert did is True and "will retry" in msg
    assert stopped == [] and venv.exists()             # both retained for the retry


def test_restore_global_rejects_a_non_object_manifest(ctx):
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps(["not", "an", "object"]))
    ok, failures = hr.restore_global(ctx.data_dir)
    assert ok is False and "malformed" in failures[0]


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
