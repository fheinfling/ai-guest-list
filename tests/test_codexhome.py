"""Per-account Codex homes keep exactly one real file (auth.json) and never a SQLite sidecar link.

A home that mixes a REAL database with symlinked ``-wal``/``-shm`` files makes SQLite fail every open
with error 14 ("unable to open database file") — the regression these tests pin down.
"""
import errno
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from acctsw import codexhome
from acctsw import paths as P

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


# --- rule 4: the app-server daemon's runtime state is per-seat -----------------------------------

def test_daemon_runtime_is_never_linked_in_from_the_shared_home(ctx):
    """A shared control socket would hand this seat the daemon that holds ANOTHER account's auth."""
    for name in ("app-server-control", "app-server-daemon"):
        (ctx._codex_real / name).mkdir(parents=True)
    (ctx._codex_real / "config.toml").write_text("x")

    home = _ensure(ctx)

    assert (home / "config.toml").is_symlink()                  # shared state still links
    assert not (home / "app-server-control").exists()
    assert not (home / "app-server-daemon").exists()


def test_stale_daemon_links_from_an_older_build_are_removed(ctx):
    (ctx._codex_real / "app-server-daemon").mkdir(parents=True)
    home = _ensure(ctx)
    (home / "app-server-daemon").symlink_to(ctx._codex_real / "app-server-daemon")

    _ensure(ctx)

    assert not (home / "app-server-daemon").is_symlink()


def test_daemon_runtime_survives_promotion_in_the_seats_own_home(ctx):
    home = _ensure(ctx)
    sock_dir = home / "app-server-control"
    sock_dir.mkdir()
    (sock_dir / "app-server-startup.lock").write_bytes(b"")
    (home / "app-server-daemon").mkdir()
    _pid(home, os.getpid(), daemonprocs.process_start(os.getpid())[0])
    pid_record = (home / 'app-server-daemon/daemon.pid').read_text()

    _ensure(ctx, promote=True)

    assert (sock_dir / "app-server-startup.lock").exists()      # still real, still this seat's
    assert (home / "app-server-daemon" / "daemon.pid").read_text() == pid_record
    assert not (ctx._codex_real / "app-server-control").exists()
    assert not (ctx._codex_real / "app-server-daemon").exists()


# --- the socket-path budget (SUN_LEN) ------------------------------------------------------------

def test_a_seat_home_leaves_room_for_the_daemon_control_socket():
    """codex 0.157+ binds <CODEX_HOME>/app-server-control/app-server-control.sock and connects to
    that literal path — macOS rejects it beyond SUN_LEN, which is why the home is short. The address
    is the longest part of a seat and must not reach the path, whatever length it is."""
    store = Path("/Users/a-twenty-char-user/.account-switcher") / P.CODEX_HOMES.name
    home = codexhome.home_dir("someone.with.a.very.long.address+codex@example.com", store)

    assert len(str(home)) <= codexhome.MAX_HOME_LEN
    assert codexhome.daemon_socket_fits(home)
    assert not codexhome.daemon_socket_fits(home / "one-directory-deeper-than-fits")


def test_the_store_layout_fits_up_to_a_23_character_username():
    """Where the layout runs out of room — an environment fact, pinned on synthetic homes so it is
    not the test machine's own username that decides whether the suite passes."""
    def fits(user):
        store = Path(f"/Users/{user}/.account-switcher") / P.CODEX_HOMES.name
        return codexhome.daemon_socket_fits(codexhome.home_dir(EMAIL, store))

    assert fits("u" * 23)
    assert not fits("u" * 24)


def test_the_fit_check_measures_the_path_codex_actually_binds(tmp_path):
    """codex resolves CODEX_HOME before building the socket path, so a short home that is really a
    symlink into a deep directory still fails — and the check has to fail with it."""
    deep = tmp_path / ("d" * 40) / ("e" * 40)
    deep.mkdir(parents=True)
    short = Path("/tmp") / f"acctsw-fit-{os.getpid()}"
    short.symlink_to(deep)
    try:
        assert len(str(short)) <= codexhome.MAX_HOME_LEN     # short by name …
        assert not codexhome.daemon_socket_fits(short)       # … but not where it lands
    finally:
        short.unlink()


