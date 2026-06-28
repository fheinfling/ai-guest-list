"""Milestone-1 smoke tests: the package imports and the CLI parser is wired up."""
import subprocess
import sys

import acctsw
from acctsw.cli import build_parser


def test_version_constant():
    assert acctsw.__version__
    assert acctsw.TOOLS == ("codex", "claude")


def test_parser_has_core_commands():
    parser = build_parser()
    # argparse stores subparser choices on the _SubParsersAction.
    sub = next(a for a in parser._actions if a.dest == "command")
    for cmd in ("install", "uninstall", "add", "remove", "list", "status", "usage", "switch", "run"):
        assert cmd in sub.choices, f"missing subcommand: {cmd}"


def test_module_runs_as_main():
    out = subprocess.run(
        [sys.executable, "-m", "acctsw", "--version"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert "acctsw" in out.stdout
