#!/usr/bin/env python3
"""Diff-scoped mutation sweep: mutate only the lines changed vs the base branch.

For every ``acctsw/*.py`` line added or changed in ``git diff -U0 <base>...HEAD`` the AST nodes on
those lines are mutated one at a time (comparison/boolean/arithmetic flips, constants, ``not``,
``if`` conditions, dropped calls, ``return None``). Each mutant runs the test files mapped to its
source file in a per-worker throwaway copy of the tree, so the real tree is never written.

Verdicts: KILLED (tests failed), SURVIVED (tests passed), TIMEOUT, INVALID (does not compile).
Tests that already fail on the unmutated tree (sandbox limits, known flakes) are deselected.

    python scripts/mutate_diff.py                       # every changed acctsw file
    python scripts/mutate_diff.py --files launcher usage --max 40 --jobs 6
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import copy
import json
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = os.environ.get("PY") or sys.executable
KNOWN_BASELINE = Path("/tmp/claude-501/sandbox-baseline-failures.txt")

# acctsw/<key>.py -> extra test files on top of tests/test_<key>*.py. Adjust freely.
TEST_MAP: dict[str, list[str]] = {
    "launcher": ["tests/test_launcher_inplace.py", "tests/test_inplace_integration.py",
                 "tests/test_launcher.py"],
    "seatswap": ["tests/test_seatswap.py", "tests/test_inplace_integration.py",
                 "tests/test_seat_env.py"],
    "procenv": ["tests/test_seat_env.py"],
    "usage": ["tests/test_signin.py", "tests/test_usage.py", "tests/test_claude_live_item.py",
              "tests/test_codex_token_expiry.py"],
    "accounts": ["tests/test_signin.py", "tests/test_usage.py", "tests/test_claude_live_item.py"],
    "keychain": ["tests/test_signin.py", "tests/test_usage.py", "tests/test_claude_live_item.py"],
    "selection": ["tests/test_selection.py", "tests/test_seatkind.py"],
    "seatkind": ["tests/test_seatkind.py"],
    "seatproxy": ["tests/test_seatproxy.py"],
    "switch": ["tests/test_switch_safety.py", "tests/test_accounts_switch.py"],
}
SKIP = shutil.ignore_patterns(".venv", ".git", ".wt", "node_modules", "__pycache__", "build", "dist",
                              ".pytest_cache", ".playwright-mcp")
LOG_NAMES = {"log", "logger", "logging", "_log", "print"}
FLIP_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
            ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot,
            ast.IsNot: ast.Is}


def changed_lines(base: str, only: list[str] | None) -> dict[str, set[int]]:
    out = subprocess.run(["git", "diff", "-U0", f"{base}...HEAD", "--", "acctsw/*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    res: dict[str, set[int]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur and (m := re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)):
            start, n = int(m[1]), int(m[2]) if m[2] is not None else 1
            if n:
                res.setdefault(cur, set()).update(range(start, start + n))
    if only:
        keep = {f"acctsw/{o.removesuffix('.py').removeprefix('acctsw/')}.py" for o in only}
        res = {k: v for k, v in res.items() if k in keep}
    return res


def tests_for(rel: str) -> list[str]:
    key = Path(rel).stem
    found = {str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob(f"test_{key}*.py")}
    found.update(TEST_MAP.get(key, []))
    if not found:   # no mapped test: any test file that mentions the module
        pat = re.compile(rf"\b{re.escape(key)}\b")
        found = {str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_*.py")
                 if pat.search(p.read_text(errors="ignore")) and "acctsw" in p.read_text(errors="ignore")}
    return sorted(t for t in found if (ROOT / t).exists())


class Mutant:
    __slots__ = ("file", "line", "op", "old", "new", "start", "end", "text")

    def __init__(self, file, line, op, old, new, start, end, text):
        self.file, self.line, self.op, self.old, self.new = file, line, op, old, new
        self.start, self.end, self.text = start, end, text

    def label(self):
        return f"{self.file}:{self.line}  {self.op}  {self.old} → {self.new}"


def _short(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def generate(rel: str, lines: set[int]) -> list[Mutant]:
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    # byte offset of each line start, since ast columns are utf-8 byte offsets
    data = src.encode()
    starts = [0]
    for ln in data.split(b"\n")[:-1]:
        starts.append(starts[-1] + len(ln) + 1)

    def span(n):
        return starts[n.lineno - 1] + n.col_offset, starts[n.end_lineno - 1] + n.end_col_offset

    skip: set[int] = set()   # ids of nodes never mutated (annotations, docstrings, logging)
    for node in ast.walk(tree):
        for f in ("annotation", "returns"):
            a = getattr(node, f, None)
            if isinstance(a, ast.AST):
                skip.update(id(x) for x in ast.walk(a))
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                skip.update(id(x) for x in ast.walk(b[0]))
        if isinstance(node, ast.Call):
            f = node.func
            root = f
            while isinstance(root, ast.Attribute):
                root = root.value
            if (isinstance(root, ast.Name) and root.id in LOG_NAMES) or (
                    isinstance(f, ast.Attribute) and f.attr in {"debug", "info", "warning", "error",
                                                                "exception", "critical"}):
                skip.update(id(x) for x in ast.walk(node))

    seen: set[tuple[int, int, str]] = set()
    out: list[Mutant] = []

    def add(node, op, new_text, new_desc=None):
        s, e = span(node)
        if (s, e, new_text) in seen:
            return
        seen.add((s, e, new_text))
        old = data[s:e].decode()
        out.append(Mutant(rel, node.lineno, op, _short(old), _short(new_desc or new_text), s, e,
                          new_text))

    def paren(n):
        return "(" + ast.unparse(n) + ")"

    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln is None or ln not in lines or id(node) in skip:
            continue
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in FLIP_CMP:
                    c = copy.deepcopy(node)
                    c.ops[i] = FLIP_CMP[type(op)]()
                    add(node, "cmp-flip", paren(c))
        elif isinstance(node, ast.BoolOp):
            c = copy.deepcopy(node)
            c.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            add(node, "bool-swap", paren(c))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add(node, "not-drop", paren(node.operand))
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                add(node, "bool-flip", str(not v))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                add(node, "num+1", repr(v + 1) if v >= 0 else f"({v + 1!r})")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            c = copy.deepcopy(node)
            c.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
            add(node, "add-sub", paren(c))
        elif isinstance(node, ast.Return) and node.value is not None \
                and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            add(node.value, "return-None", "None")
        elif isinstance(node, ast.If):
            for val in ("True", "False"):
                if ast.unparse(node.test) != val:
                    add(node.test, f"if->{val}", val)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            add(node, "drop-call", "pass")
    out.sort(key=lambda m: (m.line, m.start, m.op))
    return out


# --- execution ---------------------------------------------------------------------------------

def run_cmd(cmd, cwd, env, timeout):
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        return None, ""


def pytest_cmd(tests, deselect):
    cmd = [PY, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "-p", "no:warnings", "--tb=no",
           *tests]
    for d in deselect:
        cmd += ["--deselect", d]
    return cmd


def make_env(pyc: Path):
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(pyc)}
    env.pop("PYTHONPATH", None)
    return env


def baseline(tests: list[str], tmp: Path, timeout: int) -> list[str]:
    """Node ids failing on the unmutated tree (plus the known sandbox list for those files)."""
    tree = tmp / "base-tree"
    shutil.copytree(ROOT, tree, ignore=SKIP, symlinks=True)
    cmd = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:warnings", "--tb=no",
           "-rfE", *tests]
    rc, out = run_cmd(cmd, tree, make_env(tmp / "base-pyc"), timeout * 5)
    shutil.rmtree(tree, ignore_errors=True)
    bad = {m[1] for m in re.finditer(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", out or "", re.M)}
    if KNOWN_BASELINE.exists():
        for m in re.finditer(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", KNOWN_BASELINE.read_text(), re.M):
            if m[1].split("::")[0] in tests:
                bad.add(m[1])
    if rc is None:
        print(f"  baseline timed out for {tests}", file=sys.stderr)
    return sorted(bad)


class Worker:
    def __init__(self, tmp: Path, idx: int):
        self.dir = tmp / f"w{idx}"
        self.tree = self.dir / "tree"
        self.pyc = self.dir / "pyc"
        shutil.copytree(ROOT, self.tree, ignore=SKIP, symlinks=True)

    def run(self, m: Mutant, original: bytes, tests, deselect, timeout) -> str:
        path = self.tree / m.file
        mutated = original[:m.start] + m.text.encode() + original[m.end:]
        try:
            compile(mutated, m.file, "exec")
        except (SyntaxError, ValueError):
            return "INVALID"
        shutil.rmtree(self.pyc, ignore_errors=True)
        path.write_bytes(mutated)
        try:
            rc, out = run_cmd(pytest_cmd(tests, deselect), self.tree, make_env(self.pyc), timeout)
        finally:
            path.write_bytes(original)
        if rc is None:
            return "TIMEOUT"
        if rc == 0:
            return "SURVIVED"
        if rc == 5:
            return "SURVIVED"   # nothing selected: nothing guards it
        return "KILLED"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="main")
    ap.add_argument("--files", nargs="*", help="acctsw module names, e.g. launcher usage")
    ap.add_argument("--max", type=int, default=0, help="sample at most N mutants per file")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="/tmp/claude-501/mutate-diff-results.json")
    ap.add_argument("--survivors", default="/tmp/claude-501/mutate-diff-survivors.txt")
    a = ap.parse_args()

    t0 = time.time()
    changed = changed_lines(a.base, a.files)
    plan = {}
    rng = random.Random(a.seed)
    for rel, lines in sorted(changed.items()):
        ms = generate(rel, lines)
        if a.max and len(ms) > a.max:
            ms = sorted(rng.sample(ms, a.max), key=lambda m: (m.line, m.start))
        plan[rel] = ms
        print(f"{rel}: {len(lines)} changed lines, {len(ms)} mutants, tests={tests_for(rel)}",
              flush=True)

    with tempfile.TemporaryDirectory(prefix="mutate-diff-") as t:
        tmp = Path(t)
        base_cache: dict[tuple, list[str]] = {}
        deselect: dict[str, list[str]] = {}
        for rel in plan:
            ts = tuple(tests_for(rel))
            if ts not in base_cache:
                print(f"baseline {list(ts)} ...", flush=True)
                base_cache[ts] = baseline(list(ts), tmp, a.timeout) if ts else []
                print(f"  {len(base_cache[ts])} base failures deselected", flush=True)
            deselect[rel] = base_cache[ts]

        total = sum(len(v) for v in plan.values())
        jobs = max(1, min(a.jobs, total or 1))
        print(f"{total} mutants, {jobs} workers", flush=True)
        slots: queue.Queue[Worker] = queue.Queue()
        with cf.ThreadPoolExecutor(jobs) as ex:
            for w in list(ex.map(lambda i: Worker(tmp, i), range(jobs))):
                slots.put(w)
            originals = {rel: (ROOT / rel).read_bytes() for rel in plan}

            def task(m: Mutant):
                tests = tests_for(m.file)
                if not tests:
                    return m, "SURVIVED"
                w = slots.get()
                try:
                    return m, w.run(m, originals[m.file], tests, deselect[m.file], a.timeout)
                finally:
                    slots.put(w)

            futs = [ex.submit(task, m) for ms in plan.values() for m in ms]
            results = []
            for i, f in enumerate(cf.as_completed(futs), 1):
                results.append(f.result())
                if i % 25 == 0 or i == total:
                    print(f"  {i}/{total} done, {time.time() - t0:.0f}s", flush=True)

    rows = [{"file": m.file, "line": m.line, "op": m.op, "old": m.old, "new": m.new, "verdict": v}
            for m, v in sorted(results, key=lambda r: (r[0].file, r[0].line, r[0].start))]
    Path(a.out).write_text(json.dumps({"seconds": round(time.time() - t0, 1), "results": rows},
                                      indent=1))
    surv = []
    print()
    for rel in plan:
        rs = [r for r in rows if r["file"] == rel]
        c = {v: sum(r["verdict"] == v for r in rs) for v in ("KILLED", "SURVIVED", "TIMEOUT", "INVALID")}
        valid = c["KILLED"] + c["SURVIVED"] + c["TIMEOUT"]
        score = f"{100 * c['KILLED'] / valid:.0f}%" if valid else "n/a"
        print(f"{rel:<22} killed {c['KILLED']}/{len(rs)}  score {score}  survived {c['SURVIVED']}  "
              f"timeout {c['TIMEOUT']}  invalid {c['INVALID']}")
        surv += [f"{r['file']}:{r['line']}  {r['op']}  {r['old']} → {r['new']}"
                 for r in rs if r["verdict"] in ("SURVIVED", "TIMEOUT") ]
    Path(a.survivors).write_text("\n".join(surv) + "\n")
    print(f"\n{len(surv)} survivors/timeouts -> {a.survivors}; total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