# --- the by-address index (and the pre-1.0.2 move) -----------------------------------------------

def test_a_seat_is_findable_by_address(ctx):
    home = _ensure(ctx)
    link = codexhome.by_address(EMAIL, ctx._homes_root)

    assert link.is_symlink() and link.resolve() == home.resolve()
    assert not os.path.isabs(os.readlink(link))                 # relative: the store stays movable


def _legacy_home(ctx, token='{"tokens": {}}'):
    """A seat as the pre-1.0.2 layout left it: a real directory named by address."""
    old = codexhome.by_address(EMAIL, ctx._homes_root)
    old.mkdir(parents=True)
    (old / "auth.json").write_text(token)
    (old / f"x.sqlite{codexhome.ORPHAN_MARK}20260101T000000").write_bytes(b"parked")
    (old / "sessions").symlink_to(ctx._codex_real / "sessions")
    return old


def test_a_pre_1_0_2_home_is_moved_onto_the_short_path_with_its_auth(ctx):
    old = _legacy_home(ctx)

    home = _ensure(ctx)

    assert (home / "auth.json").read_text() == '{"tokens": {}}'
    assert (home / f"x.sqlite{codexhome.ORPHAN_MARK}20260101T000000").read_bytes() == b"parked"
    assert (home / "sessions").is_symlink()
    assert old.is_symlink() and old.resolve() == home.resolve()  # the address still finds the seat


def test_reading_a_seat_never_moves_it(ctx):
    """Usage polling reads seats on a timer, outside the state lock. A read that renamed the home
    would pull it out from under a codex child standing in it — and the pre-1.0.2 path that child
    still holds would be gone with it."""
    old = _legacy_home(ctx, token="legacy-token")

    assert codexhome.load(EMAIL, root=ctx._homes_root) == "legacy-token"

    assert old.is_dir() and not old.is_symlink()
    assert not _home(ctx).exists()


def test_adoption_that_loses_the_race_changes_nothing(ctx):
    """If the short home appeared meanwhile, the legacy directory stays put — it must never end up
    nested INSIDE the new home, where the next promoting heal would hand it to ~/.codex."""
    codexhome.save(EMAIL, "winning token", codex_home=ctx._codex_real, root=ctx._homes_root)
    home = _home(ctx)                         # the winner: short home + index already published
    old = codexhome.by_address(EMAIL, ctx._homes_root)
    old.unlink()
    old.mkdir(parents=True)                   # the loser, still holding a real directory
    (old / "auth.json").write_text("racing token")

    codexhome._adopt_legacy(EMAIL, ctx._homes_root)

    assert (old / "auth.json").read_text() == "racing token"
    assert (home / "auth.json").read_text() == "winning token"
    assert [p.name for p in home.iterdir() if not p.is_symlink()] == ["auth.json"]


def test_an_un_adoptable_legacy_home_is_never_clobbered(ctx):
    home = _ensure(ctx)                       # short home exists first …
    old = codexhome.by_address(EMAIL, ctx._homes_root)
    old.unlink()
    old.mkdir(parents=True)                   # … so this one cannot be moved onto it
    (old / "auth.json").write_text("someone's token")

    _ensure(ctx)

    assert (old / "auth.json").read_text() == "someone's token"
    assert home.is_dir()


def test_delete_removes_both_the_home_and_its_address(ctx):
    home = _ensure(ctx)
    link = codexhome.by_address(EMAIL, ctx._homes_root)

    assert codexhome.delete(EMAIL, root=ctx._homes_root) is True
    assert not home.exists() and not link.is_symlink() and not link.exists()


def test_delete_reports_failure_while_the_token_is_still_on_disk(ctx, monkeypatch):
    """Dropping the address must not read as "seat erased" when the home survived the rmtree."""
    _ensure(ctx)
    codexhome.save(EMAIL, "still-here", codex_home=ctx._codex_real, root=ctx._homes_root)
    monkeypatch.setattr(codexhome.shutil, "rmtree", lambda *a, **k: None)

    assert codexhome.delete(EMAIL, root=ctx._homes_root) is False
    assert codexhome.load(EMAIL, root=ctx._homes_root) == "still-here"


