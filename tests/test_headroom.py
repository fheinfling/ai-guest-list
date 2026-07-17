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


def _snapshot(ctx, path, original: bytes):
    """A legacy snapshot in the REAL schema the old snapshot_global() wrote (including 'path')."""
    import base64
    bdir = hr._global_backup(ctx.data_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(path)}))
    (bdir / "0.json").write_text(json.dumps(
        {"kind": "file", "path": str(path), "mode": 0o600,
         "b64": base64.b64encode(original).decode()}))
    return bdir


def test_cleanup_merges_and_keeps_edits_made_since_enabling(ctx, tmp_path, monkeypatch):
    """THE upgrade case. The snapshot is a whole-file copy from whenever save-credit was enabled.
    Replaying it reverts every unrelated edit made since. Keep the live file, drop our routing, and
    take back ONLY the provider keys routing overwrote."""
    codex = tmp_path / "codex"; codex.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    cfg = codex / "config.toml"
    _snapshot(ctx, cfg, b'model_provider = "openai"\nmodel = "gpt-5"\n')     # March
    cfg.write_text('model_provider = "headroom"\nopenai_base_url = "http://127.0.0.1:8787/v1"\n\n'
                   'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n')  # July + our routing
    did, msg = hr.cleanup_legacy(ctx)
    assert did is True
    out = cfg.read_text()
    assert 'model_provider = "openai"' in out          # the key routing OVERWROTE is recovered
    assert 'model = "gpt-5.5"' in out                  # ...and July's edits SURVIVE
    assert 'model_reasoning_effort = "xhigh"' in out
    assert "headroom" not in out and "8787" not in out
    assert hr.legacy_present(ctx) is False


def test_cleanup_does_not_delete_a_config_recorded_absent(ctx, tmp_path, monkeypatch):
    """If config.toml did not exist when save-credit was enabled, the snapshot says kind='absent'.
    Replaying that DELETES the config the user has written since."""
    import base64
    codex = tmp_path / "codex"; codex.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    cfg = codex / "config.toml"
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(cfg)}))
    (bdir / "0.json").write_text(json.dumps({"kind": "absent", "path": str(cfg)}))
    cfg.write_text('model_provider = "headroom"\nmodel = "gpt-5.5"\n')
    hr.cleanup_legacy(ctx)
    assert cfg.exists()                                # NOT deleted
    assert 'model = "gpt-5.5"' in cfg.read_text()      # their work survives
    assert "headroom" not in cfg.read_text()           # routing still removed


def test_cleanup_merges_claude_env_without_touching_the_rest(ctx, tmp_path, monkeypatch):
    """_route_claude assigned into env unconditionally, so the user's own values for those two keys
    survive only in the snapshot. Everything else in settings.json is theirs."""
    claude = tmp_path / "claude"; claude.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", claude)
    settings = claude / "settings.json"
    _snapshot(ctx, settings, json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my.corp"}}).encode())
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                                            "ENABLE_TOOL_SEARCH": "true", "MINE": "1"},
                                    "addedLater": True}))
    hr.cleanup_legacy(ctx)
    payload = json.loads(settings.read_text())
    assert payload["env"]["ANTHROPIC_BASE_URL"] == "https://my.corp"   # their value is back
    assert payload["env"]["MINE"] == "1"                               # untouched
    assert payload["addedLater"] is True                               # edits since enable survive
    assert "ENABLE_TOOL_SEARCH" not in payload["env"]                  # ours, and they had none


def test_snapshot_text_rejects_a_path_disagreement(ctx, tmp_path):
    """The snapshot recorded the path in the entry too; a corrupted manifest must not hand us another
    file's contents to merge in."""
    import base64
    cfg = tmp_path / "config.toml"
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(cfg)}))
    (bdir / "0.json").write_text(json.dumps(
        {"kind": "file", "path": "/somewhere/else/config.toml", "mode": 0o600,
         "b64": base64.b64encode(b'model_provider = "evil"\n').decode()}))
    assert hr._snapshot_text(ctx.data_dir, cfg) is None


def test_snapshot_text_rejects_corrupt_payload_and_bad_ids(ctx, tmp_path):
    cfg = tmp_path / "config.toml"
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({"0": str(cfg)}))
    (bdir / "0.json").write_text(json.dumps({"kind": "file", "path": str(cfg), "b64": "!!!!"}))
    assert hr._snapshot_text(ctx.data_dir, cfg) is None      # b64 must not decode to b""
    (bdir / "manifest.json").write_text(json.dumps({"../../etc/x": str(cfg)}))
    assert hr._snapshot_text(ctx.data_dir, cfg) is None      # id must be canonical


def test_has_backup_is_false_for_an_emptied_manifest(ctx):
    bdir = hr._global_backup(ctx.data_dir); bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({}))
    assert hr.has_backup(ctx.data_dir) is False    # else legacy_present() is true forever


