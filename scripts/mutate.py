#!/usr/bin/env python3
"""Hand-picked mutation check: break one named invariant at a time and prove its guard goes red.

Bulk mutation tools spend most of their time on mutants nobody cares about. The rules that matter
here are few and silent when broken (a signed-out seat offered for a switch, a dead credential
polled forever), so each one is mutated directly and the test that claims to guard it must fail.

Spec files live in ``tests/mutants/*.json``. Each entry::

    {"label": "...", "file": "acctsw/usage.py", "old": "exact source", "new": "mutated source",
     "test": ["tests/test_usage.py::test_name"]}      # pytest node ids, or
     "cmd": ["node", "--test", "app/web/render.test.mjs"]   # any command, run from the repo root

``old`` must occur exactly once in ``file``. Each mutant is written into a fresh throwaway copy of
the working tree and its guard runs there, so the real tree is never touched: a run killed by any
signal (or a hung guard) cannot leave a mutant behind. Verdicts:

- GUARDED   the guard failed on the mutant (good)
- SURVIVED  the guard still passed: the invariant is unprotected (exit status 1)
- NO-OP     ``old`` not found or not unique: the spec is stale (exit status 1)
- NO-TEST   the pytest selection collected nothing: the guard name is wrong (exit status 1)
- INVALID   the mutant does not import/parse: proves nothing about the tests (exit status 1)
- TIMEOUT   the guard ran past TIMEOUT_S: proves nothing either way (exit status 1)

Run from anywhere:

    python scripts/mutate.py                 # every spec in tests/mutants/
    python scripts/mutate.py tests/mutants/signin.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = os.environ.get("PY") or sys.executable
TIMEOUT_S = 600
INVALID_MARKERS = ("SyntaxError", "IndentationError", "ImportError while", "errors during collection",
                   "ReferenceError", "is not defined")
# Not part of the source under test: environments, VCS data, sibling worktrees, build output, caches.
SKIP = shutil.ignore_patterns(".venv", ".git", ".wt", "node_modules", "__pycache__", "build", "dist",
                              ".pytest_cache", ".playwright-mcp")


def _run(entry: dict) -> tuple[str, str]:
    original = (ROOT / entry["file"]).read_text()
    if original.count(entry["old"]) != 1:
        return "NO-OP", f"'old' occurs {original.count(entry['old'])}x in {entry['file']}"
    if "cmd" in entry:
        cmd = entry["cmd"]
    else:
        cmd = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:warnings",
               *entry["test"]]
    with tempfile.TemporaryDirectory(prefix="mutate-") as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(ROOT, tree, ignore=SKIP, symlinks=True)
        (tree / entry["file"]).write_text(original.replace(entry["old"], entry["new"], 1))
        # Python trusts a cached .pyc whose recorded source mtime and size match, so mutant bytecode
        # must never land where another run could find it: it compiles into this throwaway dir.
        env = {**os.environ, "PYTHONPYCACHEPREFIX": str(Path(tmp) / "pycache")}
        env.pop("PYTHONPATH", None)   # the copy's own sources, not the real tree's, must load
        try:
            res = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, timeout=TIMEOUT_S,
                                 env=env)
        except subprocess.TimeoutExpired:
            return "TIMEOUT", f"guard ran past {TIMEOUT_S}s"
    out = res.stdout + res.stderr
    if res.returncode == 5 or "no tests ran" in out:
        return "NO-TEST", "the selection matched nothing"
    if res.returncode != 0 and any(m in out for m in INVALID_MARKERS):
        return "INVALID", "mutant does not load"
    if res.returncode != 0:
        return "GUARDED", ""
    return "SURVIVED", "guard stayed green"


def main(argv: list[str]) -> int:
    specs = [Path(a) for a in argv] or sorted((ROOT / "tests" / "mutants").glob("*.json"))
    bad = 0
    total = 0
    for spec in specs:
        for entry in json.loads(Path(spec).read_text()):
            total += 1
            verdict, why = _run(entry)
            bad += verdict != "GUARDED"
            print(f"{verdict:<9} {spec.stem}: {entry['label']}" + (f"  ({why})" if why else ""),
                  flush=True)
    print(f"\n{total - bad}/{total} mutants guarded")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
