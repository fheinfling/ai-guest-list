"""Is the menubar app running right now? — tracked via a PID heartbeat file.

The supervised launcher (cx/cl) only auto-switches seats while the app is open. When the app
is CLOSED, terminal ``codex``/``claude`` must behave like the stock tool (no supervision, no
seat-hopping) — the app is the master switch for that behavior.

The app writes its PID here on launch (and refreshes it each usage poll) and removes it on
quit. A stale file (dead PID) reads as "closed", so a crash/force-quit safely degrades to
stock behavior rather than leaving auto-switch silently on.

Functions take the engine's ``data_dir`` (ctx.data_dir) so the app and the launcher agree on
one location and tests stay isolated to their temp dir.
"""
from __future__ import annotations

import os
from pathlib import Path


def _pidfile(data_dir: Path) -> Path:
    return Path(data_dir) / "app.pid"


def _alive(pid: int) -> bool:
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


def mark_alive(data_dir: Path) -> None:
    """Record this process as the running app (called by the menubar app on launch + each poll).

    Writes atomically (temp file + os.replace) so a concurrent ``app_running`` read during the
    periodic refresh never sees a truncated/empty pidfile (which would make cx/cl wrongly run stock).
    """
    f = _pidfile(data_dir)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text(str(os.getpid()))
    os.replace(tmp, f)


def mark_dead(data_dir: Path) -> None:
    """Remove the heartbeat (called on quit). Best-effort; a stale file is harmless (PID is checked)."""
    try:
        _pidfile(data_dir).unlink()
    except OSError:
        pass


def app_running(data_dir: Path) -> bool:
    """True iff the menubar app is alive right now (heartbeat present AND its PID is live)."""
    try:
        pid = int(_pidfile(data_dir).read_text().strip())
    except (OSError, ValueError):
        return False
    return _alive(pid)