def test_migration_lock_never_blocks_the_launch(ctx, tmp_path, monkeypatch):
    """The oplock is non-blocking on purpose: it sits on the cx/cl launch path. If another process is
    migrating, we let it — we must never stall the tool starting."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    hr.hr_venv_dir(ctx.data_dir).mkdir(parents=True)          # ensure there IS work to do
    assert hr.legacy_present(ctx) is True
    with hr._oplocked(ctx.data_dir):                          # someone else holds it
        did, msg = hr.cleanup_legacy(ctx)
    assert did is False                                       # returned immediately, no deadlock


def test_pid_identity_rejects_a_lookalike_binary(monkeypatch):
    """`"headroom" in name` also matches `headroom-worker`; a substring match would SIGKILL it."""
    class _R:
        stdout = "/usr/local/bin/headroom-worker proxy --port 8787"
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _R())
    assert hr._pid_is_proxy(4242) is False


def test_unusable_pidfile_keeps_the_proxy_tracked(ctx, tmp_path, monkeypatch):
    """The old writer wrote this file non-atomically, so a hard kill can leave it empty while the
    proxy lives. Collapsing that to 'no proxy' deletes its venv and forgets it forever."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    hr._proxy_pidfile(ctx.data_dir).write_text("")            # truncated mid-write
    assert hr._proxy_pid(ctx.data_dir) == -1
    assert hr.legacy_present(ctx) is True
    venv = hr.hr_venv_dir(ctx.data_dir); venv.mkdir(parents=True)
    assert hr.stop_proxy(ctx.data_dir) is False               # not "confirmed gone"
    hr.cleanup_legacy(ctx)
    assert venv.exists()                                      # venv retained for a retry


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


def test_pid_identity_accepts_a_console_script_run_via_its_shebang(monkeypatch):
    """The REAL shape. The proxy was a pip console script, so the kernel execs the interpreter and ps
    reports `<python> .../hr-venv/bin/headroom proxy ...` — argv[0] is Python, NOT headroom. Matching
    on argv[0] alone calls the real proxy foreign, drops its pidfile and deletes its venv while it
    keeps running. Verified against actual `ps` output for a shebang script."""
    class _R:
        stdout = ("/opt/homebrew/Cellar/python@3.13/.../Resources/Python.app/Contents/MacOS/Python "
                  "/Users/x/.account-switcher/hr-venv/bin/headroom proxy --host 127.0.0.1 --port 8787")
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _R())
    assert hr._pid_is_proxy(4242) is True


def test_pid_identity_rejects_a_bystander_naming_our_log_anywhere_in_argv(monkeypatch):
    """The looser 'headroom token anywhere' match must still reject a process that merely NAMES our
    files — `proxy` has to be an argument after the headroom binary, not just present somewhere."""
    for cmd in ("tail -f /Users/x/.account-switcher/headroom-proxy.log",
                "grep proxy /Users/x/.account-switcher/headroom.log",
                "vim headroom-proxy.log"):
        class _R:
            stdout = cmd
        monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _R())
        assert hr._pid_is_proxy(4242) is False, cmd


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the 0o000 mode this test relies on")
def test_unreadable_config_is_not_treated_as_clean(ctx, tmp_path, monkeypatch):
    """A file that EXISTS but can't be inspected is not 'clean'. Treating it as clean lets cleanup
    stop the proxy and delete the venv while the file is still routed to a now-dead port."""
    codex = tmp_path / "codex"; codex.mkdir()
    monkeypatch.setattr(P, "CODEX_HOME", codex)
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    cfg = codex / "config.toml"
    cfg.write_text('model_provider = "headroom"\n')
    cfg.chmod(0o000)                                   # exists, unreadable
    try:
        assert hr._any_unknown() is True
        venv = hr.hr_venv_dir(ctx.data_dir); venv.mkdir(parents=True)
        did, msg = hr.cleanup_legacy(ctx)
        assert did is True and "will retry" in msg
        assert venv.exists()                           # teardown withheld until we can actually look
    finally:
        cfg.chmod(0o600)


def test_venv_survives_when_the_proxy_cannot_be_confirmed_stopped(ctx, tmp_path, monkeypatch):
    """The venv is what a surviving proxy is RUNNING FROM. Deleting it while the process is alive
    strands a live proxy with its executable pulled out from under it."""
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    venv = hr.hr_venv_dir(ctx.data_dir); venv.mkdir(parents=True)
    hr._proxy_pidfile(ctx.data_dir).write_text(str(os.getpid()))
    monkeypatch.setattr(hr, "_pid_is_proxy", lambda pid: None)      # can't verify
    did, msg = hr.cleanup_legacy(ctx)
    assert did is True and "will retry" in msg
    assert venv.exists() and hr._proxy_pidfile(ctx.data_dir).exists()


def test_stop_proxy_reports_confirmed_gone_for_a_dead_pid(ctx):
    hr._proxy_pidfile(ctx.data_dir).write_text("999999")            # not a live pid
    assert hr.stop_proxy(ctx.data_dir) is True
    assert not hr._proxy_pidfile(ctx.data_dir).exists()


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
