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

INVARIANT: a home holds exactly ONE real file, ``auth.json`` — plus the daemon's own runtime state
(rule 4). Everything else is shared state owned by the real ~/.codex and appears here only as a
symlink. Four rules keep it that way:

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
   it, and any such link an older build left is removed on sight.

Promotion MOVES files, so the launcher asks for it only when no supervised codex session is live;
otherwise the heal simply waits for the next launch.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import paths as P
from .util import atomic_write_text

# Files SQLite creates beside a database. Never symlinked (see rule 1 above).
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# The app-server daemon's per-home runtime state (see rule 4 above). Never shared between seats.
DAEMON_RUNTIME = frozenset({"app-server-control", "app-server-daemon", "app-server-startup.lock"})
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
    """The home's directory name: short (MAX_HOME_LEN), stable, and a single path component."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:8]


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

    False means codex still runs, but only as `codex --no-daemon` would: its background app-server
    can neither bind nor be reached, and codex says so on every launch. Nothing is left for us to
    shorten at that point — the user's own home directory is too deep — so this is a check to run
    (the layout test, and the VERIFY.md step), not a condition to handle.
    """
    return len(str(socket_path(home)).encode("utf-8")) < SUN_LEN


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
    """
    old, new = by_address(email, root), home_dir(email, root)
    if old.is_symlink() or not old.is_dir() or new.exists() or new.is_symlink():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(new.parent, 0o700)
    with contextlib.suppress(OSError):
        _relocate(old, new)


def _index_by_address(email: str, root: Path | None = None, home: Path | None = None) -> None:
    """Keep ``codex-homes/<address>`` pointing at the seat's home.

    The home is named by hash, so this symlink is how a seat stays findable by address — for a human
    reading the store, for the docs, and for a codex child spawned before ``_adopt_legacy`` moved it.
    Relative, so moving or copying the whole store keeps it valid.
    """
    home = home or home_dir(email, root)
    link = by_address(email, root)
    target = os.path.relpath(home, link.parent)
    if link.is_symlink():
        if os.readlink(link) == target:
            return
        link.unlink()
    elif link.exists():
        return          # a real home _adopt_legacy could not move — never clobber someone's auth
    link.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        link.symlink_to(target)


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
    return home


def save(email: str, blob: str, *, codex_home: Path | None = None, root: Path | None = None) -> None:
    ensure_home(email, codex_home=codex_home, root=root)
    atomic_write_text(auth_path(email, root), blob, mode=0o600)


def load(email: str, *, root: Path | None = None) -> str | None:
    _adopt_legacy(email, root)   # a seat still on the pre-1.0.2 path is not a seat without a token
    try:
        return auth_path(email, root).read_text(encoding="utf-8")
    except FileNotFoundError:
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
    indexed = _remove(by_address(email, root))
    return _remove(home_dir(email, root)) or indexed
