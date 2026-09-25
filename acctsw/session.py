"""Is a supervised tool session running right now? — tracked by PID + process start-time.

The menubar app needs to know which seat a live ``cx``/``cl`` supervisor is using. A plain PID is
not enough: after a crash or force-quit the stale file can outlive the process, and the OS may later
recycle that PID for an unrelated program. Recording ``ps -o lstart=`` alongside the PID gives the
process a stable identity, so ``active_session`` rejects both dead and recycled PIDs.

The heartbeat is JSON because it also carries the active seat and launch time. Writes go through
``util.write_json`` for the same atomic temp+``os.replace`` behavior and strict ``0o600`` mode used
for the engine's other private state. Functions take ``data_dir`` so the app and launcher share one
location and tests remain isolated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .procenv import proc_start, same_proc_start
from .util import iso, now, write_json


def _session_file(data_dir: Path, tool: str) -> Path:
    return Path(data_dir) / f"session-{tool}.json"


def _process_file(data_dir: Path, tool: str, pid: int) -> Path:
    return Path(data_dir) / f"session-{tool}-{pid}.json"


def _session_files(data_dir: Path, tool: str) -> list[Path]:
    return [_session_file(data_dir, tool), *Path(data_dir).glob(f"session-{tool}-*.json")]


def _alive(pid: int) -> bool:
    """Bare liveness (used only as a fallback when ``ps`` is unavailable)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


# Shared with ``appalive`` (one implementation, so the two heartbeats can't drift): reads the
# start-time in the C locale and compares tolerantly. Aliased as a module global so tests can
# monkeypatch the ``ps`` call on this module.
_proc_start = proc_start


_START_CACHE: dict[int, str] = {}


def mark_session(data_dir: Path, tool: str, email: str) -> None:
    """Record this supervisor and its active seat at launch and after every successful hop.

    A process's ``ps`` start-time never changes, so cache it instead of spawning ``ps`` for every
    relaunch. An empty value means ``ps`` was unavailable and readers fall back to PID liveness.
    """
    pid = os.getpid()
    process_start = _START_CACHE.get(pid)
    if process_start is None:
        process_start = _proc_start(pid) or ""
        _START_CACHE[pid] = process_start
    data = {
        "email": email,
        "pid": pid,
        "process_start": process_start,
        "started_at": iso(now()),
    }
    # Keep each terminal's record independently. Retain the legacy mirror for older apps,
    # preserving its existing owner before another terminal replaces it during an upgrade.
    legacy = _session_file(data_dir, tool)
    try:
        previous = json.loads(legacy.read_text(encoding="utf-8"))
        previous_pid = int(previous["pid"])
        if previous_pid != pid and _read_session(legacy) is not None:
            # Never overwrite another supervisor's newer record. A hard link snapshots the
            # legacy inode atomically and fails if that process already owns a registry entry.
            os.link(legacy, _process_file(data_dir, tool, previous_pid))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    write_json(_process_file(data_dir, tool, pid), data, mode=0o600)
    write_json(legacy, data, mode=0o600)


def clear_session(data_dir: Path, tool: str) -> None:
    """Remove the session heartbeat. Best-effort; any stale file is verified when read."""
    try:
        _process_file(data_dir, tool, os.getpid()).unlink()
    except OSError:
        pass
    try:
        legacy = _session_file(data_dir, tool)
        data = json.loads(legacy.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("pid") == os.getpid():
            legacy.unlink()
    except (OSError, ValueError, TypeError):
        pass


def session_mtime_ns(data_dir: Path, tool: str) -> int:
    """Signature of the heartbeat files' mtimes, or 0 when absent. A change-signal for the menubar:
    a session starting or ending writes/removes this file WITHOUT touching state.json's ``rev``
    (a launch on the already-active seat saves no state), so ``rev`` alone cannot see it."""
    signature = 0
    for path in _session_files(data_dir, tool):
        try:
            signature += path.stat().st_mtime_ns
        except OSError:
            pass
    return signature


def active_session(data_dir: Path, tool: str, *, email: str | None = None) -> dict | None:
    """Return the live session's public fields, rejecting dead or recycled recorded PIDs."""
    sessions = active_sessions(data_dir, tool)
    if email is not None:
        sessions = [s for s in sessions if s["email"] == email]
    return max(sessions, key=lambda s: s["started_at"]) if sessions else None


def active_sessions(data_dir: Path, tool: str) -> list[dict]:
    """Every live terminal, deduplicating the backwards-compatible legacy mirror."""
    sessions = {}
    for path in _session_files(data_dir, tool):
        data = _read_session(path)
        if data is not None:
            previous = sessions.get(data["pid"])
            if previous is None or data["started_at"] > previous["started_at"]:
                sessions[data["pid"]] = data
    return list(sessions.values())


def _read_session(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        email = data["email"]
        pid = int(data["pid"])
        started_at = data["started_at"]
        if not isinstance(email, str) or not isinstance(started_at, str):
            return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if not _alive(pid):
        return None
    stored_start = data.get("process_start")
    start = _proc_start(pid)
    if start is None:
        pass  # ps unavailable → bare liveness already confirmed the process exists
    elif not start:
        return None  # process exited between the cheap liveness check and ps
    elif stored_start and not same_proc_start(stored_start, start):
        # Tolerant compare: a heartbeat written by a pre-fix build carries the writer's locale
        # formatting until that supervisor exits, so raw inequality is not proof of PID reuse.
        return None
    return {"email": email, "pid": pid, "started_at": started_at}