def test_one_login_spelled_two_ways_is_one_home(ctx):
    """``codex_jwt_matches`` case-folds, and the old address-named homes collapsed on a
    case-insensitive filesystem — the hash has to agree with both."""
    assert codexhome.home_dir("A@X.com", ctx._homes_root) == codexhome.home_dir("a@x.com",
                                                                               ctx._homes_root)


def test_a_repointed_address_is_replaced_atomically(ctx):
    """A stale index entry is repaired without ever unlinking it first."""
    home = _ensure(ctx)
    link = codexhome.by_address(EMAIL, ctx._homes_root)
    link.unlink()
    link.symlink_to("../somewhere-else")

    _ensure(ctx)

    assert link.resolve() == home.resolve()
    assert not any(p.name.startswith(".") and p.name.endswith(".tmp")
                   for p in link.parent.iterdir())        # nothing staged left behind

# --- rule 5 and explicit daemon maintenance ---------------------------------------------------
import fcntl
import json
import signal
import socket
import subprocess
import sys
import tempfile

from acctsw import daemonprocs


@pytest.fixture
def short_home(monkeypatch):
    # macOS pytest paths routinely exceed SUN_LEN. Real sockets need a genuinely short parent.
    with tempfile.TemporaryDirectory(prefix='cx-', dir='/tmp') as tmp:
        base = Path(tmp).resolve()
        home = base / 'h'
        (home / 'app-server-control').mkdir(parents=True)
        (home / 'app-server-daemon').mkdir()
        targets = base / 's'
        targets.mkdir()
        monkeypatch.setattr(codexhome, 'TMP_DAEMONS', targets)
        yield home, targets


def _socket(home, target, *, listen=False):
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(target))
    except PermissionError:
        server.close()
        pytest.skip('execution sandbox denies AF_UNIX bind')
    if listen:
        server.listen()
    codexhome.socket_path(home).symlink_to(target)
    return server


def _pid(home, pid, seconds, micros=None):
    identity = {'startSeconds': seconds}
    if micros is not None:
        identity['startMicroseconds'] = micros
    (home / 'app-server-daemon/daemon.pid').write_text(json.dumps(
        {'pid': pid, 'processIdentity': identity}))


def test_daemon_live_socket_untouched(short_home):
    home, targets = short_home
    with _socket(home, targets / 'sock', listen=True):
        assert codexhome.heal_daemon_socket(home) == 'live'
        assert codexhome.socket_path(home).is_symlink()
        assert (targets / 'sock').exists()


def test_daemon_closed_socket_dead_pid_healed(short_home, monkeypatch):
    home, targets = short_home
    _socket(home, targets / 'sock').close()
    _pid(home, 123456, 1)
    monkeypatch.setattr(daemonprocs, 'alive', lambda pid: False)
    (home / codexhome.STARTUP_LOCK).touch()
    (home / 'app-server-daemon/daemon-updater.pid').write_text('leave alone')
    assert codexhome.daemon_state(home) == 'stale'
    assert codexhome.heal_daemon_socket(home) == 'healed'
    assert not codexhome.socket_path(home).is_symlink()
    assert not (targets / 'sock').exists()
    assert not (home / codexhome.STARTUP_LOCK).exists()
    assert not (home / 'app-server-daemon/daemon.pid').exists()
    assert len(list((home / 'app-server-daemon').glob('daemon.pid.stale-*'))) == 1
    assert (home / 'app-server-daemon/daemon-updater.pid').read_text() == 'leave alone'
    assert codexhome.heal_daemon_socket(home) == 'absent'


def test_daemon_dangling_symlink(short_home):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    assert codexhome.heal_daemon_socket(home) == 'healed'
    assert not codexhome.socket_path(home).is_symlink()


@pytest.mark.parametrize('record', ['{', 'null', '[]', '{"pid":0}', '{"pid":"12"}'])
def test_daemon_bad_pid_record(short_home, record):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    (home / 'app-server-daemon/daemon.pid').write_text(record)
    assert codexhome.heal_daemon_socket(home) == 'healed'


