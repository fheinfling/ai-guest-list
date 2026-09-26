"""Per-account Codex homes (isolation, spec §4).

Each Codex account gets its own ``CODEX_HOME`` directory under ~/.account-switcher/ch/<id>/
containing that account's REAL ``auth.json`` plus symlinks to everything else in the user's real
``~/.codex`` (config.toml, sessions, plugins, sqlite, …). codex run with that ``CODEX_HOME`` reads
and refreshes the account's own auth.json in place — so using/rotating one account NEVER touches
another (the cross-invalidation that the shared-auth.json swap model suffered). Sessions/config are
shared via the symlinks, so cross-account `codex resume` still works.

This keeps the user's real ~/.codex as the shared source of truth (we never relocate it); only
auth.json is per-account. Fully reversible: delete the store's home root.

``<id>`` is a hash, not the address, because the path length is a hard constraint — see
``MAX_HOME_LEN``. ``codex-homes/<address>`` remains as a symlink into ``ch/<id>`` so a seat is still
findable by address (and a child spawned before the move keeps resolving its files).

The index carries an ENGINE of this version or newer; it is not a compatibility layer for an older
one. A pre-1.0.2 engine still reads and writes a seat correctly through it, but it knows neither
rule 4 (its heal would promote a seat's daemon runtime into ~/.codex and link the shared copy into
every other seat) nor this layout (its guards recognise only ``codex-homes``, so a short home
inherited as ``CODEX_HOME`` looks like a user's own custom mirror, and its ``delete`` drops the
address while leaving the home). That window is an app upgrade with the previous menubar process
still resident, and it closes when the app restarts — which is what an upgrade should do.

INVARIANT: a home holds exactly ONE real file, ``auth.json`` — plus the daemon's own runtime state
(rule 4). Everything else is shared state owned by the real ~/.codex and appears here only as a
symlink. Five rules keep it that way:

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
4. **The app-server daemon's runtime state is per-seat.** Codex 0.157+ runs a background app-server
   per ``CODEX_HOME`` and keeps its socket, startup lock, pid and log under the names in
   ``DAEMON_RUNTIME``. One daemon serves ONE account's auth, so sharing that state would hand a seat
   another account's server: those names are never linked in from ~/.codex, never promoted out to
   it, and any such link an older build left is removed on sight. Packages stay per-seat too:
   sharing their independently updated current pointer has not been proven safe.
5. **Dead control sockets are healed at launch.** Only ECONNREFUSED/ENOENT plus a missing, invalid,
   dead or reused daemon PID permits cleanup, while a held startup flock vetoes it. Remove the
   in-home socket and startup lock, and only our own dead socket in the daemon temp directory.
   Leave PID/updater/package state alone; uncertain inspection must never disrupt a live daemon.

Promotion MOVES files, so the launcher asks for it only when no supervised codex session is live;
otherwise the heal simply waits for the next launch.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

from . import daemonprocs
from . import paths as P
from .util import atomic_write_text

# Files SQLite creates beside a database. Never symlinked (see rule 1 above).
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# The app-server daemon's runtime and packages (rule 4). Never shared between seats.
DAEMON_RUNTIME = frozenset({"app-server-control", "app-server-daemon", "app-server-startup.lock", "packages"})
# Marks a parked divergent copy. Such entries are inert: never linked, moved, parked or followed.
ORPHAN_MARK = ".orphaned-"

# Where codex 0.157+ binds the app-server daemon's control socket, relative to CODEX_HOME.
SOCKET_REL = "app-server-control/app-server-control.sock"
# macOS/BSD cap a unix-socket path at SUN_LEN bytes INCLUDING the NUL (Linux allows 108). Codex
# resolves CODEX_HOME before building that path, so a short symlink to a long home does not help —
# the home itself must fit, which is why a seat is a hash and its root is two letters. The old
# ~/.account-switcher/codex-homes/<address> layout blew the budget for every real address, and codex
# then failed every launch with "path must be shorter than SUN_LEN".
SUN_LEN = 104
MAX_HOME_LEN = SUN_LEN - 1 - len("/" + SOCKET_REL)   # 60 bytes for the CODEX_HOME path itself


def _safe(email: str) -> str:
    s = "".join(c if c.isalnum() or c in "._+-@" else "_" for c in email)
    # "/" is already replaced, so the result is a single path component — but "." / ".." would still
    # escape (home_dir("..") → the parent dir). Reject those explicitly so the sanitizer is never the
    # weak link if a malformed email ever reaches here.
    return s if s not in ("", ".", "..") else "seat"


def _slug(email: str) -> str:
    """The home's directory name: short (MAX_HOME_LEN), stable, and a single path component.

    Case-folded, like ``codex_jwt_matches``: one login spelled two ways is one seat with one token,
    which is also what the old address-named homes gave us on a case-insensitive filesystem.
    32 bits is a deliberate floor — a collision needs two addresses in the same store to share a
    prefix (~1e-8 for a handful of seats), while every extra character spends the path budget that
    a deep ``$HOME`` needs far more often.
    """
    return hashlib.sha256(email.casefold().encode("utf-8")).hexdigest()[:8]


def home_dir(email: str, root: Path | None = None) -> Path:
    return (root or P.CODEX_HOMES) / _slug(email)


def index_root(root: Path | None = None) -> Path:
    """Where the by-address symlinks live: ``<store>/codex-homes``, the pre-1.0.2 homes root."""
    return (root or P.CODEX_HOMES).parent / P.CODEX_HOMES_LEGACY.name


def by_address(email: str, root: Path | None = None) -> Path:
    """The by-address index entry for a seat — a symlink to its home, and the pre-1.0.2 home path."""
    return index_root(root) / _safe(email)


def auth_path(email: str, root: Path | None = None) -> Path:
    return home_dir(email, root) / "auth.json"


def socket_path(home: Path) -> Path:
    """The daemon control socket codex will bind (and connect to) for this home."""
    return home / SOCKET_REL


def daemon_socket_fits(home: Path) -> bool:
    """Whether ``home`` leaves room for the control socket path inside SUN_LEN.

    Measured on the RESOLVED path, because that is the one codex builds the socket from: a home
    reached through a symlinked ``$HOME`` (a relocated or network account) is longer than it looks,
    and the check has to fail for exactly the launches that fail. False means every interactive
    launch dies with "app server did not become ready … path must be shorter than SUN_LEN" until the
    user passes --no-daemon. Nothing is left for us to shorten at that point — their home directory
    itself is too deep — so this is a check to run (the layout test, and the VERIFY.md step), not a
    condition to handle.
    """
    return len(str(socket_path(home).resolve()).encode("utf-8")) < SUN_LEN


# --- per-home daemon maintenance --------------------------------------------------------------

STARTUP_LOCK = 'app-server-control/app-server-startup.lock'
TMP_DAEMONS = Path('/private/tmp' if sys.platform == 'darwin' else '/tmp') / f'codex-daemon-{os.getuid()}'


def _inside(path: Path, home: Path) -> bool:
    """Refuse escaped runtime/package parents as well as symlink loops."""
    return path.resolve().is_relative_to(home.resolve())


def _connect_errno(path: Path) -> int:
    """Probe the resolved short target: never spend SUN_LEN on the long in-home alias."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.2)
            client.connect(str(path.resolve()))
        return 0
    except OSError as exc:
        return exc.errno or errno.EAGAIN    # timeout is uncertainty, not a dead server


def _daemon_may_live(home: Path) -> bool:
    """Malformed/missing pid records cannot protect a refusing socket; unknown live starts can.

    Compare kernel epoch seconds, not processStartTime's locale-formatted ps string. Microseconds
    tighten the reuse guard when supplied. A live PID with an older record lacking identity stays
    protected: absence of an identity is not positive evidence that the PID was recycled.
    """
    try:
        data = json.loads((home / 'app-server-daemon/daemon.pid').read_text())
        pid = data['pid']
        if type(pid) is not int or pid <= 0:
            return False
    except (FileNotFoundError, ValueError, TypeError, KeyError):
        return False
    except OSError:
        return True                        # unreadable is not the same as missing/unparseable
    if not daemonprocs.alive(pid):
        return False
    state = daemonprocs.process_state(pid)
    if state is not None and state.dead:
        return False  # P_WEXIT/SZOMB can still pass kill(pid, 0), but will never bind again.
    identity = data.get('processIdentity')
    start = daemonprocs.process_start(pid)
    if not isinstance(identity, dict) or start is None:
        return True
    seconds = identity.get('startSeconds')
    micros = identity.get('startMicroseconds')
    return (type(seconds) is not int or
            (seconds == start[0] and (type(micros) is not int or micros == start[1])))


@contextlib.contextmanager
def _try_lock(path: Path, *, create: bool = False):
    """Hold the same inode throughout inspection/cleanup; an unavailable lock means defer.

    O_NOFOLLOW prevents a replaced lock from opening something outside the seat. A missing lock
    needs no creation for read-only diagnostics; maintenance creates one before its second probe.
    """
    fd = None
    acquired = False
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except FileNotFoundError:
            acquired = not create
        except OSError:
            pass
        yield acquired
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _daemon_state(home: Path) -> str:
    link = socket_path(home)
    if not link.exists() and not link.is_symlink():
        if _daemon_may_live(home):
            return 'busy'
        pidfile = home / 'app-server-daemon/daemon.pid'
        return 'stale' if pidfile.exists() or pidfile.is_symlink() else 'absent'
    error = _connect_errno(socket_path(home))
    if error == 0:
        return 'live'
    if error not in (errno.ENOENT, errno.ECONNREFUSED) or _daemon_may_live(home):
        return 'busy'
    link = socket_path(home)
    return 'stale' if link.exists() or link.is_symlink() else 'absent'


def daemon_state(home: Path) -> str:
    """Read-only diagnostic: live/stale/absent/busy; busy also covers uncertain inspection."""
    try:
        if not _inside(home / 'app-server-control', home) or not _inside(home / 'app-server-daemon', home):
            return 'busy'
        with _try_lock(home / STARTUP_LOCK) as acquired:
            return _daemon_state(home) if acquired else 'busy'
    except (OSError, RuntimeError):
        return 'busy'


def _unlink_dead_tmp_socket(target: Path, *, lock: bool = False) -> None:
    """Only the uid-owned socket named by this home is ours to reap, never arbitrary symlinks.

    Orphan cleanup may remove its matching .lock too, but only while holding that lock. Do not
    sweep the global temp directory: a refusing socket there can belong to an unrelated startup.
    """
    try:
        if target.parent != TMP_DAEMONS.resolve() or target.is_symlink():
            return
        info = target.lstat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            return
        if _connect_errno(target) not in (errno.ENOENT, errno.ECONNREFUSED):
            return
        if lock:
            lockfile = target.with_name(target.name + '.lock')
            with _try_lock(lockfile) as acquired:
                if not acquired:
                    return
                _unlink_dead_tmp_socket(target)
                if not target.exists() and lockfile.lstat().st_uid == os.getuid():
                    if stat.S_ISREG(lockfile.lstat().st_mode):
                        lockfile.unlink()
        elif target.lstat() == info:
            target.unlink()
    except (OSError, RuntimeError):
        pass


