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


def test_not_implemented_exit_code(isolated):
    assert cli.main(["run", "codex"]) == cli.EXIT_NOIMPL


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()