@pytest.mark.parametrize('reuse', [False, True])
def test_daemon_live_pid_start_identity(short_home, reuse):
    home, targets = short_home
    _socket(home, targets / 'sock').close()
    start = daemonprocs.process_start(os.getpid())
    assert start is not None
    _pid(home, os.getpid(), start[0] - int(reuse))
    assert codexhome.heal_daemon_socket(home) == ('healed' if reuse else 'busy')
    assert codexhome.socket_path(home).is_symlink() != reuse


def test_daemon_unknown_start_is_busy(short_home, monkeypatch):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    _pid(home, os.getpid(), 1)
    monkeypatch.setattr(daemonprocs, 'process_start', lambda pid: None)
    assert codexhome.heal_daemon_socket(home) == 'busy'


def test_daemon_held_startup_flock(short_home):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    # A separate process owns the lock; its stdout is just a synchronization pipe for this test.
    child = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX); '
        'print("ready",flush=True); sys.stdin.read()', str(home / codexhome.STARTUP_LOCK)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline()
        assert codexhome.daemon_state(home) == 'busy'
        assert codexhome.heal_daemon_socket(home) == 'busy'
        assert codexhome.socket_path(home).is_symlink()
    finally:
        child.communicate(timeout=5)
    assert codexhome.heal_daemon_socket(home) == 'healed'


@pytest.mark.parametrize('error', [errno.EACCES, errno.EAGAIN, errno.ENAMETOOLONG])
def test_daemon_unknown_connect_error_defers(short_home, monkeypatch, error):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    monkeypatch.setattr(codexhome, '_connect_errno', lambda path: error)
    assert codexhome.heal_daemon_socket(home) == 'busy'
    assert codexhome.socket_path(home).is_symlink()


def test_daemon_tmp_target_owner_check(short_home, monkeypatch):
    home, targets = short_home
    target = targets / 'sock'
    _socket(home, target).close()
    monkeypatch.setattr(codexhome.os, 'getuid', lambda: target.stat().st_uid + 1)
    assert codexhome.heal_daemon_socket(home) == 'healed'
    assert target.exists()                 # alias belongs to the seat; target does not belong to us


def test_daemon_external_regular_target_preserved(short_home):
    home, targets = short_home
    target = targets / 'not-a-socket'
    target.write_text('keep')
    codexhome.socket_path(home).symlink_to(target)
    assert codexhome.heal_daemon_socket(home) in ('healed', 'busy')  # ENOTSOCK on Darwin
    assert target.read_text() == 'keep'


def test_daemon_runtime_parent_escape_defers(short_home):
    home, targets = short_home
    (home / 'app-server-control').rmdir()
    (home / 'app-server-control').symlink_to(targets)
    assert codexhome.heal_daemon_socket(home) == 'busy'


def test_ensure_home_runs_heal_and_keeps_packages_private(ctx, monkeypatch):
    home = _ensure(ctx)
    (home / 'packages').mkdir()
    (home / 'packages/keep').write_text('seat')
    (ctx._codex_real / 'packages').mkdir(parents=True)
    (ctx._codex_real / 'packages/shared').write_text('shared')
    calls = []
    monkeypatch.setattr(codexhome, 'heal_daemon_socket', lambda h: calls.append(h))
    _ensure(ctx, promote=True)
    assert calls == [home]
    assert (home / 'packages/keep').read_text() == 'seat'
    assert not (home / 'packages/shared').exists()
    shutil.rmtree(home / 'packages')
    (home / 'packages').symlink_to(ctx._codex_real / 'packages')
    _ensure(ctx)
    assert not (home / 'packages').exists()
    assert (ctx._codex_real / 'packages/shared').exists()


def _proc(pid, path, *, uid=None, start=(1, 0)):
    return daemonprocs.Process(pid, os.getuid() if uid is None else uid,
                              (str(path), 'app-server', '--managed-daemon'), Path(path), start)


def _register(ctx):
    state = ctx.load_state()
    state.upsert_seat('codex', EMAIL)
    state.save()
    return ctx.codex_home(EMAIL)


