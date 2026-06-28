"""Headroom integration — the "save credit" toggle.

When enabled, the supervised launcher routes the agent through Headroom
(https://github.com/headroomlabs-ai/headroom), which compresses what the agent reads → fewer
tokens → usage limits are hit more slowly. Headroom is a pure data-path wrapper: it never touches
credentials or the keychain.

It's installed into THIS app's venv by `acctsw install` (so it "just works" — no separate install),
and we locate it next to the running interpreter even when the venv's bin isn't on PATH.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths as P

WRAP_PREFIX = ("headroom", "wrap")
PACKAGE = "headroom-ai[proxy]"

# Files `headroom wrap <tool>` mutates (globally + persistently). We snapshot+restore these around
# a wrapped session so Headroom never permanently changes the user's setup (non-destructive).
def _touched(tool: str) -> list[Path]:
    if tool == "codex":
        return [P.CODEX_HOME / "config.toml", P.CODEX_HOME / "AGENTS.md"]
    return [P.CLAUDE_CONFIG_DIR / "CLAUDE.md", P.CLAUDE_CONFIG_DIR / "settings.json"]


@contextlib.contextmanager
def scoped(tool: str):
    """Snapshot the files Headroom injects into, then restore them EXACTLY on exit.

    During the wrapped session the injection is live (so the tool routes through the proxy); after,
    the user's config is byte-identical to before — stock codex/claude keep working.
    """
    snaps = {p: (p.read_bytes() if p.exists() else None) for p in _touched(tool)}
    try:
        yield
    finally:
        for p, data in snaps.items():
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                elif p.read_bytes() != data:
                    p.write_bytes(data)
            except OSError:
                pass


def headroom_path() -> str | None:
    """Absolute path to the `headroom` CLI: PATH first, then this venv's bin dir."""
    found = shutil.which("headroom")
    if found:
        return found
    cand = Path(sys.executable).parent / "headroom"
    return str(cand) if cand.exists() else None


def available() -> bool:
    return headroom_path() is not None


def venv_bin_dir() -> str:
    return str(Path(sys.executable).parent)


def ensure_installed() -> bool:
    """Best-effort: install headroom into the current venv if missing. Returns availability."""
    if available():
        return True
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", PACKAGE],
                       capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        pass
    return available()


def wrap(tool: str, tool_args: list, *, enabled: bool, is_available=None,
         exe: str = "headroom") -> list | None:
    """Return the Headroom command for ``tool`` (``headroom wrap codex -- <args>``), or None when
    save-credit is off / Headroom is missing (caller then runs the tool directly).

    Headroom uses a per-tool subcommand and launches the tool itself, so we pass the tool NAME
    (not the binary) and forward the tool's args after ``--``. ``exe`` is the resolved headroom path.
    """
    ok = available() if is_available is None else is_available
    if not (enabled and ok):
        return None
    cmd = [exe, "wrap", tool]
    if tool_args:
        cmd += ["--", *tool_args]
    return cmd
