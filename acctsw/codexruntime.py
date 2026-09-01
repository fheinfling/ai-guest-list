"""Cross-process coordination for supervised Codex children.

Codex now keeps several SQLite/WAL families directly in ``CODEX_HOME``.  AI Guest List therefore
leaves Codex on its canonical home and switches only ``auth.json``.  Because that credential file is
shared, a seat change is safe only while no supervised Codex child is running.

Every supervisor owns one flock-backed lease for its lifetime.  The lease is marked ``running`` only
while its child can read or refresh credentials; a stopped child that is classifying a limit or
waiting for another session does not block a hop.  The advisory lock makes stale cleanup automatic:
after a crash the OS releases it, and the next scan removes the orphaned file.
"""
from __future__ import annotations

import fcntl
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


_DIR = "codex-runners"
_GATE = ".gate"
_LEASE_SUFFIX = ".lease"


def _root(data_dir: Path) -> Path:
    root = Path(data_dir) / _DIR
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


@contextmanager
def gate(data_dir: Path) -> Iterator[Path]:
    """Serialize lease changes/scans with credential switches."""
    root = _root(data_dir)
    f = open(root / _GATE, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield root
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _read_phase(f: TextIO) -> str:
    try:
        f.seek(0)
        return (f.readline().strip().split("\t", 1)[0] or "stopped")
    except OSError:
        return "stopped"


def running_count_locked(root: Path) -> int:
    """Count live ``running`` leases. Caller must hold :func:`gate`."""
    running = 0
    for path in root.glob(f"*{_LEASE_SUFFIX}"):
        try:
            other = open(path, "r+")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if _read_phase(other) == "running":
                    running += 1
                continue
            # Nobody owns the lock: the supervisor crashed or exited before unlinking.
            try:
                path.unlink()
            except OSError:
                pass
            finally:
                fcntl.flock(other, fcntl.LOCK_UN)
        finally:
            other.close()
    return running


def running_count(data_dir: Path) -> int:
    with gate(data_dir) as root:
        return running_count_locked(root)


class SupervisorLease:
    """A live supervisor lease whose phase can follow each spawned child."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path: Path | None = None
        self._file: TextIO | None = None
        with gate(self.data_dir) as root:
            path = root / f"{os.getpid()}-{uuid.uuid4().hex}{_LEASE_SUFFIX}"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            f = os.fdopen(fd, "r+")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path, self._file = path, f
            self._write_locked("stopped", "")

    def _write_locked(self, phase: str, email: str) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"{phase}\t{email}\n")
        self._file.flush()

    def mark_running(self, email: str) -> None:
        with gate(self.data_dir):
            self._write_locked("running", email)

    def mark_stopped(self) -> None:
        with gate(self.data_dir):
            self._write_locked("stopped", "")

    def close(self) -> None:
        f, path = self._file, self.path
        if f is None:
            return
        try:
            with gate(self.data_dir):
                self._write_locked("stopped", "")
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
                try:
                    if path is not None:
                        path.unlink()
                except OSError:
                    pass
        finally:
            self._file = None
            self.path = None
            if not f.closed:
                f.close()

    def __enter__(self) -> "SupervisorLease":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