def test_reaper_only_legacy_and_unknown_homes(ctx):
    live = _register(ctx)
    legacy = codexhome.by_address(EMAIL, ctx._homes_root)
    legacy.parent.mkdir()
    live.mkdir(parents=True)
    legacy.symlink_to(live)                # must NOT resolve this before matching
    suffix = 'packages/app-server-daemon/releases/v/bin/codex'
    processes = [_proc(11, legacy / suffix), _proc(12, ctx._homes_root / 'unknown' / suffix),
                 _proc(13, live / suffix), _proc(14, legacy / suffix, uid=os.getuid()+1),
                 _proc(15, ctx.data_dir / 'other' / suffix),
                 _proc(16, legacy / 'packages/app-server-daemon-evil/releases/v/bin/codex')]
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        if pid == 11 or sig == signal.SIGKILL:
            processes[:] = [p for p in processes if p.pid != pid]
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=lambda: processes[:],
                                       kill=kill, sleep=lambda _: None) == [11, 12]
    assert calls == [(11, signal.SIGTERM), (12, signal.SIGTERM), (12, signal.SIGKILL)]
    assert {p.pid for p in processes} == {13, 14, 15, 16}


def test_reaper_preserves_recycled_pid(ctx):
    path = ctx._homes_root / 'old/packages/app-server-daemon/releases/v/bin/codex'
    processes = [_proc(11, path)]
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        processes[:] = [_proc(pid, path, start=(2, 0))]
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=lambda: processes[:],
                                       kill=kill, sleep=lambda _: None) == [11]
    assert calls == [(11, signal.SIGTERM)]


def test_reaper_rechecks_each_target_immediately_before_signal(ctx):
    path = ctx._homes_root / 'old/packages/app-server-daemon/releases/v/bin/codex'
    processes = [_proc(11, path), _proc(12, path)]
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        processes[:] = [_proc(12, path, start=(2, 0))]
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=lambda: processes[:],
                                       kill=kill, sleep=lambda _: None) == [11]
    assert calls == [(11, signal.SIGTERM)]


def test_reaper_snapshot_failure_and_bad_registry_defer(ctx):
    def fail():
        raise OSError('unavailable')
    def kill(*args):
        pytest.fail('must not signal without a trusted snapshot')
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=fail, kill=kill) == []
    ctx.state_file.write_text('{}')
    path = ctx._homes_root / 'old/packages/app-server-daemon/releases/v/bin/codex'
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=lambda: [_proc(1, path)],
                                       kill=kill) == []


def test_reaper_cleans_only_corresponding_dead_socket(short_home, tmp_path):
    home, targets = short_home
    root = home.parent / 'ch'
    orphan = root / 'old'
    (orphan / 'app-server-control').mkdir(parents=True)
    _socket(orphan, targets / 'dead').close()
    (targets / 'dead.lock').touch()
    other = socket.socket(socket.AF_UNIX)
    other.bind(str(targets / 'unrelated'))
    other.close()
    processes = [_proc(11, orphan / 'packages/app-server-daemon/releases/v/bin/codex')]
    result = codexhome.reap_orphan_daemons(root, list_procs=lambda: processes[:],
                                         kill=lambda *_: processes.clear(), sleep=lambda _: None)
    assert result == [11]
    assert not (targets / 'dead').exists()
    assert not (targets / 'dead.lock').exists()
    assert (targets / 'unrelated').exists()


def _releases(home):
    package = home / 'packages/app-server-daemon'
    for name in ('current-v', 'in-use', 'old'):
        release = package / 'releases' / name
        release.mkdir(parents=True)
        (release / 'codex').write_bytes(b'12345')
    (package / 'current').symlink_to(package / 'releases/current-v')
    return package


def test_gc_keeps_current_and_running_release(tmp_path):
    package = _releases(tmp_path)
    procs = [_proc(11, package / 'releases/in-use/codex')]
    assert codexhome.gc_daemon_releases(tmp_path, list_procs=lambda: procs) == 5
    assert {p.name for p in (package / 'releases').iterdir()} == {'current-v', 'in-use'}


