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
import subprocess
from pathlib import Path

from .util import iso, now, write_json


def _session_file(data_dir: Path, tool: str) -> Path:
    return Path(data_dir) / f"session-{tool}.json"


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


def _proc_start(pid: int) -> str | None:
    """The process's stable absolute start-time, or None when ``ps`` itself is unavailable."""
    if pid <= 0:
        return ""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    return r.stdout.strip()


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
    write_json(
        _session_file(data_dir, tool),
        {
            "email": email,
            "pid": pid,
            "process_start": process_start,
            "started_at": iso(now()),
        },
        mode=0o600,
    )


def clear_session(data_dir: Path, tool: str) -> None:
    """Remove the session heartbeat. Best-effort; any stale file is verified when read."""
    try:
        _session_file(data_dir, tool).unlink()
    except OSError:
        pass


def active_session(data_dir: Path, tool: str) -> dict | None:
    """Return the live session's public fields, rejecting dead or recycled recorded PIDs."""
    try:
        data = json.loads(_session_file(data_dir, tool).read_text())
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
    elif stored_start and start != stored_start:
        return None
    return {"email": email, "pid": pid, "started_at": started_at}
