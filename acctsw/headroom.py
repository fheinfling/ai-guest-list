"""Legacy Headroom cleanup — a one-time, idempotent migration.

Earlier versions offered a "save credit" toggle that routed plain `codex`/`claude` (and the GUI)
through a local Headroom compression proxy. Measuring it on real workloads (see
`docs/SECURITY-headroom.md` and the retire-Headroom plan) showed the compression was ~1-3%
cache-adjusted on Claude and a rare truncation guardrail on Codex — not worth a wire-path ML proxy
that kept turning itself off. The feature was removed.

This module remains ONLY to clean up after it on machines that used it. `cleanup_legacy` strips any
leftover provider routing from `~/.codex/config.toml` and `~/.claude/settings.json` (restoring the
user's exact pre-routing config from the snapshot when present, else a surgical unroute), stops an
orphaned proxy by its PID file, and deletes the managed venv + bookkeeping. It is safe to call on
every app launch / `cx` / `cl` run; `legacy_present` is the cheap gate that keeps it off the hot path
once there's nothing left to clean.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import paths as P

PROXY_PORT = 8787                                  # the port the old proxy bound; only used for markers
_PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
# Config-syntax strings our old routing wrote — used to detect a still-injected (dirty) config.
INJECT_MARKERS = ('model_provider = "headroom"', _PROXY_URL)


# --- detecting/stripping the old provider routing --------------------------------------------------

def _config_dir(tool: str) -> Path:
    return P.CODEX_HOME if tool == "codex" else P.CLAUDE_CONFIG_DIR


def _touched(tool: str) -> list[Path]:
    d = _config_dir(tool)
    if tool == "codex":
        return [d / "config.toml", d / "AGENTS.md"]
    return [d / "CLAUDE.md", d / "settings.json", d / "settings.local.json", d / ".mcp.json"]


def _is_injected(tool: str) -> bool:
    for cfg in _touched(tool):
        try:
            text = cfg.read_text(errors="ignore")
        except OSError:
            continue
        if any(m in text for m in INJECT_MARKERS):
            return True
    return False


def _any_injected() -> bool:
    return any(_is_injected(t) for t in ("codex", "claude"))


import re

_CODEX_MARK_START = "# --- acctsw headroom routing ---"
_CODEX_MARK_END = "# --- end acctsw headroom routing ---"
_CODEX_TOP_KEYS = (
    re.compile(r'(?m)^[ \t]*model_provider[ \t]*=.*\r?\n'),
    re.compile(r'(?m)^[ \t]*openai_base_url[ \t]*=.*\r?\n'),
)


def _strip_codex_routing(content: str) -> str:
    while _CODEX_MARK_START in content and _CODEX_MARK_END in content:
        s = content.index(_CODEX_MARK_START)
        e = content.index(_CODEX_MARK_END, s) + len(_CODEX_MARK_END)
        content = content[:s].rstrip("\n") + ("\n" + content[e:].lstrip("\n"))
    for pat in _CODEX_TOP_KEYS:
        content = pat.sub("", content)
    return content.lstrip("\n")


def _unroute_codex() -> None:
    from .util import atomic_write_text
    path = P.CODEX_HOME / "config.toml"
    if not path.exists():
        return
    try:
        stripped = _strip_codex_routing(path.read_text())
    except OSError:
        return
    atomic_write_text(path, stripped if stripped.strip() else "")


def _unroute_claude() -> None:
    from .util import atomic_write_text
    path = P.CLAUDE_CONFIG_DIR / "settings.json"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    env = payload.get("env")
    if not (isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == _PROXY_URL):
        return                                          # not our routing → leave it alone
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ENABLE_TOOL_SEARCH", None)
    if env:
        payload["env"] = env
    else:
        payload.pop("env", None)
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _unroute_all() -> None:
    _unroute_codex()
    _unroute_claude()


# --- the pre-routing config snapshot the old enable path saved -------------------------------------

def _global_backup(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-global-backup"


def has_backup(store: Path | None = None) -> bool:
    return (_global_backup(store) / "manifest.json").exists()


def restore_global(store: Path | None = None) -> tuple[bool, list[str]]:
    """Restore every snapshotted path to its ORIGINAL state (bytes/mode/symlink/absent). Returns
    (ok, failures); the backup is deleted only if every path restored cleanly."""
    from .util import atomic_write_bytes
    bdir = _global_backup(store)
    mf = bdir / "manifest.json"
    if not mf.exists():
        return True, []
    try:
        manifest = json.loads(mf.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"manifest unreadable: {e}"]
    failures: list[str] = []
    for i, path_s in manifest.items():
        p = Path(path_s)
        try:
            entry = json.loads((bdir / f"{i}.json").read_text())
            if p.is_symlink() or p.exists():
                p.unlink()
            if entry["kind"] == "symlink":
                p.symlink_to(entry["target"])
            elif entry["kind"] == "file":
                atomic_write_bytes(p, base64.b64decode(entry["b64"]), mode=entry.get("mode", 0o600))
            # kind == "absent" → leave it removed
        except OSError as e:
            failures.append(f"{p}: {e}")
    if not failures:
        shutil.rmtree(bdir, ignore_errors=True)
    return (not failures), failures


# --- stopping an orphaned proxy by its PID file ----------------------------------------------------

def _proxy_pidfile(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-proxy.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_is_proxy(pid: int) -> bool:
    """Identity guard against PID REUSE: confirm `pid` is actually a Headroom proxy before signalling
    it — the OS can recycle a dead proxy's PID for an unrelated process, and we must never kill a
    bystander. ps unavailable / no match → False (we'd rather leak than kill the wrong process)."""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "headroom" in out and "proxy" in out


def _proxy_pid(store: Path | None) -> int:
    try:
        return int(_proxy_pidfile(store).read_text().strip())
    except (OSError, ValueError):
        return 0


def stop_proxy(store: Path | None = None, *, kill=None, sleep=None) -> None:
    """Stop the old proxy (by PID file) and clear the file. Best-effort; safe if not running. Kills
    ONLY if the PID is alive AND is really a Headroom proxy (never a recycled/unrelated PID)."""
    import signal
    import time
    _kill = kill or os.kill
    _sleep = sleep or time.sleep
    pid = _proxy_pid(store)
    if pid > 0 and _pid_alive(pid) and _pid_is_proxy(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                _kill(pid, sig)
            except OSError:
                break
            _sleep(0.3)
            if not _pid_alive(pid):
                break
    try:
        _proxy_pidfile(store).unlink()
    except OSError:
        pass


# --- the managed venv + bookkeeping the old feature created ----------------------------------------

def hr_venv_dir(store: Path | None = None) -> Path:
    return (store or P.DATA_DIR) / "hr-venv"


def _leftover_files(store: Path | None) -> list[Path]:
    d = store or P.DATA_DIR
    return [
        d / "headroom-proxy.pid", d / "headroom-proxy.log", d / "headroom.log",
        d / "rtk.sha256", d / "headroom-baseline-seeded", d / ".headroom-oplock",
    ]


# --- public migration API --------------------------------------------------------------------------

def _data_dir(ctx_or_store) -> Path:
    """Accept a Context (``.data_dir``) or a plain store Path."""
    return getattr(ctx_or_store, "data_dir", ctx_or_store) or P.DATA_DIR


def legacy_present(ctx_or_store=None) -> bool:
    """Cheap, subprocess-free check: is there any leftover Headroom state to clean? Lets the launch /
    cx / cl hot path skip cleanup entirely once nothing remains (the common case going forward)."""
    store = _data_dir(ctx_or_store)
    if has_backup(store) or _any_injected():
        return True
    if _proxy_pid(store) > 0 or hr_venv_dir(store).exists():
        return True
    return any(p.exists() for p in _leftover_files(store))


def cleanup_legacy(ctx) -> tuple[bool, str]:
    """One-time, idempotent teardown of the retired Headroom feature. Strips any leftover routing
    (restoring the exact pre-routing config from the snapshot when present), stops an orphaned proxy,
    deletes the managed venv + bookkeeping, and clears the old `headroom`/`savings_level` settings.
    ``ctx`` is duck-typed: ``data_dir``, ``locked()``, ``load_state()``. Returns (did_work, msg)."""
    store = _data_dir(ctx)
    if not legacy_present(store):
        return False, "clean"
    # 1. undo routing: exact restore from snapshot beats a surgical strip (routing REPLACED user keys).
    if has_backup(store):
        ok, failures = restore_global(store)
        if not ok:
            _log(store, "restore failed", "\n".join(failures))
    if _any_injected():
        _unroute_all()
    # 2. stop any orphaned proxy, then delete the managed venv + all bookkeeping.
    stop_proxy(store)
    shutil.rmtree(_global_backup(store), ignore_errors=True)
    shutil.rmtree(hr_venv_dir(store), ignore_errors=True)
    for p in _leftover_files(store):
        try:
            p.unlink()
        except OSError:
            pass
    # 3. clear the old settings so the (removed) toggle can't linger as truthy metadata.
    try:
        with ctx.locked():
            s = ctx.load_state()
            changed = False
            for k in ("headroom", "savings_level", "headroom_event"):
                if k in s.settings():
                    s.settings().pop(k, None); changed = True
                if k in s.data:
                    s.data.pop(k, None); changed = True
            if changed:
                s.save()
    except Exception:
        pass
    return True, "removed legacy Headroom routing, proxy, and files"


def _log(store: Path | None, label: str, text: str) -> None:
    from .util import now, iso
    try:
        p = (store or P.DATA_DIR) / "headroom-cleanup.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"\n[{iso(now())}] {label}\n{text}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass
