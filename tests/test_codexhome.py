"""Per-account Codex homes keep exactly one real file (auth.json) and never a SQLite sidecar link.

A home that mixes a REAL database with symlinked ``-wal``/``-shm`` files makes SQLite fail every open
with error 14 ("unable to open database file") — the regression these tests pin down.
"""
import errno
import shutil
import sqlite3
from datetime import datetime

import pytest

from acctsw import codexhome

EMAIL = "a@x.com"


def _home(ctx):
    return codexhome.home_dir(EMAIL, ctx._homes_root)


def _ensure(ctx, *, promote=False):
    return codexhome.ensure_home(EMAIL, codex_home=ctx._codex_real, root=ctx._homes_root,
                                 promote=promote)


def _make_db(path, rows=(1,), hold=False):
    """A real WAL-mode database. A clean close checkpoints and REMOVES the ``-wal``/``-shm`` files,
    so ``hold=True`` returns the still-open connection to keep genuine sidecars on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.executemany("INSERT INTO t VALUES(?)", [(r,) for r in rows])
    conn.commit()
    if hold:
        return conn
    conn.close()
    return path


def _sidecars(path):
    """Stand in for the ``-wal``/``-shm`` files an open SQLite connection leaves beside a database
    (an empty wal is valid: zero frames), without depending on when SQLite checkpoints."""
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).write_bytes(b"")


def _parked(d):
    return sorted(p.name for p in d.iterdir() if codexhome.ORPHAN_MARK in p.name)


FROZEN_STAMP = "20260101T000000"


def _freeze_clock(monkeypatch):
    """``_park`` stamps to the second, so two heals inside one second collide by construction."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 0)

    monkeypatch.setattr(codexhome, "datetime", _Frozen)


# --- rule 1: sidecars are never linked ----------------------------------------------------------

def test_ensure_home_links_databases_but_never_their_sidecars(ctx):
    _sidecars(_make_db(ctx._codex_real / "logs_2.sqlite"))
    home = _ensure(ctx)
    assert (home / "logs_2.sqlite").is_symlink()
    assert not (home / "logs_2.sqlite-wal").exists() and not (home / "logs_2.sqlite-wal").is_symlink()
    assert not (home / "logs_2.sqlite-shm").exists() and not (home / "logs_2.sqlite-shm").is_symlink()


def test_ensure_home_removes_stale_sidecar_symlinks(ctx):
    """Links an older build created (both live and dangling) are the only route to a mixed family."""
    _sidecars(_make_db(ctx._codex_real / "logs_2.sqlite"))
    home = _ensure(ctx)
    (home / "logs_2.sqlite-wal").symlink_to(ctx._codex_real / "logs_2.sqlite-wal")
    (home / "memories_1.sqlite-shm").symlink_to(ctx._codex_real / "memories_1.sqlite-shm")  # dangling
    _ensure(ctx)
    assert not (home / "logs_2.sqlite-wal").is_symlink()
    assert not (home / "memories_1.sqlite-shm").is_symlink()
    assert (home / "logs_2.sqlite").is_symlink()          # the database link itself stays


# --- rule 2: promotion --------------------------------------------------------------------------

def test_promote_moves_a_database_the_child_created_into_the_real_home(ctx):
    """A name ~/.codex doesn't have yet (a Codex upgrade renaming logs_1 → logs_2) rejoins the
    shared home instead of drifting per seat."""
    home = _ensure(ctx)
    _sidecars(_make_db(home / "logs_2.sqlite", rows=(7,)))
    inode = (home / "logs_2.sqlite").stat().st_ino
    _ensure(ctx, promote=True)
    moved = ctx._codex_real / "logs_2.sqlite"
    assert moved.is_file() and not moved.is_symlink()
    assert moved.stat().st_ino == inode
    assert (home / "logs_2.sqlite").is_symlink()
    assert (home / "logs_2.sqlite").resolve() == moved.resolve()
    assert (ctx._codex_real / "logs_2.sqlite-wal").exists()   # the real sidecars travelled along
    assert not (home / "logs_2.sqlite-wal").is_symlink()
    assert _parked(home) == []
    conn = sqlite3.connect(moved)
    assert conn.execute("SELECT x FROM t").fetchall() == [(7,)]
    conn.close()


