"""Headroom integration — the "save credit" toggle.

When enabled, the supervised launcher routes the agent through Headroom
(https://github.com/headroomlabs-ai/headroom), which compresses what the agent reads → fewer
tokens → usage limits are hit more slowly. Headroom is a pure data-path wrapper: it never touches
credentials or the keychain.

It's installed into THIS app's venv by `acctsw install` (so it "just works" — no separate install),
and we locate it next to the running interpreter even when the venv's bin isn't on PATH.
"""
from __future__ import annotations

import base64
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths as P

WRAP_PREFIX = ("headroom", "wrap")
PINNED_VERSION = "0.27.0"                       # pin: audited build (see docs/SECURITY-headroom.md)
PACKAGE = f"headroom-ai[proxy]=={PINNED_VERSION}"

# Hardening env applied to every wrapped session: telemetry off, no third-party tracing/analytics,
# stay local-only. (All of headroom's cloud features are already opt-in via unset keys; this is
# belt-and-suspenders so nothing can phone home even if a default ever changes.)
HARDENING_ENV = {
    "HEADROOM_TELEMETRY": "off",     # anonymous usage telemetry (already off by default)
    "LITELLM_TELEMETRY": "False",    # disable litellm's own telemetry
    "DO_NOT_TRACK": "1",             # honored by litellm + others
}


def harden_env(env: dict | None = None) -> dict:
    """Return env with hardening flags applied (does not mutate the input)."""
    import os as _os
    e = dict(_os.environ if env is None else env)
    e.update(HARDENING_ENV)
    return e


def rtk_path() -> Path:
    return Path.home() / ".headroom" / "bin" / "rtk"


def verify_rtk(record_dir: Path | None) -> tuple[bool, str]:
    """TOFU checksum-pin the runtime-downloaded `rtk` binary.

    Records rtk's sha256 on first sight; on later runs, verifies it's unchanged. Returns
    (ok, message). ok=False means rtk changed unexpectedly (possible supply-chain tamper) → caller
    should refuse to run with Headroom until the user re-confirms.
    """
    from .util import sha256_text
    rtk = rtk_path()
    if not rtk.exists():
        return True, "rtk not present yet (downloaded on first wrap)"
    digest = sha256_text(rtk.read_bytes().hex())
    store = (record_dir or (Path.home() / ".account-switcher")) / "rtk.sha256"
    if store.exists():
        recorded = store.read_text().strip()
        if recorded != digest:
            return False, f"rtk checksum changed ({recorded[:12]}… → {digest[:12]}…)"
        return True, "rtk checksum verified"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(digest)
    return True, f"rtk checksum recorded ({digest[:12]}…)"

# Markers Headroom leaves in the tool config — used to detect a still-injected (dirty) config.
INJECT_MARKERS = ("Headroom", 'model_provider = "headroom"', "headroom:rtk-instructions")


def _config_dir(tool: str) -> Path:
    return P.CODEX_HOME if tool == "codex" else P.CLAUDE_CONFIG_DIR


def _touched(tool: str) -> list[Path]:
    """Files `headroom wrap <tool>` mutates. (Codex verified live; Claude best-effort superset.)"""
    d = _config_dir(tool)
    if tool == "codex":
        return [d / "config.toml", d / "AGENTS.md"]
    return [d / "CLAUDE.md", d / "settings.json", d / "settings.local.json", d / ".mcp.json"]


def _store_dir(store: Path | None) -> Path:
    return (store or (P.DATA_DIR / "headroom-backup"))


def _snapshot_file(store: Path | None, tool: str) -> Path:
    return _store_dir(store) / f"{tool}.snapshot.json"


def _is_injected(tool: str) -> bool:
    cfg = _touched(tool)[0]
    try:
        text = cfg.read_text()
    except OSError:
        return False
    return any(m in text for m in INJECT_MARKERS)


def _write_snapshot(store: Path | None, tool: str) -> None:
    snap = {}
    for p in _touched(tool):
        snap[str(p)] = (base64.b64encode(p.read_bytes()).decode() if p.exists() else None)
    sf = _snapshot_file(store, tool)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(snap))


def _restore_snapshot(store: Path | None, tool: str) -> bool:
    """Restore files from the on-disk snapshot and delete it. Returns True if one existed."""
    sf = _snapshot_file(store, tool)
    if not sf.exists():
        return False
    try:
        snap = json.loads(sf.read_text())
    except (OSError, json.JSONDecodeError):
        sf.unlink(missing_ok=True)
        return False
    for path_s, b64 in snap.items():
        p = Path(path_s)
        try:
            if b64 is None:
                if p.exists():
                    p.unlink()
            else:
                data = base64.b64decode(b64)
                if not p.exists() or p.read_bytes() != data:
                    p.write_bytes(data)
        except OSError:
            pass
    sf.unlink(missing_ok=True)
    return True


def recover_stale(store: Path | None = None) -> None:
    """Called at launcher start: if a prior wrapped session crashed without cleanup, its on-disk
    snapshot is still present — restore it so a SIGKILL can't leave the config permanently injected.
    """
    for tool in ("codex", "claude"):
        _restore_snapshot(store, tool)


@contextlib.contextmanager
def scoped(tool: str, store: Path | None = None):
    """Snapshot the files Headroom injects into (ON DISK), then restore them EXACTLY on exit.

    Crash-safe: the snapshot lives on disk, so ``recover_stale`` can undo an injection left by a
    SIGKILLed session on the next start. We never re-baseline a still-injected config — if a stale
    snapshot exists we restore it first, guaranteeing the baseline we capture is clean.
    """
    if _snapshot_file(store, tool).exists():
        _restore_snapshot(store, tool)  # prior crash → recover before capturing a clean baseline
    _write_snapshot(store, tool)
    try:
        yield
    finally:
        _restore_snapshot(store, tool)


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


def output_savings_pct() -> int | None:
    """Cumulative token-reduction % from `headroom output-savings` (None until it has data)."""
    import re
    exe = headroom_path()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "output-savings"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)\s*%", out.stdout or "")
        return int(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


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