def heal_daemon_socket(home: Path) -> str:
    """Best-effort launch repair: absent/live/healed/busy (rule 5).

    ECONNREFUSED/ENOENT alone is not enough: a matching live daemon or a startup lock holder may be
    about to bind. Exiting/zombie records count as dead. Re-probe with the startup flock held,
    archive daemon.pid, then remove the socket and startup lock. Unknown errno/identity defers.
    """
    try:
        before = daemon_state(home)
        if before != 'stale':
            return before
        lock = home / STARTUP_LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        with _try_lock(lock, create=True) as acquired:
            if not acquired:
                return 'busy'
            state = _daemon_state(home)
            if state != 'stale':
                return state
            link = socket_path(home)
            target = link.resolve()
            # Direct socket files are possible too; never unlink a regular file at this path.
            if link.exists() and not link.is_symlink() and not stat.S_ISSOCK(link.lstat().st_mode):
                return 'busy'
            # Codex trusts a matching live PID even if the kernel says it is stuck exiting.
            # Keep the original record for diagnosis, but stop feeding it back to stock startup.
            pidfile = home / 'app-server-daemon/daemon.pid'
            with contextlib.suppress(FileNotFoundError):
                pidfile.rename(pidfile.with_name(f'daemon.pid.stale-{time.time_ns()}'))
            _unlink_dead_tmp_socket(target)
            with contextlib.suppress(FileNotFoundError):
                link.unlink()
            with contextlib.suppress(OSError):
                lock.unlink()
            return 'healed'
    except (OSError, RuntimeError):
        return 'busy'                       # the launcher owns the TTY; no maintenance stdout


def _orphan_home(proc: daemonprocs.Process, root: Path, ids: set[str]) -> Path | None:
    """Classify the ORIGINAL argv[0], normalising dots but never following the address index."""
    if proc.uid != os.getuid() or not proc.argv or not Path(proc.argv[0]).is_absolute():
        return None
    path = Path(os.path.normpath(proc.argv[0]))
    # Canonicalise only the store root (e.g. /tmp -> /private/tmp), never the legacy index itself.
    for base, legacy in ((index_root(root), True), (root, False)):
        for spelling in (base.absolute(), base.parent.resolve() / base.name):
            if not path.is_relative_to(spelling):
                continue
            parts = path.relative_to(spelling).parts
            if len(parts) >= 4 and parts[1:3] == ('packages', 'app-server-daemon'):
                if legacy or parts[0] not in ids:
                    return spelling / parts[0]
    return None


def _seat_ids(root: Path) -> set[str]:
    # Directory existence is not seat membership: removed seats can leave homes behind. Refuse
    # malformed state rather than letting State.load's recovery defaults classify every seat dead.
    statefile = root.parent / 'state.json'
    if statefile.exists():
        data = json.loads(statefile.read_text())
        accounts = data['tools']['codex']['accounts']
        if not isinstance(accounts, dict):
            raise ValueError('invalid seat registry')
        return {_slug(email) for email in accounts}
    return set()


def orphan_daemons(root: Path | None = None, *, list_procs=daemonprocs.list_processes):
    """A failed snapshot/registry read returns no candidates, never a wider kill set."""
    root = root or P.CODEX_HOMES
    try:
        ids = _seat_ids(root)
        return [(proc, home) for proc in list_procs()
                if (home := _orphan_home(proc, root, ids)) is not None]
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return []