def test_promote_parks_a_divergent_copy_when_the_real_home_already_has_it(ctx):
    canonical = _make_db(ctx._codex_real / "logs_2.sqlite", rows=(1,))
    canonical_bytes = canonical.read_bytes()
    home = _ensure(ctx)
    (home / "logs_2.sqlite").unlink()
    _sidecars(_make_db(home / "logs_2.sqlite", rows=(99,)))
    _ensure(ctx, promote=True)
    assert canonical.read_bytes() == canonical_bytes          # canonical wins, untouched
    parked = _parked(home)
    assert any(n.startswith("logs_2.sqlite" + codexhome.ORPHAN_MARK) for n in parked)
    assert any(n.startswith("logs_2.sqlite-wal" + codexhome.ORPHAN_MARK) for n in parked)
    assert (home / "logs_2.sqlite").is_symlink()
    assert (home / "logs_2.sqlite").resolve() == canonical.resolve()


def test_relocate_falls_back_to_copy_when_rename_crosses_filesystems(ctx, monkeypatch):
    """A home on another volume than ~/.codex cannot be renamed across the boundary. Promotion must
    still move the database and its sidecars, byte for byte, and link the base name back."""
    home = _ensure(ctx)
    _sidecars(_make_db(home / "logs_2.sqlite", rows=(7,)))
    payload = (home / "logs_2.sqlite").read_bytes()
    attempted = []

    def _cross_device(src, dst):
        attempted.append(str(src))
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(codexhome.os, "replace", _cross_device)
    _ensure(ctx, promote=True)

    moved = ctx._codex_real / "logs_2.sqlite"
    assert attempted                                          # the rename really was tried first
    assert moved.is_file() and not moved.is_symlink()
    assert moved.read_bytes() == payload
    assert (ctx._codex_real / "logs_2.sqlite-wal").exists()    # the real sidecars travelled along
    assert (home / "logs_2.sqlite").is_symlink()
    assert (home / "logs_2.sqlite").resolve() == moved.resolve()
    assert not (home / "logs_2.sqlite-wal").exists()           # nothing left behind in the home
    assert _parked(home) == []
    conn = sqlite3.connect(moved)
    assert conn.execute("SELECT x FROM t").fetchall() == [(7,)]
    conn.close()


def test_park_never_clobbers_an_existing_parked_copy(ctx, monkeypatch):
    """Two heals in the same second land on the same stamp. Each divergent copy gets its own
    ``-2`` / ``-3`` suffix instead of overwriting the copy parked a moment earlier."""
    canonical = _make_db(ctx._codex_real / "logs_2.sqlite", rows=(1,))
    canonical_bytes = canonical.read_bytes()
    home = _ensure(ctx)
    _freeze_clock(monkeypatch)
    base = f"logs_2.sqlite{codexhome.ORPHAN_MARK}{FROZEN_STAMP}"
    (home / base).write_bytes(b"parked-earlier")

    for n, rows in ((2, (99,)), (3, (98,))):
        (home / "logs_2.sqlite").unlink()          # drop the link; the child writes its own copy
        _make_db(home / "logs_2.sqlite", rows=rows)
        divergent = (home / "logs_2.sqlite").read_bytes()
        _ensure(ctx, promote=True)
        assert (home / f"{base}-{n}").read_bytes() == divergent
        assert (home / base).read_bytes() == b"parked-earlier"   # the earlier copy is untouched
        assert canonical.read_bytes() == canonical_bytes         # canonical still wins
        assert (home / "logs_2.sqlite").is_symlink()
        assert (home / "logs_2.sqlite").resolve() == canonical.resolve()

    assert _parked(home) == [base, f"{base}-2", f"{base}-3"]


def test_promote_false_leaves_real_files_alone(ctx):
    home = _ensure(ctx)
    _sidecars(_make_db(home / "logs_2.sqlite", rows=(7,)))
    _ensure(ctx)
    assert (home / "logs_2.sqlite").is_file() and not (home / "logs_2.sqlite").is_symlink()
    assert not (ctx._codex_real / "logs_2.sqlite").exists()
    assert _parked(home) == []
    assert not (home / "logs_2.sqlite-wal").is_symlink()      # rule 1 still applies


