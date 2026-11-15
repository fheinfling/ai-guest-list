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


def test_node_runner_reports_no_test_when_pattern_matches_nothing(repo):
    (repo / "pkg" / "mod.mjs").write_text("export const answer = 42;\n")
    (repo / "pkg" / "mod.test.mjs").write_text(
        'import test from "node:test";\n'
        'import assert from "node:assert/strict";\n'
        'import { answer } from "./mod.mjs";\n'
        'test("guards answer", () => { assert.equal(answer, 42); });\n'
        'test("always green", () => { assert.equal(1, 1); });\n'
    )
    harness = _harness(repo)

    unmatched = {
        "label": "missing pattern",
        "file": "pkg/mod.mjs",
        "old": "42",
        "new": "43",
        "cmd": ["node", "--test", "--test-name-pattern=does not match anything", "pkg/mod.test.mjs"],
    }
    assert "--test-reporter=tap" in harness._prepare_cmd(unmatched)
    assert harness._run(unmatched) == ("NO-TEST", "the selection matched nothing")

    guarded = {**unmatched, "label": "matching failing test",
               "cmd": ["node", "--test", "--test-name-pattern=guards answer", "pkg/mod.test.mjs"]}
    assert harness._run(guarded) == ("GUARDED", "")

    survived = {**unmatched, "label": "matching passing test",
                "cmd": ["node", "--test", "--test-name-pattern=always green", "pkg/mod.test.mjs"]}
    assert harness._run(survived) == ("SURVIVED", "guard stayed green")


def test_every_mutant_spec_matches_its_target_file_once(repo):
    real_root = SCRIPT.parent.parent
    harness = _harness(repo)

    missing = {"label": "stale", "file": "pkg/mod.py", "old": "999", "new": "0", "cmd": ["true"]}
    assert harness._run(missing) == ("NO-OP", "'old' occurs 0x in pkg/mod.py")

    specs = sorted((real_root / "tests" / "mutants").glob("*.json"))
    assert specs
    stale = []
    for spec in specs:
        for idx, entry in enumerate(json.loads(spec.read_text())):
            noop = harness._check_target(entry, real_root)
            if noop is not None:
                stale.append(f"{spec.name}#{idx} ({entry['label']}): {noop[1]}")
    assert stale == []


def test_mutate_diff_uses_portable_tempdir_defaults():
    diff_script = SCRIPT.parent / "mutate_diff.py"
    spec = importlib.util.spec_from_file_location("mutate_diff_under_test", diff_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "/tmp/claude-" not in str(mod.KNOWN_BASELINE)
    assert mod.KNOWN_BASELINE.parent == mod.DEFAULT_TMP
    source = diff_script.read_text()
    assert "/tmp/claude-" not in source


def test_screenshots_shot_css_targets_current_body_scroll_containers():
    shot_script = SCRIPT.parent / "screenshots.py"
    spec = importlib.util.spec_from_file_location("screenshots_under_test", shot_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "#root .body, #root .set-body" in mod.SHOT_CSS
    assert ".main-body" not in mod.SHOT_CSS


def test_check_layout_is_self_contained_and_targets_current_ui(tmp_path):
    layout_script = SCRIPT.parent / "check-layout.py"
    assert not (SCRIPT.parent / "design-gallery.py").exists()
    spec = importlib.util.spec_from_file_location("check_layout_under_test", layout_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    states = mod.gallery_states()
    ids = [entry["id"] for entry in states]
    assert ids == [
        "main", "spending", "asking", "asking-unpriced", "trouble",
        "settings", "models", "models-unpriced", "long-identities",
        "spending-overflow", "trouble-overflow",
    ]
    assert ".body, .set-body:not(.picker-body), .model-list, .key-prompts:not(:empty)" in mod.MEASURE
    assert ".roster" not in mod.MEASURE
    assert ".guest-drawer" not in layout_script.read_text()
    assert "r.width >= 28 && r.height >= 26 - tolerance" in mod.MEASURE

    res = subprocess.run(
        [sys.executable, str(layout_script)],
        env={"PATH": "", "AGL_CHROME": str(tmp_path / "no-such-chrome")},
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 2
    assert "ERROR:" in res.stderr
