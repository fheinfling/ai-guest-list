"""Milestone-1 smoke tests: the package imports and the CLI parser is wired up."""
import pathlib
import re
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


def test_pyproject_version_matches_the_source_of_truth():
    """``acctsw.__version__`` is the single source of truth: setup.py regex-reads it for the app
    bundle and release.yml fails the build when the pushed tag disagrees. Nothing, however, validated
    pyproject.toml — which silently drifted to 0.2.3 while the app shipped 0.6.0. Pin them together
    so a release can never advertise two different versions."""
    root = pathlib.Path(__file__).resolve().parent.parent
    declared = re.search(r'(?m)^version\s*=\s*"([^"]+)"',
                         (root / "pyproject.toml").read_text())
    assert declared, "pyproject.toml has no [project] version"
    assert declared.group(1) == acctsw.__version__, (
        f"pyproject.toml says {declared.group(1)} but acctsw.__version__ is {acctsw.__version__}")


def test_setup_py_reads_the_same_version():
    """setup.py's own regex must still find it — a reformat of the assignment (e.g. adding a
    trailing comment on the next line) would otherwise break the bundle version silently."""
    root = pathlib.Path(__file__).resolve().parent.parent
    found = re.search(r'__version__\s*=\s*"([^"]+)"',
                      (root / "acctsw" / "__init__.py").read_text())
    assert found and found.group(1) == acctsw.__version__