def test_partial_darwin_snapshot_reaps_by_argv(ctx, monkeypatch):
    from tests.test_daemonprocs import install_darwin_table
    path = ctx._homes_root / 'old/packages/app-server-daemon/releases/v/bin/codex'
    records = [replace(_proc(11, path), executable=None),
               replace(_proc(12, path), argv=(), executable=None)]
    install_darwin_table(monkeypatch, records)
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        records[:] = [p for p in records if p.pid != pid]
    assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=daemonprocs.list_processes,
                                       kill=kill, sleep=lambda _: None) == [11]
    assert calls == [(11, signal.SIGTERM)]
    assert records[0].pid == 12


@pytest.mark.parametrize('missing', ['executable', 'argv', 'both'])
def test_partial_darwin_snapshot_gc_needs_either_path(tmp_path, monkeypatch, missing):
    from tests.test_daemonprocs import install_darwin_table
    package = _releases(tmp_path)
    proc = _proc(11, package / 'releases/in-use/codex')
    if missing in ('executable', 'both'):
        proc = replace(proc, executable=None)
    if missing in ('argv', 'both'):
        proc = replace(proc, argv=())
    install_darwin_table(monkeypatch, [proc])
    reasons = []
    freed = codexhome.gc_daemon_releases(tmp_path, list_procs=daemonprocs.list_processes,
                                         on_skip=reasons.append)
    assert freed == (0 if missing == 'both' else 5)
    assert (package / 'releases/in-use').exists()
    assert (package / 'releases/old').exists() == (missing == 'both')
    assert reasons == (['process-details-unavailable (1 live records lack executable and argv)']
                       if missing == 'both' else [])


@pytest.mark.parametrize('kind', ['foreign', 'exiting', 'zombie'])
def test_gc_ignores_intentionally_missing_details(tmp_path, kind):
    package = _releases(tmp_path)
    proc = replace(_proc(11, package / 'releases/in-use/codex'), executable=None, argv=(),
                   uid=os.getuid() + 1 if kind == 'foreign' else os.getuid(),
                   state='running' if kind == 'foreign' else kind)
    assert codexhome.gc_daemon_releases(tmp_path, list_procs=lambda: [proc]) == 10


@pytest.mark.parametrize('both_unknown', [False, True])
def test_maintenance_reports_partial_inspection_and_gc_reason(ctx, monkeypatch, both_unknown):
    from tests.test_daemonprocs import install_darwin_table
    home = _register(ctx)
    package = _releases(home)
    proc = replace(_proc(11, package / 'releases/in-use/codex'), executable=None)
    if both_unknown:
        proc = replace(proc, argv=())
    install_darwin_table(monkeypatch, [proc])
    # All process calls are injected; never signal a machine's existing processes.
    reaper = codexhome.reap_orphan_daemons
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', lambda root: reaper(
        root, list_procs=daemonprocs.list_processes,
        kill=lambda *_: pytest.fail('registered seat cannot be reaped')))
    gc = codexhome.gc_daemon_releases
    monkeypatch.setattr(codexhome, 'gc_daemon_releases', lambda home, **kw: gc(
        home, list_procs=daemonprocs.list_processes, **kw))
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['process_inspection'] == 'partial (1 records incomplete)'
    assert report['reaper'] == 'checked'
    row = report['seats'][0]
    assert row['gc'] == ('process-details-unavailable (1 live records lack executable and argv)'
                         if both_unknown else 'checked')
    assert row['bytes_freed'] == (0 if both_unknown else 5)


def test_maintenance_reports_details_lost_before_gc(ctx, monkeypatch):
    home = _register(ctx)
    package = _releases(home)
    proc = replace(_proc(11, package / 'releases/in-use/codex'), executable=None, argv=())
    monkeypatch.setattr(daemonprocs, 'list_processes', lambda: [])
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', lambda root: [])
    gc = codexhome.gc_daemon_releases
    monkeypatch.setattr(codexhome, 'gc_daemon_releases',
                        lambda home, **kw: gc(home, list_procs=lambda: [proc], **kw))
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['seats'][0]['gc'] == ('process-details-unavailable '
                                       '(1 live records lack executable and argv)')
    assert report['seats'][0]['bytes_freed'] == 0


