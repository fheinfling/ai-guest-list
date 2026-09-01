"""Per-account Codex credential snapshots.

Each Codex account keeps one real ``auth.json`` under
``~/.account-switcher/codex-homes/<id>/``.  These directories used to be complete ``CODEX_HOME``
overlays with symlinks into ``~/.codex``.  That became unsafe when Codex added top-level SQLite/WAL
families: a database could be created locally while its later ``-wal``/``-shm`` files were linked to
the canonical home, producing SQLite error 14 at startup.

The directories are now auth-only stores.  Codex itself always runs against its canonical home so
all configuration, session history, and runtime databases stay together.  Legacy non-auth entries
are deliberately left untouched and ignored; deleting a seat still removes its whole old directory.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import paths as P
from .util import atomic_write_text


def _safe(email: str) -> str:
    s = "".join(c if c.isalnum() or c in "._+-@" else "_" for c in email)
    # "/" is already replaced, so the result is a single path component — but "." / ".." would still
    # escape (home_dir("..") → the parent dir). Reject those explicitly so the sanitizer is never the
    # weak link if a malformed email ever reaches here.
    return s if s not in ("", ".", "..") else "seat"


def home_dir(email: str, root: Path | None = None) -> Path:
    return (root or P.CODEX_HOMES) / _safe(email)


def auth_path(email: str, root: Path | None = None) -> Path:
    return home_dir(email, root) / "auth.json"


def ensure_home(email: str, *, codex_home: Path | None = None, root: Path | None = None) -> Path:
    """Create the account's auth store without touching legacy non-auth contents.

    ``codex_home`` remains as a compatibility-only keyword for existing callers; it is intentionally
    ignored so no canonical Codex entry is ever linked into the seat store again.
    """
    home = home_dir(email, root)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home


def save(email: str, blob: str, *, codex_home: Path | None = None, root: Path | None = None) -> None:
    ensure_home(email, codex_home=codex_home, root=root)
    atomic_write_text(auth_path(email, root), blob, mode=0o600)


def load(email: str, *, root: Path | None = None) -> str | None:
    try:
        return auth_path(email, root).read_text()
    except FileNotFoundError:
        return None


def delete(email: str, *, root: Path | None = None) -> bool:
    home = home_dir(email, root)
    if home.is_symlink():
        home.unlink()  # never traverse a replaced/malformed seat root
        return True
    if not home.exists():
        return False
    # shutil.rmtree unlinks directory symlinks rather than following them, so this safely removes
    # auth-only stores as well as legacy homes that contain real runtime directories/databases.
    shutil.rmtree(home)
    return True
