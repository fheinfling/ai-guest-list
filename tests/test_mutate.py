"""The mutation harness never leaves a mutant in the tree it checks, however its run ends."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mutate.py"
SOURCE = "ANSWER = 42\n"


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo holding a copy of the harness, so ROOT is this repo, never the real one."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "mutate.py")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(SOURCE)
    (root / ".venv").mkdir()
    (root / ".venv" / "big").write_text("not copied")
    return root


def _harness(root):
    spec = importlib.util.spec_from_file_location("mutate_under_test", root / "scripts" / "mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(code):
    return {"label": "x", "file": "pkg/mod.py", "old": "42", "new": "43",
            "cmd": [sys.executable, "-c", code]}


def test_the_guard_runs_on_a_copy_and_the_real_tree_is_never_written(repo):
    real = repo / "pkg" / "mod.py"
    # Exit 0 only when the guard's cwd holds the mutant, the real file does not, and the copy
    # left out environments.
    check = (f"import pathlib, sys; here = pathlib.Path('pkg/mod.py').read_text(); "
             f"real = pathlib.Path({str(real)!r}).read_text(); "
             f"sys.exit(0 if here == 'ANSWER = 43\\n' and real == {SOURCE!r} "
             f"and not pathlib.Path('.venv').exists() else 1)")
    assert _harness(repo)._run(_entry(check)) == ("SURVIVED", "guard stayed green")
    assert real.read_text() == SOURCE


def test_a_run_killed_outright_leaves_no_mutant_behind(repo):
    """SIGKILL runs no finally: only never writing the real tree is safe."""
    spec = repo / "spec.json"
    spec.write_text(json.dumps([_entry("import os, signal; os.kill(os.getppid(), signal.SIGKILL)")]))
    res = subprocess.run([sys.executable, str(repo / "scripts" / "mutate.py"), str(spec)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == -9
    assert (repo / "pkg" / "mod.py").read_text() == SOURCE


def test_a_guard_that_hangs_is_a_timeout_not_a_verdict(repo):
    harness = _harness(repo)
    harness.TIMEOUT_S = 1
    assert harness._run(_entry("import time; time.sleep(30)"))[0] == "TIMEOUT"
    assert (repo / "pkg" / "mod.py").read_text() == SOURCE
