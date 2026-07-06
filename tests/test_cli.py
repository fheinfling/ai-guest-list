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
