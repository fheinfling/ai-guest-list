"""The engine must load on the oldest interpreter it ships with, not just the one running the tests.

The app bundles Python 3.11 (pyproject requires >=3.11). Newer grammar (PEP 701 f-strings with
backslashes or nested quotes, `type` statements, ...) imports fine under the dev interpreter and is a
SyntaxError in the shipped app, which silently disables whatever module holds it. Compile every
engine and app module with the oldest interpreter available here.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIN = (3, 11)


BUNDLE = "/Applications/AI Guest List.app/Contents/MacOS/python"


def _env_for(exe):
    env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    # The bundle's python finds its stdlib only through PYTHONHOME (see install.py's wrapper).
    if exe.endswith(".app/Contents/MacOS/python"):
        env["PYTHONHOME"] = str(Path(exe).parent.parent / "Resources")
    return env


def _candidates():
    env = os.environ.get("ACCTSW_MIN_PYTHON")
    if env:
        yield env
    yield BUNDLE
    for name in ("python3.11",):
        found = shutil.which(name)
        if found:
            yield found


def _min_python():
    for exe in _candidates():
        if not os.access(exe, os.X_OK):
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys; print(sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=30,
                                 env=_env_for(exe))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip() == str(MIN):
            return exe
    return None


def test_engine_compiles_on_the_shipped_interpreter():
    exe = _min_python()
    if exe is None:
        if sys.version_info[:2] == MIN:
            exe = sys.executable
        else:
            pytest.skip(f"no Python {MIN[0]}.{MIN[1]} found (set ACCTSW_MIN_PYTHON)")
    files = sorted(str(p) for d in ("acctsw", "app") for p in (ROOT / d).rglob("*.py"))
    # compile() only: no imports, no bytecode written into the tree.
    code = ("import sys\nbad = []\nfor f in sys.argv[1:]:\n"
            "    try:\n        compile(open(f, encoding='utf-8').read(), f, 'exec')\n"
            "    except SyntaxError as e:\n        bad.append(f'{f}:{e.lineno}: {e.msg}')\n"
            "print('\\n'.join(bad)); sys.exit(1 if bad else 0)")
    res = subprocess.run([exe, "-c", code, *files], capture_output=True, text=True,
                         timeout=120, env=_env_for(exe))
    assert res.returncode == 0, f"does not compile on {MIN[0]}.{MIN[1]}:\n{res.stdout}{res.stderr}"
