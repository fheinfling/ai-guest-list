"""CLI integration tests — drive acctsw.cli.main with an isolated Context."""
import json

import pytest

from acctsw import cli
from acctsw.context import Context
from tests.conftest import make_codex_blob


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    c = Context.for_test(tmp_path)
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: c))
    return c


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert "acctsw" in capsys.readouterr().out


def test_add_list_switch_status_flow(isolated, capsys):
    # sign in as a, add
    isolated.cred["codex"].set_live(make_codex_blob("a@x.com"))
    assert cli.main(["add", "codex", "--name", "work"]) == 0
    # sign in as b, add
    isolated.cred["codex"].set_live(make_codex_blob("b@x.com"))
    assert cli.main(["add", "codex"]) == 0

    capsys.readouterr()  # drain prior prints
    assert cli.main(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {s["email"] for s in data["codex"]} == {"a@x.com", "b@x.com"}

    # switch back to a
    assert cli.main(["switch", "codex", "a@x.com"]) == 0
    assert isolated.load_state().data["tools"]["codex"]["manual_switch"]["email"] == "a@x.com"
    capsys.readouterr()  # drain switch print
    assert cli.main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["tools"]["codex"]["active"] == "a@x.com"


def test_switch_unknown_returns_error(isolated):
    isolated.cred["codex"].set_live(make_codex_blob("a@x.com"))
    cli.main(["add", "codex"])
    assert cli.main(["switch", "codex", "ghost@x.com"]) == cli.EXIT_ERR


def test_cli_install_uninstall_dry_run(isolated, tmp_path, monkeypatch):
    """CLI install/uninstall wiring — dry-run must touch nothing on disk."""
    import acctsw.install as inst
    monkeypatch.setattr(inst, "BIN_DIR", tmp_path / "bin")
    isolated.cred["codex"].set_live(make_codex_blob("a@x.com"))
    assert cli.main(["install", "--dry-run"]) == 0
    assert cli.main(["uninstall", "--dry-run"]) == 0
    assert not (tmp_path / "bin").exists()  # dry-run wrote nothing


def test_cli_path_wires_rc(isolated, tmp_path, monkeypatch, capsys):
    import acctsw.install as inst
    rc = tmp_path / ".zshrc"
    monkeypatch.setattr(inst, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    assert cli.main(["path"]) == 0
    body = rc.read_text()
    assert inst.BLOCK_BEGIN in body and "alias codex=cx" in body
    assert "✓" in capsys.readouterr().out


def test_cli_path_idempotent(isolated, tmp_path, monkeypatch, capsys):
    import acctsw.install as inst
    rc = tmp_path / ".zshrc"
    monkeypatch.setattr(inst, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    cli.main(["path"]); capsys.readouterr()
    assert cli.main(["path"]) == 0                  # second run: no-op
    assert "·" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_remove_path(isolated, capsys):
    isolated.cred["codex"].set_live(make_codex_blob("a@x.com"))
    cli.main(["add", "codex"])
    capsys.readouterr()
    assert cli.main(["remove", "codex", "a@x.com"]) == 0
    assert "goodbye" in capsys.readouterr().out
    # removing again reports no-op, still exit 0
    assert cli.main(["remove", "codex", "a@x.com"]) == 0


def test_keychain_error_is_friendly(isolated, monkeypatch, capsys):
    """A keychain failure surfaces as a friendly stderr line, not a traceback.

    (Claude still uses the keychain for its snapshots; codex uses on-disk homes.)
    """
    from acctsw.keychain import KeychainError
    from tests.conftest import make_claude_blob
    isolated.cred["claude"].set_live(make_claude_blob())
    from acctsw import usage
    monkeypatch.setattr(usage, "claude_user_agent", lambda *_: "claude-code/fake")
    monkeypatch.setattr(usage, "_default_get", lambda *_:
                        (200, '{"account":{"email":"c@x.com"}}'))

    def boom(*a, **k):
        raise KeychainError("security: boom")
    monkeypatch.setattr(isolated.keychain, "set", boom)
    rc = cli.main(["add", "claude", "--email", "c@x.com"])
    assert rc == cli.EXIT_ERR
    assert "acctsw:" in capsys.readouterr().err


def test_ctrl_c_exits_cleanly(isolated, monkeypatch, capsys):
    """Ctrl-C (e.g. during the all-seats-resting wait) prints one friendly line and exits 130 —
    never the raw KeyboardInterrupt traceback the wait used to dump."""
    def interrupted(ctx, ns):
        raise KeyboardInterrupt
    monkeypatch.setitem(cli.HANDLERS, "list", interrupted)
    rc = cli.main(["list"])
    assert rc == 130
    err = capsys.readouterr().err
    assert "interrupted" in err and "Traceback" not in err


def test_daemons_json_and_fix(isolated, monkeypatch, capsys):
    from acctsw import codexhome
    state = isolated.load_state()
    state.upsert_seat('codex', 'a@x.com')
    state.save()
    home = isolated.codex_home('a@x.com')
    package = home / 'packages/app-server-daemon'
    for version in ['v1', 'v2']:
        release = package / 'releases' / version
        release.mkdir(parents=True)
        (release / 'codex').write_bytes(b'123')
    (package / 'current').symlink_to(package / 'releases/v2')
    monkeypatch.setattr(codexhome, 'orphan_daemons', lambda *args, **kwargs: [])
    monkeypatch.setattr(codexhome.daemonprocs, 'list_processes', lambda: [])
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', lambda *args: [])
    real_gc = codexhome.gc_daemon_releases
    monkeypatch.setattr(codexhome, 'gc_daemon_releases',
                        lambda home, **kw: real_gc(home, list_procs=lambda: [], **kw))
    assert cli.main(['daemons', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    row = report['seats'][0]
    assert row == {'address': 'a@x.com', 'home_id': home.name, 'state': 'absent', 'packages_bytes': 6}
    assert (package / 'releases/v1').exists()
    assert cli.main(['daemons', '--fix', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['seats'][0]['bytes_freed'] == 3
    assert report['seats'][0]['packages_bytes'] == 3
    assert not (package / 'releases/v1').exists()
    assert cli.main(['daemons']) == 0
    assert 'a@x.com' in capsys.readouterr().out


def test_daemons_text_reports_partial_inspection(isolated, monkeypatch, capsys):
    from acctsw import daemonprocs
    from tests.test_daemonprocs import supervisor
    monkeypatch.setattr(daemonprocs, 'list_processes', lambda: [supervisor(executable=None)])
    assert cli.main(['daemons']) == 0
    out = capsys.readouterr().out
    assert 'process inspection: partial (1 records incomplete)' in out
    assert 'unavailable' not in out


@pytest.mark.parametrize("tool", [None, "codex", "claude"])
@pytest.mark.parametrize("as_json", [False, True])
def test_usage_refresh_output(isolated, monkeypatch, capsys, tool, as_json):
    from acctsw import usage
    state = isolated.load_state()
    state.upsert_seat("codex", "ready@example.com")
    state.upsert_seat("codex", "resting@example.com")
    state.upsert_seat("claude", "claude@example.com")
    state.set_limited_until("codex", "resting@example.com", "2099-01-01T00:00:00Z")
    state.save()
    agents, refreshed = [], []

    def user_agent(binary):
        assert not isolated._store_lock_held()
        agents.append(binary)
        return "claude-code/fake"

    def refresh(ctx, current, **kwargs):
        assert ctx is isolated and ctx._store_lock_held()
        refreshed.append(kwargs)
        current.set_usage("codex", "ready@example.com", {
            "windows": {"5h": {"used_pct": 12.6}, "weekly": {"used_pct": 42}}})
        return {"updated": 1}

    monkeypatch.setattr(usage, "claude_user_agent", user_agent)
    monkeypatch.setattr(usage, "refresh", refresh)
    args = ["usage", "refresh"]
    if tool:
        args += ["--tool", tool]
    if as_json:
        args += ["--json"]
    assert cli.main(args) == cli.EXIT_OK
    assert agents == ([] if tool == "codex" else [isolated.claude_bin])
    assert refreshed == [{"tool": tool, "user_agent": (
        cli.P.CLAUDE_USER_AGENT_FALLBACK if tool == "codex" else "claude-code/fake")}]
    out = capsys.readouterr().out
    if as_json:
        result = json.loads(out)
        assert result["refresh"] == {"updated": 1}
        seat = result["status"]["tools"]["codex"]["seats"][0]
        assert seat["usage"]["windows"]["5h"]["used_pct"] == 12.6
    else:
        if tool in (None, "codex"):
            assert "codex:\n" in out
            assert "ready@example.com: 5h 13%, weekly 42%  [ok]" in out
            assert "resting@example.com: 5h —, weekly —  [resting]" in out
        else:
            assert "codex:" not in out
        if tool in (None, "claude"):
            assert "claude:\n  claude@example.com: 5h —, weekly —  [ok]" in out
        else:
            assert "claude:" not in out


def test_status_text_shows_active_resting_ready_and_empty_tool(isolated, capsys):
    state = isolated.load_state()
    for email in ("active@example.com", "resting@example.com", "ready@example.com"):
        state.upsert_seat("codex", email)
    state.set_active("codex", "active@example.com")
    state.set_limited_until("codex", "active@example.com", "2099-01-01T00:00:00Z")
    state.set_limited_until("codex", "resting@example.com", "2099-01-01T00:00:00Z")
    state.save()
    assert cli.main(["status"]) == cli.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "codex: active=active@example.com", "  [active] active@example.com",
        "  [ready] ready@example.com", "  [resting] resting@example.com", "claude: active=—"]


@pytest.mark.parametrize("signals", [[], [321]])
def test_daemons_fix_text_reports_maintenance(isolated, monkeypatch, capsys, signals):
    from acctsw import codexhome
    calls = []
    def report(ctx, *, fix):
        calls.append((ctx, fix))
        return {"seats": [{"address": "seat@example.com", "home_id": "seat-home",
                           "state": "stale", "packages_bytes": 10, "heal": "healed",
                           "gc": "cleaned", "bytes_freed": 20}],
                "process_inspection": "complete", "orphan_pids": [],
                "wedged_supervisors": signals, "signalled_supervisors": signals,
                "reaper": "ok", "reaped_pids": signals}
    monkeypatch.setattr(codexhome, "daemon_report", report)
    assert cli.main(["daemons", "--fix"]) == cli.EXIT_OK
    assert calls == [(isolated, True)]
    pids = "321" if signals else "none"
    assert capsys.readouterr().out.splitlines() == [
        "seat@example.com  seat-home  stale  packages=10 bytes",
        "  heal=healed  gc=cleaned  freed=20 bytes", "process inspection: complete",
        "orphan pids: none", f"wedged supervisors: {pids}",
        f"signalled supervisors: {pids}", "reaper: ok", f"reaped pids: {pids}"]
