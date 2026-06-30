"""Is the menubar app running right now? — tracked via a PID + start-time heartbeat file.

The supervised launcher (cx/cl) only auto-switches seats while the app is open. When the app
is CLOSED, terminal ``codex``/``claude`` must behave like the stock tool (no supervision, no
seat-hopping) — the app is the master switch for that behavior.

The app writes its PID **and process start-time** here on launch (refreshed each usage poll) and
removes it on quit. ``app_running`` checks both: a stale file whose PID is dead reads as "closed",
and — crucially — a stale PID that the OS has since RECYCLED for an unrelated process reads as closed
too, because the live process's start-time won't match the one we recorded. So a crash/force-quit
safely degrades to stock behavior instead of a recycled PID masquerading as the app.

Functions take the engine's ``data_dir`` (ctx.data_dir) so the app and the launcher agree on one
location and tests stay isolated to their temp dir.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _pidfile(data_dir: Path) -> Path:
    return Path(data_dir) / "app.pid"


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
    """The process's absolute start-time string (a stable per-process identity that survives PID
    reuse), via ``ps``. Returns "" if no such process, or None if ``ps`` itself couldn't run."""
    if pid <= 0:
        return ""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None  # ps unavailable → caller falls back to bare liveness
    return r.stdout.strip()  # empty when the PID is not running


_START_CACHE: dict[int, str] = {}


def mark_alive(data_dir: Path) -> None:
    """Record this process as the running app (called by the menubar app on launch + each poll).

    Stores ``PID\\nstart-time`` and writes atomically (temp + os.replace) so a concurrent
    ``app_running`` read during the periodic refresh never sees a truncated/empty file (which would
    make cx/cl wrongly run stock). A process's start-time is invariant for its lifetime, so it's
    computed once and cached — the per-poll refresh never re-spawns ``ps``."""
    pid = os.getpid()
    start = _START_CACHE.get(pid)
    if start is None:
        start = _proc_start(pid) or ""   # "" = ps unavailable → legacy (PID-only) mode
        _START_CACHE[pid] = start
    body = f"{pid}\n{start}" if start else str(pid)
    f = _pidfile(data_dir)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{pid}.tmp")
    tmp.write_text(body)
    os.replace(tmp, f)


def mark_dead(data_dir: Path) -> None:
    """Remove the heartbeat (called on quit). Best-effort; a stale file is harmless (it's verified)."""
    try:
        _pidfile(data_dir).unlink()
    except OSError:
        pass


def app_running(data_dir: Path) -> bool:
    """True iff the menubar app is alive right now: the heartbeat's PID is live AND (when recorded)
    its start-time still matches — so a recycled PID does not read as the app."""
    try:
        lines = _pidfile(data_dir).read_text().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return False
    if not _alive(pid):             # cheap (no subprocess): dead/absent PID → app closed. Skips the
        return False                # `ps` spawn on the common cx/cl path (app closed → run stock).
    stored_start = lines[1].strip() if len(lines) > 1 else None
    start = _proc_start(pid)        # PID is live — now pay `ps` to defend against PID reuse.
    if start is None:               # ps unavailable → bare liveness already confirmed alive
        return True
    if not start:                   # raced: process exited between the checks → closed
        return False
    return stored_start is None or start == stored_start  # identity match when we recorded one
