"""Subprocess environment hygiene — nothing to do with any single feature.

Every child process we spawn (the supervised codex/claude launchers, the Terminal login flow) must
run with a CLEAN interpreter environment. py2app injects PYTHONHOME/PYTHONPATH (and friends) pointing
at the FROZEN app's stripped, zipped stdlib; if those leak into a DIFFERENT interpreter (a system
python, a tool's own venv) it resolves stdlib against the app bundle and dies (e.g.
`ModuleNotFoundError: No module named 'uuid'`). Strip them so each child uses its own stdlib.

(These helpers formerly lived in the now-removed `headroom` module; they are launcher/terminal
infrastructure, not compression, so they live here.)
"""
from __future__ import annotations

import os

# Interpreter-redirect vars py2app sets on the frozen app; must not leak into a child interpreter.
_PY_ENV_STRIP = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")

# Belt-and-suspenders: opt out of any third-party telemetry a spawned tool might honor. Local-only.
HARDENING_ENV = {"DO_NOT_TRACK": "1"}


def harden_env(env: dict | None = None) -> dict:
    """Return env with the interpreter-redirect vars stripped (so a child python uses its own stdlib,
    not the frozen app's) + local-only hardening flags applied. Does not mutate the input."""
    e = dict(os.environ if env is None else env)
    for k in _PY_ENV_STRIP:
        e.pop(k, None)
    e.update(HARDENING_ENV)
    return e
