"""Per-account Codex homes (isolation, spec §4).

Each Codex account gets its own ``CODEX_HOME`` directory under ~/.account-switcher/codex-homes/<id>/
containing that account's REAL ``auth.json`` plus symlinks to everything else in the user's real
``~/.codex`` (config.toml, sessions, plugins, sqlite, …). codex run with that ``CODEX_HOME`` reads
and refreshes the account's own auth.json in place — so using/rotating one account NEVER touches
another (the cross-invalidation that the shared-auth.json swap model suffered). Sessions/config are
shared via the symlinks, so cross-account `codex resume` still works.

This keeps the user's real ~/.codex as the shared source of truth (we never relocate it); only
auth.json is per-account. Fully reversible: delete the codex-homes dir.

INVARIANT: a home holds exactly ONE real file, ``auth.json``. Everything else is shared state owned
by the real ~/.codex and appears here only as a symlink. Three rules keep it that way:

1. **SQLite sidecars are never linked.** Codex keeps databases at the top level of CODEX_HOME
   (logs_2.sqlite, goals_1.sqlite, memories_1.sqlite, queue_1.sqlite). When the database itself is a
   symlink SQLite resolves it and puts the ``-wal``/``-shm`` files next to the REAL file, so sidecar
   links buy nothing — but a MIXED family (a real database here, its sidecars linked into ~/.codex)
   makes SQLite fail every open with "unable to open database file" (error 14). That mix was
   reachable: a supervised child creates a database name ~/.codex doesn't have yet (a Codex upgrade
   renaming logs_1 → logs_2), stock codex later creates the canonical one WITH sidecars, and the
   next link pass pulls the sidecar names in. So sidecar names are never linked, and any sidecar
   link left by an older build is removed on sight (safe mid-session: the child holds the real path).
2. **Promotion.** With ``promote=True`` a real file a child created here is moved into ~/.codex
   (with its real sidecars) and the base name symlinked back — the shared state rejoins the shared
   home instead of drifting per seat.
3. **Parking.** If ~/.codex already has that name, the canonical one wins and the home's divergent
   copy is renamed ``<name>.orphaned-<YYYYmmddTHHMMSS>`` — never deleted, never linked, and ignored
   by every later pass. A real sidecar left behind without its database is parked as well, never
   moved: a stray -wal beside a canonical database would hand SQLite frames it never wrote.

Promotion MOVES files, so the launcher asks for it only when no supervised codex session is live;
otherwise the heal simply waits for the next launch.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from . import paths as P
from .util import atomic_write_text

# Files SQLite creates beside a database. Never symlinked (see rule 1 above).
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Marks a parked divergent copy. Such entries are inert: never linked, moved, parked or followed.
ORPHAN_MARK = ".orphaned-"


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


def _is_sidecar(name: str) -> bool:
    return name.endswith(SQLITE_SIDECARS)


def _relocate(src: Path, dst: Path) -> None:
    """Move ``src`` onto the (absent) ``dst``: a rename on one filesystem, copy+remove across two."""
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _park(entry: Path) -> None:
    """Rename a divergent copy aside as ``<name>.orphaned-<stamp>`` — user data is never deleted."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = entry.with_name(f"{entry.name}{ORPHAN_MARK}{stamp}")
    n = 2
    while dst.exists() or dst.is_symlink():  # two heals in the same second must not clobber
        dst = entry.with_name(f"{entry.name}{ORPHAN_MARK}{stamp}-{n}")
        n += 1
    _relocate(entry, dst)


def _real_sidecars(entry: Path) -> list[Path]:
    """The REAL (non-symlink) sidecars sitting beside ``entry`` — they travel with their database."""
    out = []
    for suffix in SQLITE_SIDECARS:
        side = entry.with_name(entry.name + suffix)
        if side.exists() and not side.is_symlink():
            out.append(side)
    return out


def _heal(home: Path, real: Path) -> None:
    """Give the home back its invariant: only auth.json is real, nothing dangles (rules 2 & 3)."""
    for entry in sorted(home.iterdir()):
        name = entry.name
        if name == "auth.json" or ORPHAN_MARK in name:
            continue                      # per-account token / inert parked copy
        if entry.is_symlink():
            if not entry.exists():
                entry.unlink()            # target is gone; the link pass re-creates it if it returns
            continue
        if _is_sidecar(name):
            # A database takes its real sidecars along (below), so one still standing here has no
            # database to own it. Park it: injecting a stray -wal beside a canonical database would
            # hand SQLite frames it never wrote.
            if entry.exists():
                _park(entry)
            continue
        real.mkdir(parents=True, exist_ok=True)
        target = real / name
        family = [entry, *_real_sidecars(entry)]
        if target.exists() or target.is_symlink():
            for member in family:
                _park(member)             # canonical entry wins; keep ours aside, unlinked
        else:
            for member in family:
                _relocate(member, real / member.name)
    # The link pass now sees every promoted name in ~/.codex and symlinks the base names back.


def ensure_home(email: str, *, codex_home: Path | None = None, root: Path | None = None,
                promote: bool = False) -> Path:
    """Create the account's home: real auth.json lives here; everything else symlinks to ~/.codex.

    Idempotent. Re-links any new shared entries that appeared in ~/.codex since last time, and never
    links a SQLite sidecar (rule 1) — stale sidecar links from older builds are removed regardless of
    ``promote``, since they are the only way to reach the error-14 mixed family.

    ``promote=True`` additionally MOVES real files a child created here into ~/.codex (parking a
    divergent copy) and drops dangling links. Only callers that know no supervised codex session is
    running may ask for it; the default keeps today's read-only behaviour.
    """
    real = codex_home or P.CODEX_HOME
    home = home_dir(email, root)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    for entry in list(home.iterdir()):   # materialised: we unlink while walking
        if entry.is_symlink() and _is_sidecar(entry.name) and ORPHAN_MARK not in entry.name:
            entry.unlink()                # rule 1: SQLite never needs these, and they break it
    if promote:
        _heal(home, real)
    if real.exists():
        for entry in real.iterdir():
            if entry.name == "auth.json":
                continue  # auth is per-account (a real file in the home)
            if _is_sidecar(entry.name):
                continue  # rule 1: SQLite finds the sidecars next to the resolved real database
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
    if home.is_symlink():
        home.unlink()   # a replaced home root: drop the link, never traverse into its target
        return True
    if not home.exists():
        return False
    # rmtree unlinks symlinked entries instead of following them (so ~/.codex is untouched) while
    # still removing the real auth.json, parked .orphaned-* copies and any real dir a child left.
    shutil.rmtree(home, ignore_errors=True)
    return not home.exists()