def test_gc_install_lock_held(tmp_path):
    package = _releases(tmp_path)
    with (package / 'install.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert codexhome.gc_daemon_releases(tmp_path, list_procs=lambda: []) == 0
    assert (package / 'releases/old').exists()


@pytest.mark.parametrize('escape', ['release', 'current', 'packages', 'lock'])
def test_gc_symlink_escape_defers(tmp_path, escape):
    home = tmp_path / 'home'
    package = _releases(home)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    if escape == 'release':
        (package / 'releases/escape').symlink_to(outside)
    elif escape == 'current':
        (package / 'current').unlink()
        (package / 'current').symlink_to(outside)
    elif escape == 'lock':
        (package / 'install.lock').symlink_to(outside / 'keep')
    else:
        shutil.move(home / 'packages', outside / 'packages')
        (home / 'packages').symlink_to(outside / 'packages')
    assert codexhome.gc_daemon_releases(home, list_procs=lambda: []) == 0
    assert (outside / 'keep').read_text() == 'keep'
    assert (home / 'packages/app-server-daemon/releases/old').exists()


def test_gc_missing_current_and_unknown_processes_defer(tmp_path):
    package = _releases(tmp_path)
    def fail():
        raise OSError('cannot inspect')
    assert codexhome.gc_daemon_releases(tmp_path, list_procs=fail) == 0
    (package / 'current').unlink()
    assert codexhome.gc_daemon_releases(tmp_path, list_procs=lambda: []) == 0
    assert (package / 'releases/old').exists()


def test_gc_never_follows_nested_symlink(tmp_path):
    home = tmp_path / 'home'
    package = _releases(home)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (package / 'releases/old/link').symlink_to(outside)
    assert codexhome.gc_daemon_releases(home, list_procs=lambda: []) == 10
    assert (outside / 'keep').read_text() == 'keep'


def test_maintenance_skips_gc_with_supervised_session(ctx, monkeypatch):
    from acctsw import session
    home = _register(ctx)
    _releases(home)
    monkeypatch.setattr(session, 'active_session', lambda *args: {'email': 'another@seat'})
    monkeypatch.setattr(codexhome, 'orphan_daemons', lambda *args, **kwargs: [])
    monkeypatch.setattr(codexhome.daemonprocs, 'list_processes', lambda: [])
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', lambda *args: [])
    def forbidden(*args):
        pytest.fail('GC during a supervised session')
    monkeypatch.setattr(codexhome, 'gc_daemon_releases', forbidden)
    monkeypatch.setattr(codexhome, 'reap_orphan_daemons', forbidden)
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['seats'][0]['gc'] == 'session-live'
    assert report['seats'][0]['bytes_freed'] == 0


def test_reaper_real_child_only(ctx):
    """Signal a process THIS test spawned; identity snapshots still come from the kernel."""
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    path = ctx._homes_root / 'removed/packages/app-server-daemon/releases/v/bin/codex'
    try:
        start = daemonprocs.process_start(child.pid)
        assert start is not None
        def snapshot():
            return [] if child.poll() is not None else [_proc(child.pid, path, start=start)]
        assert codexhome.reap_orphan_daemons(ctx._homes_root, list_procs=snapshot) == [child.pid]
        assert child.returncode == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_dead_tmp_cleanup_preserves_listener_and_held_lock(short_home):
    home, targets = short_home
    target = targets / 'sock'
    with _socket(home, target, listen=True):
        codexhome._unlink_dead_tmp_socket(target, lock=True)
        assert target.exists()
    lockfile = targets / 'sock.lock'
    with lockfile.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        codexhome._unlink_dead_tmp_socket(target, lock=True)
        assert target.exists()
        assert lockfile.exists()
    codexhome._unlink_dead_tmp_socket(target, lock=True)
    assert not target.exists()
    assert not lockfile.exists()


def test_daemon_microsecond_reuse(short_home):
    home, targets = short_home
    codexhome.socket_path(home).symlink_to(targets / 'missing')
    seconds, micros = daemonprocs.process_start(os.getpid())
    _pid(home, os.getpid(), seconds, (micros + 1) % 1_000_000)
    assert codexhome.heal_daemon_socket(home) == 'healed'


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel argv ABI')
def test_kernel_argv_preserves_spaces_and_excludes_environment():
    from acctsw.daemonprocs import _argv
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()', 'arg with spaces'],
                             stdin=subprocess.PIPE, env={**os.environ, 'DAEMON_TEST_SECRET': 'nope'})
    try:
        args = _argv(child.pid)
        assert args[-1] == 'arg with spaces'
        assert all('DAEMON_TEST_SECRET' not in arg for arg in args)
        assert len(args) == 4
    finally:
        child.communicate(timeout=5)


