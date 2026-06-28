"""Per-account Codex homes (isolation, spec §4).

Each Codex account gets its own ``CODEX_HOME`` directory under ~/.account-switcher/codex-homes/<id>/
containing that account's REAL ``auth.json`` plus symlinks to everything else in the user's real
``~/.codex`` (config.toml, sessions, plugins, sqlite, …). codex run with that ``CODEX_HOME`` reads
and refreshes the account's own auth.json in place — so using/rotating one account NEVER touches
another (the cross-invalidation that the shared-auth.json swap model suffered). Sessions/config are
shared via the symlinks, so cross-account `codex resume` still works.

This keeps the user's real ~/.codex as the shared source of truth (we never relocate it); only
auth.json is per-account. Fully reversible: delete the codex-homes dir.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import paths as P
from .util import atomic_write_text


def _safe(email: str) -> str:
    return "".join(c if c.isalnum() or c in "._+-@" else "_" for c in email) or "seat"


def home_dir(email: str, root: Path | None = None) -> Path:
    return (root or P.CODEX_HOMES) / _safe(email)


def auth_path(email: str, root: Path | None = None) -> Path:
    return home_dir(email, root) / "auth.json"


def ensure_home(email: str, *, codex_home: Path | None = None, root: Path | None = None) -> Path:
    """Create the account's home: real auth.json lives here; everything else symlinks to ~/.codex.

    Idempotent. Re-links any new shared entries that appeared in ~/.codex since last time.
    """
    real = codex_home or P.CODEX_HOME
    home = home_dir(email, root)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    if real.exists():
        for entry in real.iterdir():
            if entry.name == "auth.json":
                continue  # auth is per-account (a real file in the home)
            link = home / entry.name
            if link.is_symlink():
                if link.resolve() != entry.resolve():
                    link.unlink(); link.symlink_to(entry)
            elif not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
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
    if not home.exists():
        return False
    # unlink symlinks (don't follow into ~/.codex), remove the real auth.json, then the dir
    for entry in home.iterdir():
        try:
            entry.unlink()
        except OSError:
            pass
    try:
        home.rmdir()
    except OSError:
        pass
    return True