def reap_orphan_daemons(root: Path | None = None, *, list_procs=daemonprocs.list_processes,
                        kill=os.kill, sleep=time.sleep) -> list[int]:
    """TERM orphan executables, give them a grace period, then KILL verified survivors.

    Re-enumerate before EACH signal: a recycled PID must never inherit an old kill decision. The
    live-seat registry is also read again, since adding a seat while maintenance waits is legal.
    Darwin has no pidfd: start-time identity and a recheck right before the signal mitigate,
    but cannot eliminate, the residual PID-reuse TOCTOU between identity recheck and kill.
    Return only PIDs observed gone; signalling is not proof of exit. No process names/banners.
    """
    candidates = orphan_daemons(root, list_procs=list_procs)
    if not candidates:
        return []
    targets = {}
    for proc, home in candidates:
        with contextlib.suppress(OSError, RuntimeError):
            targets[proc.pid] = socket_path(home).resolve()

    def current():
        return {p.pid: p for p, _ in orphan_daemons(root, list_procs=list_procs)}

    signalled = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for proc, _ in candidates:
            fresh = current().get(proc.pid)
            if (fresh is not None and
                    (fresh.uid, fresh.start, fresh.argv) == (proc.uid, proc.start, proc.argv)):
                try:
                    kill(proc.pid, sig)
                    if proc not in signalled:
                        signalled.append(proc)
                except OSError:
                    pass
        if signalled:
            sleep(0.5 if sig == signal.SIGTERM else 0.1)
    try:
        remaining = {p.pid: p for p in list_procs()}
    except OSError:
        return []
    reaped = [p.pid for p in signalled if p.pid not in remaining or remaining[p.pid].start != p.start]
    for pid in reaped:
        if pid in targets:
            _unlink_dead_tmp_socket(targets[pid], lock=True)
    return reaped