def test_promote_never_touches_auth_json(ctx):
    codexhome.save(EMAIL, '{"tokens": {}}', codex_home=ctx._codex_real, root=ctx._homes_root)
    (ctx._codex_real).mkdir(parents=True, exist_ok=True)
    (ctx._codex_real / "auth.json").write_text('{"other": true}')
    home = _ensure(ctx, promote=True)
    assert (home / "auth.json").is_file() and not (home / "auth.json").is_symlink()
    assert codexhome.load(EMAIL, root=ctx._homes_root) == '{"tokens": {}}'
    assert (ctx._codex_real / "auth.json").read_text() == '{"other": true}'
    assert _parked(home) == []


def test_promote_removes_dangling_symlinks(ctx):
    (ctx._codex_real / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (ctx._codex_real / "config.toml").write_text("model = 'gpt'\n")
    (ctx._codex_real / "gone.json").write_text("{}")
    home = _ensure(ctx)
    (ctx._codex_real / "gone.json").unlink()
    _ensure(ctx, promote=True)
    assert not (home / "gone.json").is_symlink()
    assert (home / "config.toml").is_symlink()


def test_promote_parks_a_sidecar_left_without_its_database(ctx):
    """Half of a healed family: the database is already a link, so its leftover real -wal has no
    owner. It is parked, never moved next to the canonical database."""
    _make_db(ctx._codex_real / "logs_2.sqlite")
    home = _ensure(ctx)
    (home / "logs_2.sqlite-wal").write_bytes(b"stale")
    _ensure(ctx, promote=True)
    parked = _parked(home)
    assert len(parked) == 1 and parked[0].startswith("logs_2.sqlite-wal" + codexhome.ORPHAN_MARK)
    assert (home / parked[0]).read_bytes() == b"stale"
    assert not (ctx._codex_real / "logs_2.sqlite-wal").exists()
    assert (home / "logs_2.sqlite").is_symlink()


def test_orphaned_files_are_ignored_on_later_runs(ctx):
    home = _ensure(ctx)
    parked = home / f"logs_2.sqlite{codexhome.ORPHAN_MARK}20260101T000000"
    parked.write_bytes(b"old")
    for _ in range(2):
        _ensure(ctx, promote=True)
    assert parked.read_bytes() == b"old"
    assert _parked(home) == [parked.name]                     # never re-parked
    assert not (ctx._codex_real / parked.name).exists()        # never promoted


def test_sqlite_error_14_regression(ctx):
    """A real database beside symlinked sidecars breaks EVERY open; the heal must fix it in place."""
    canonical = ctx._codex_real / "db.sqlite"
    live = _make_db(canonical, hold=True)   # open: the real -wal/-shm stay on disk
    home = _ensure(ctx)
    (home / "db.sqlite").unlink()
    shutil.copy2(canonical, home / "db.sqlite")     # the child's own real copy
    for suffix in ("-wal", "-shm"):
        (home / ("db.sqlite" + suffix)).symlink_to(ctx._codex_real / ("db.sqlite" + suffix))

    with pytest.raises(sqlite3.OperationalError):
        conn = sqlite3.connect(home / "db.sqlite")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO t VALUES(2)")
            conn.commit()
        finally:
            conn.close()

    _ensure(ctx, promote=True)

    conn = sqlite3.connect(home / "db.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t VALUES(2)")
    conn.commit()
    conn.close()
    assert live.execute("SELECT x FROM t ORDER BY x").fetchall() == [(1,), (2,)]
    live.close()                                               # the write landed in the shared home


def test_delete_removes_parked_files_and_never_follows_symlinks(ctx):
    (ctx._codex_real / "sessions").mkdir(parents=True)
    (ctx._codex_real / "sessions" / "keep.jsonl").write_text("{}\n")
    home = _ensure(ctx)
    (home / f"logs_2.sqlite{codexhome.ORPHAN_MARK}20260101T000000").write_bytes(b"old")
    assert (home / "sessions").is_symlink()
    assert codexhome.delete(EMAIL, root=ctx._homes_root) is True
    assert not home.exists()
    assert (ctx._codex_real / "sessions" / "keep.jsonl").read_text() == "{}\n"

    home.symlink_to(ctx._codex_real)          # a home root replaced by a link: drop the link only
    assert codexhome.delete(EMAIL, root=ctx._homes_root) is True
    assert not home.is_symlink()
    assert (ctx._codex_real / "sessions" / "keep.jsonl").exists()