def test_maintenance_reports_unavailable_process_inspection(ctx, monkeypatch):
    home = _register(ctx)
    _releases(home)
    def fail():
        raise OSError('blocked')
    monkeypatch.setattr(daemonprocs, 'list_processes', fail)
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['process_inspection'] == 'unavailable'
    assert report['reaper'] == 'processes-unavailable'
    assert report['seats'][0]['gc'] == 'processes-unavailable'
    assert (home / 'packages/app-server-daemon/releases/old').exists()


def test_gc_invalid_current_pointing_at_release_collection_defers(tmp_path):
    package = _releases(tmp_path)
    (package / 'current').unlink()
    (package / 'current').symlink_to(package / 'releases')
    assert codexhome.gc_daemon_releases(tmp_path, list_procs=lambda: []) == 0
    assert (package / 'releases/old').exists()


@pytest.mark.parametrize('state', ['exiting', 'zombie', 'running', None])
@pytest.mark.parametrize('socket_present', [True, False])
def test_daemon_kernel_state_and_pid_archive(short_home, monkeypatch, state, socket_present):
    home, targets = short_home
    if socket_present:
        codexhome.socket_path(home).symlink_to(targets / 'sock')
        monkeypatch.setattr(codexhome, '_connect_errno', lambda _: errno.ECONNREFUSED)
    _pid(home, 123456, 42, 123)
    pidfile = home / 'app-server-daemon/daemon.pid'
    original = pidfile.read_bytes()
    monkeypatch.setattr(daemonprocs, 'alive', lambda pid: True)
    monkeypatch.setattr(daemonprocs, 'process_start', lambda pid: (42, 123))
    record = None if state is None else daemonprocs.ProcessState(os.getuid(), 1, False, (42, 123), state)
    monkeypatch.setattr(daemonprocs, 'process_state', lambda pid: record)
    if state in ('exiting', 'zombie'):
        assert codexhome.heal_daemon_socket(home) == 'healed'
        assert not pidfile.exists()
        archived, = pidfile.parent.glob('daemon.pid.stale-*')
        assert archived.read_bytes() == original
        assert codexhome.heal_daemon_socket(home) == 'absent'
    else:
        assert codexhome.heal_daemon_socket(home) == 'busy'
        assert pidfile.read_bytes() == original
        assert not list(pidfile.parent.glob('daemon.pid.stale-*'))


def test_maintenance_rescues_before_live_session_gate(ctx, monkeypatch):
    from tests.test_daemonprocs import supervisor, child
    table = [supervisor(), child()]
    signals = []
    monkeypatch.setattr(daemonprocs, 'list_processes', lambda: table)
    rescue = daemonprocs.wedged_supervisors
    monkeypatch.setattr(daemonprocs, 'wedged_supervisors', lambda **kw: rescue(
        **kw, sleep=lambda _: None, kill=lambda *args: signals.append(args)))
    monkeypatch.setattr('acctsw.session.active_session', lambda *args: {'pid': 101})
    report = codexhome.daemon_report(ctx)
    assert report['wedged_supervisors'] == [101]
    assert report['signalled_supervisors'] == signals == []
    report = codexhome.daemon_report(ctx, fix=True)
    assert report['wedged_supervisors'] == report['signalled_supervisors'] == [101]
    assert signals == [(101, signal.SIGKILL)]
    assert report['reaper'] == 'session-live'