def _tree_bytes(path: Path) -> int:
    """Logical regular-file bytes, without following symlinks (including the root)."""
    if path.is_symlink() or not path.is_dir():
        return 0
    total = 0
    for base, _, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                info = (Path(base) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def _gc_process_issue(processes) -> str | None:
    unknown = sum(p.uid == os.getuid() and not p.dead and
                  p.executable is None and not p.argv for p in processes)
    if unknown:
        return f'process-details-unavailable ({unknown} live records lack executable and argv)'
    return None


def gc_daemon_releases(home: Path, *, list_procs=daemonprocs.list_processes,
                       on_skip=None) -> int:
    """Keep current and all executing releases; a missing current/snapshot/lock defers GC.

    Only explicit maintenance calls this, never ensure_home or polling. Callers must additionally
    exclude live supervised sessions. Symlinks within a discarded tree are unlinked by rmtree,
    never traversed; a release symlink or an escaped package/current parent aborts the whole pass.
    Either executable or argv[0] pins a release. Live owned records missing both defer GC;
    on_skip, if supplied, receives that reason from the fresh snapshot under the install lock.
    """
    freed = 0
    try:
        package = home / 'packages/app-server-daemon'
        releases = package / 'releases'
        if not _inside(releases, home) or not releases.is_dir():
            return 0
        with _try_lock(package / 'install.lock', create=True) as acquired:
            if not acquired:
                return 0
            current = (package / 'current').resolve(strict=True)
            if (current == releases.resolve() or not current.is_relative_to(releases.resolve())
                    or not current.is_dir()):
                return 0
            entries = list(releases.iterdir())
            if any(p.is_symlink() or not _inside(p, home) for p in entries):
                return 0
            processes = list_procs()
            if reason := _gc_process_issue(processes):
                if on_skip is not None:
                    on_skip(reason)
                return 0
            executing = [p.executable.resolve() for p in processes
                         if not p.dead and p.uid == os.getuid() and p.executable is not None]
            # argv may retain a release's pre-migration spelling; resolved argv supplements the
            # kernel executable path, and keeping extra versions is always the safe direction.
            executing += [Path(p.argv[0]).resolve() for p in processes
                          if not p.dead and p.uid == os.getuid() and p.argv and
                          Path(p.argv[0]).is_absolute()]
            for entry in entries:
                resolved = entry.resolve()
                if (not entry.is_dir() or current.is_relative_to(resolved) or
                        any(p.is_relative_to(resolved) for p in executing)):
                    continue
                size = _tree_bytes(entry)
                shutil.rmtree(entry)
                freed += size
    except (OSError, RuntimeError):
        pass
    return freed


def daemon_report(ctx, *, fix: bool = False) -> dict:
    """One maintenance boundary for CLI and app startup; all live terminals veto release GC.

    The store lock serialises the snapshot with seat changes/launcher activation. Reaper sleeps
    only on explicit maintenance, off the UI thread. Defer GC for ALL seats if any supervisor is
    live, matching the launcher's conservative promote=True gate and covering concurrent terminals.
    The same gate defers orphan reaping: an app restart must not disrupt an older live supervisor.
    """
    from .session import active_session
    # Old builds can leave a supervisor holding a PTY after its terminal vanished. This rescue
    # has its own strict kernel predicates and must run even if the stale session marker is live.
    wedges, rescued = daemonprocs.wedged_supervisors(fix=fix)
    with ctx.locked():
        seats = list(ctx.load_state().accounts('codex'))
        try:
            snapshot = daemonprocs.list_processes()
        except OSError:
            snapshot = None
        incomplete = sum(p.incomplete for p in snapshot or [])
        inspection = ('unavailable' if snapshot is None else
                      f'partial ({incomplete} records incomplete)' if incomplete else 'ok')
        gc_issue = _gc_process_issue(snapshot) if snapshot is not None else 'processes-unavailable'
        orphans = [p.pid for p, _ in orphan_daemons(
            ctx._homes_root, list_procs=lambda: snapshot or [])]
        busy = active_session(ctx.data_dir, 'codex') is not None if fix else False
        reaper = ('read-only' if not fix else 'session-live' if busy else
                  'processes-unavailable' if snapshot is None else 'checked')
        reaped = reap_orphan_daemons(ctx._homes_root) if reaper == 'checked' else []
        rows = []
        for email in seats:
            home = home_dir(email, ctx._homes_root)
            row = {'address': email, 'home_id': home.name, 'state': daemon_state(home)}
            if fix:
                row['heal'] = heal_daemon_socket(home)
                busy = active_session(ctx.data_dir, 'codex') is not None
                row['gc'] = 'session-live' if busy else gc_issue or 'checked'
                row['bytes_freed'] = (gc_daemon_releases(
                    home, on_skip=lambda reason: row.update(gc=reason))
                    if row['gc'] == 'checked' else 0)
                row['state'] = daemon_state(home)
            row['packages_bytes'] = _tree_bytes(home / 'packages')
            rows.append(row)
        return {'seats': rows, 'orphan_pids': orphans, 'reaped_pids': reaped,
                'wedged_supervisors': wedges, 'signalled_supervisors': rescued,
                'reaper': reaper, 'process_inspection': inspection}


def _is_sidecar(name: str) -> bool:
    return name.endswith(SQLITE_SIDECARS)


def _never_shared(name: str) -> bool:
    """Names that must stay whatever the home itself made them: never linked in, never promoted out."""
    return name in DAEMON_RUNTIME


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
        if name == "auth.json" or ORPHAN_MARK in name or _never_shared(name):
            continue                      # per-account token / inert parked copy / this seat's daemon
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


def _adopt_legacy(email: str, root: Path | None = None) -> None:
    """Move a pre-1.0.2 ``codex-homes/<address>`` home onto its short path (see MAX_HOME_LEN).

    A plain rename inside the store, so the seat keeps its auth.json, its parked copies and its
    permissions. Best-effort and idempotent: if the short home already exists the old directory is
    left untouched for the user to inspect, and a seat that cannot be moved simply re-authenticates.

    Deliberately ``os.replace`` and not ``_relocate``: both paths are in the same store, so there is
    no filesystem to cross, and ``shutil.move`` onto a destination another process just created
    would move the home INSIDE it (``ch/<id>/<address>/auth.json``) — which the next promoting heal
    would read as an ordinary shared directory and hand to ~/.codex. A lost race must be a no-op.
    """
    old, new = by_address(email, root), home_dir(email, root)
    if old.is_symlink() or not old.is_dir() or new.exists() or new.is_symlink():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(new.parent, 0o700)
    try:
        os.replace(old, new)
    except OSError:
        return
    # The address must never be left pointing at nothing: a codex child started before the move
    # still has the old path, and an older engine knows no other name for this seat.
    _index_by_address(email, root, new)


def _index_by_address(email: str, root: Path | None = None, home: Path | None = None) -> None:
    """Keep ``codex-homes/<address>`` pointing at the seat's home.

    The home is named by hash, so this symlink is how a seat stays findable by address — for a human
    reading the store, for the docs, and for a codex child spawned before ``_adopt_legacy`` moved it.
    Relative, so moving or copying the whole store keeps it valid. Published by an atomic rename, so
    a reader racing a repair sees the old link or the new one, never a missing address.
    """
    home = home or home_dir(email, root)
    link = by_address(email, root)
    target = os.path.relpath(home, link.parent)
    if link.is_symlink() and os.readlink(link) == target:
        return
    if link.exists() and not link.is_symlink():
        return          # a real home _adopt_legacy could not move — never clobber someone's auth
    link.parent.mkdir(parents=True, exist_ok=True)
    staged = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    try:
        staged.symlink_to(target)
        os.replace(staged, link)
    except OSError:
        with contextlib.suppress(OSError):
            staged.unlink()


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
    _adopt_legacy(email, root)
    home = home_dir(email, root)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    _index_by_address(email, root, home)
    for entry in list(home.iterdir()):   # materialised: we unlink while walking
        if entry.is_symlink() and ORPHAN_MARK not in entry.name and (
                _is_sidecar(entry.name) or _never_shared(entry.name)):
            entry.unlink()                # rule 1: SQLite never needs these, and they break it
                                          # rule 4: a shared daemon would serve the wrong account
    if promote:
        _heal(home, real)
    if real.exists():
        for entry in real.iterdir():
            if entry.name == "auth.json":
                continue  # auth is per-account (a real file in the home)
            if _is_sidecar(entry.name):
                continue  # rule 1: SQLite finds the sidecars next to the resolved real database
            if _never_shared(entry.name):
                continue  # rule 4: this seat's app-server daemon is its own, socket and all
            link = home / entry.name
            if link.is_symlink():
                if link.resolve() != entry.resolve():
                    link.unlink(); link.symlink_to(entry)
            elif not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    heal_daemon_socket(home)
    return home


def save(email: str, blob: str, *, codex_home: Path | None = None, root: Path | None = None) -> None:
    ensure_home(email, codex_home=codex_home, root=root)
    atomic_write_text(auth_path(email, root), blob, mode=0o600)


def load(email: str, *, root: Path | None = None) -> str | None:
    """The seat's token, read wherever it currently lies — including a home still on the pre-1.0.2
    path, which is a seat with a token and not a seat without one.

    Reads never move anything. Usage polling calls this from outside the state lock and on a timer,
    so adopting here would rename a directory out from under a codex child that is standing in it,
    at a moment no one chose. ``ensure_home`` — which every launch and every save goes through —
    owns the move.
    """
    for path in (auth_path(email, root), by_address(email, root) / "auth.json"):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue     # missing, or the address is a real file / dangling link: try the next one
    return None


def _remove(path: Path) -> bool:
    if path.is_symlink():
        path.unlink()   # a replaced home root / the by-address index: never traverse into the target
        return True
    if not path.exists():
        return False
    # rmtree unlinks symlinked entries instead of following them (so ~/.codex is untouched) while
    # still removing the real auth.json, parked .orphaned-* copies and any real dir a child left.
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def delete(email: str, *, root: Path | None = None) -> bool:
    # The index entry goes first: dropping the home behind a live symlink would leave the address
    # pointing at nothing, and a later ensure_home would have to guess whether it was ours.
    home = home_dir(email, root)
    indexed = _remove(by_address(email, root))
    removed = _remove(home)
    # A dropped address can stand for a seat that had no home left, but never for a home that
    # survived: reporting a seat erased while its token is still on disk is the one wrong answer.
    return removed or (indexed and not home.exists())
