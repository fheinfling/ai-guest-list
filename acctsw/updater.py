"""In-app updater engine: check GitHub's latest release, verify a download, hand off the swap.

Checking is automatic and cheap (one small JSON file, at most every ``CHECK_INTERVAL_S``);
downloading and installing happen only when the user clicks. The contract with the release
pipeline lives in ``docs/RELEASING.md`` and ``scripts/update-manifest.py``; this module reads the
``latest.json`` that script writes and validates it just as strictly.

Trust: the manifest's SHA-256 only proves the zip is the one the manifest names. The publisher is
authenticated separately, by ``codesign`` against the designated requirement shipped in the app
(``scripts/update-requirement.txt``) and by Gatekeeper (``spctl``). Every check fails closed.

Stdlib only, no PyObjC: network, subprocess and clock are injectable so all of it is unit-tested.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

from . import __version__
from .engine import parse_version
from .procenv import harden_env
from .util import atomic_write_text, iso, now, parse_iso, write_json

REPO_URL = "https://github.com/fheinfling/ai-guest-list"
BUNDLE_ID = "com.fheinfling.aiguestlist"
APP_NAME = "AI Guest List.app"
# ``/releases/latest/`` resolves to the newest *published* release only, so drafts are never seen.
MANIFEST_URL = REPO_URL + "/releases/latest/download/latest.json"
CHECK_INTERVAL_S = 20 * 3600
STATUS_FILE = "update.json"

# Mirrors scripts/update-manifest.py.
SCHEMA = 1
MAX_ZIP_SIZE = 300_000_000  # strictly less than this
MAX_MANIFEST_SIZE = 16 * 1024
KEYS = frozenset({"schema", "version", "build", "published_at", "url", "sha256", "size",
                  "minimum_system_version", "notes_url"})

# github.com answers release downloads with a 302 to a signed URL on
# release-assets.githubusercontent.com (observed 2026-11); objects.githubusercontent.com is the
# host it used before and may still serve older assets. Nothing else is accepted, at any hop.
ALLOWED_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                           "objects.githubusercontent.com"})

PHASES = ("available", "downloading", "verifying", "installing", "failed")

_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_BUILD_RE = re.compile(r"[0-9]+")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_MACOS_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}")
_PUBLISHED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

_TIMEOUT_S = 30
_TOOL_TIMEOUT_S = 600
_CHUNK = 1024 * 1024
_LOCK = threading.RLock()  # serializes read-modify-write of update.json within this process


class UpdateError(Exception):
    """A short, user-facing reason an update can't proceed."""


# --- network ------------------------------------------------------------------------------------

def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UpdateError("refused an invalid download address") from None
    if (parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS
            or port not in (None, 443) or parts.username or parts.password):
        raise UpdateError("refused a download from an unexpected address")


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": f"AI-Guest-List/{__version__}"})
    response = urllib.request.build_opener(_AllowlistRedirects()).open(req, timeout=_TIMEOUT_S)
    try:
        _check_url(response.geturl())
    except BaseException:
        response.close()
        raise
    return response


def _default_fetch(url: str, max_bytes: int) -> bytes:
    """GET ``url`` (https, allowlisted hosts at every hop) and return at most ``max_bytes``."""
    with _open(url) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise UpdateError("update info is too large")
    return body


def _default_fetch_stream(url: str, dest: Path, max_bytes: int) -> None:
    """Stream ``url`` into ``dest``, aborting as soon as more than ``max_bytes`` arrive."""
    total = 0
    with _open(url) as response, open(dest, "wb") as out:
        while True:
            block = response.read(_CHUNK)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise UpdateError("download is larger than announced")
            out.write(block)


def _system_version() -> str:
    return platform.mac_ver()[0]


# --- manifest -----------------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UpdateError(message)


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise UpdateError("duplicate key")
    return dict(pairs)


def _reject_constant(name):
    raise UpdateError(f"non-standard number {name}")


def parse_manifest(body: bytes) -> dict:
    """Decode and validate ``latest.json`` bytes; raise UpdateError naming the broken rule."""
    _require(isinstance(body, (bytes, bytearray)), "manifest must be bytes")
    _require(len(body) <= MAX_MANIFEST_SIZE, "manifest too large")
    try:
        manifest = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_no_duplicates,
                              parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(f"manifest is not valid JSON: {exc}") from None
    return validate_manifest(manifest)


def validate_manifest(manifest) -> dict:
    """The reader's half of scripts/update-manifest.py's validate_manifest."""
    _require(isinstance(manifest, dict), "manifest must be an object")
    _require(set(manifest) == KEYS, "manifest keys don't match the contract")
    _require(type(manifest["schema"]) is int and manifest["schema"] == SCHEMA,
             "unsupported manifest schema")
    version = manifest["version"]
    _require(isinstance(version, str) and _VERSION_RE.fullmatch(version) is not None,
             "invalid version")
    tag = f"v{version}"
    build = manifest["build"]
    _require(isinstance(build, str) and _BUILD_RE.fullmatch(build) is not None, "invalid build")
    _require(manifest["url"] == f"{REPO_URL}/releases/download/{tag}/AI-Guest-List-{tag}.zip",
             "invalid download url")
    _require(manifest["notes_url"] == f"{REPO_URL}/releases/tag/{tag}", "invalid notes_url")
    sha = manifest["sha256"]
    _require(isinstance(sha, str) and _SHA_RE.fullmatch(sha) is not None, "invalid sha256")
    size = manifest["size"]
    _require(type(size) is int and 0 < size < MAX_ZIP_SIZE, "invalid size")
    minimum = manifest["minimum_system_version"]
    _require(isinstance(minimum, str) and _MACOS_RE.fullmatch(minimum) is not None,
             "invalid minimum_system_version")
    published = manifest["published_at"]
    _require(isinstance(published, str) and _PUBLISHED_RE.fullmatch(published) is not None,
             "invalid published_at")
    try:
        datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise UpdateError("invalid published_at") from None
    return dict(manifest)


def _newer(version, current) -> bool:
    mine, theirs = parse_version(current), parse_version(version)
    return mine is not None and theirs is not None and theirs > mine


def _supported(minimum: str, system: str) -> bool:
    """Fail closed: an unknown running macOS supports nothing."""
    need, have = parse_version(minimum), parse_version(system)
    return need is not None and have is not None and need <= have


# --- status file --------------------------------------------------------------------------------

def _path(data_dir) -> Path:
    return Path(data_dir) / STATUS_FILE


def _load(data_dir) -> dict:
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stored_manifest(data: dict) -> dict | None:
    try:
        return validate_manifest(data.get("manifest"))
    except UpdateError:
        return None


def reconcile(data_dir) -> None:
    """At launch, nothing is installing: a busy phase left behind (the app quit mid-download, or a
    helper put the previous app back) would keep the popover's update button disabled for good."""
    with _LOCK:
        data = _load(data_dir)
        if isinstance(data, dict) and data.get("phase") in ("downloading", "verifying", "installing"):
            set_phase(data_dir, "failed", "the last update didn't finish")


def set_phase(data_dir, phase: str, error: str | None = None) -> None:
    """Record the install phase (and, for ``failed``, a short user-facing error)."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    with _LOCK:
        data = _load(data_dir)
        data["phase"] = phase
        data["error"] = error if phase == "failed" else None
        write_json(_path(data_dir), data)


def _set_phase_for(data_dir, version: str, phase: str, error: str | None = None) -> None:
    """set_phase, unless a newer check has replaced the update this phase belongs to."""
    with _LOCK:
        stored = _stored_manifest(_load(data_dir))
        if stored is None or stored["version"] == version:
            set_phase(data_dir, phase, error)


def status(data_dir, *, current: str = __version__) -> dict | None:
    """What the popover shows: the known update newer than ``current``, or None."""
    data = _load(data_dir)
    manifest = _stored_manifest(data)
    if manifest is None or not _newer(manifest["version"], current):
        return None
    phase = data.get("phase")
    error = data.get("error")
    return {
        "current": current,
        "version": manifest["version"],
        "notes_url": manifest["notes_url"],
        "phase": phase if phase in PHASES else "available",
        "error": error if phase == "failed" and isinstance(error, str) else None,
        "checked_at": data.get("checked_at") if isinstance(data.get("checked_at"), str) else None,
    }


# --- check --------------------------------------------------------------------------------------

def check(data_dir, *, force: bool = False,
          fetch: Callable[[str, int], bytes] = _default_fetch,
          current: str = __version__,
          system_version: str | Callable[[], str] = _system_version,
          at: datetime | None = None) -> dict:
    """Look for a newer published release (throttled to CHECK_INTERVAL_S unless ``force``).

    Returns ``{"ok", "checked", "up_to_date", "version", "error"}``. A failed check records a
    short error but never forgets an update already known to be available.
    """
    at = at or now()
    with _LOCK:
        data = _load(data_dir)
    known = _stored_manifest(data)
    known_version = known["version"] if known and _newer(known["version"], current) else None

    last = parse_iso(data.get("checked_at")) if isinstance(data.get("checked_at"), str) else None
    if (not force and last is not None
            and timedelta(0) <= at - last < timedelta(seconds=CHECK_INTERVAL_S)):
        return {"ok": True, "checked": False, "up_to_date": known_version is None,
                "version": known_version, "error": None}

    error = None
    manifest = None
    try:
        manifest = parse_manifest(fetch(MANIFEST_URL, MAX_MANIFEST_SIZE))
    except UpdateError as exc:
        error = _short_check_error(exc)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        error = "couldn't reach GitHub"

    system = system_version() if callable(system_version) else system_version
    with _LOCK:
        data = _load(data_dir)  # re-read: a prepare() may have moved the phase meanwhile
        data["checked_at"] = iso(at)
        if manifest is None:
            data["check_error"] = error
            write_json(_path(data_dir), data)
            known = _stored_manifest(data)
            kv = known["version"] if known and _newer(known["version"], current) else None
            return {"ok": False, "checked": True, "up_to_date": kv is None, "version": kv,
                    "error": error}
        data["check_error"] = None
        available = (_newer(manifest["version"], current)
                     and _supported(manifest["minimum_system_version"], system))
        if available:
            stored = _stored_manifest(data)
            if stored is None or stored != manifest:
                data["manifest"] = manifest
                if stored is None or stored["version"] != manifest["version"]:
                    data["phase"] = "available"
                    data["error"] = None
            data.setdefault("phase", "available")
        else:
            for key in ("manifest", "phase", "error"):
                data.pop(key, None)
        write_json(_path(data_dir), data)
    return {"ok": True, "checked": True, "up_to_date": not available,
            "version": manifest["version"] if available else None, "error": None}


def _short_check_error(exc: UpdateError) -> str:
    text = str(exc)
    if text.startswith("refused") or text == "update info is too large":
        return text
    return f"invalid update info ({text})"


# --- prepare ------------------------------------------------------------------------------------

def load_requirement(candidates) -> str:
    """The designated requirement text from the first readable, non-empty candidate file."""
    for candidate in candidates:
        try:
            text = Path(candidate).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text
    raise UpdateError("the app's update signature requirement is missing")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(_CHUNK), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size




MAX_ZIP_ENTRIES = 20_000
MAX_UNPACKED_SIZE = 1_000_000_000
_SPACE_MARGIN = 32 * 1024 * 1024


def _inspect_zip(zip_path: Path) -> int:
    """Member names (same rule as release-bundle.py check-zip), entry count and unpacked size,
    all before ditto runs. Returns the total uncompressed size the zip claims."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise UpdateError("the download isn't a valid zip") from None
    _require(bool(infos), "the download is empty")
    _require(len(infos) <= MAX_ZIP_ENTRIES, "the download has too many files")
    total = 0
    for info in infos:
        name = info.filename
        parts = PurePosixPath(name).parts
        _require(bool(parts) and parts[0] == APP_NAME and ".." not in parts
                 and "\\" not in name and not name.startswith("/"),
                 "the download has an unexpected layout")
        total += info.file_size
        _require(total <= MAX_UNPACKED_SIZE, "the download unpacks too large")
    return total


def _check_space(path: Path, needed: int, where: str) -> None:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        raise UpdateError(f"couldn't check free space {where}") from None
    _require(free >= needed + _SPACE_MARGIN, f"not enough free disk space {where}")


def _check_workdir(workdir: Path) -> None:
    """Staging must happen in a directory only we can write: ours, not group/world writable."""
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(workdir)
    _require(stat.S_ISDIR(st.st_mode), "the update folder isn't a plain folder")
    _require(st.st_uid == os.getuid(), "the update folder belongs to another user")
    _require(not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH),
             "the update folder is writable by other users")


def _check_layout(extract: Path) -> Path:
    """Exactly one top-level real directory named APP_NAME; no link or special file escapes it."""
    entries = os.listdir(extract)
    _require(entries == [APP_NAME], "the download has an unexpected layout")
    app = extract / APP_NAME
    _require(app.is_dir() and not app.is_symlink(), "the download has an unexpected layout")
    root = os.path.realpath(app)
    for dirpath, dirnames, filenames in os.walk(app):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                target = os.path.realpath(path)
                _require(target == root or target.startswith(root + os.sep),
                         "the download contains a link outside the app")
            else:
                _require(stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode),
                         "the download contains an unexpected file")
    return app


def _run(run, argv: list[str]) -> bool:
    try:
        result = run(argv, capture_output=True, text=True, timeout=_TOOL_TIMEOUT_S,
                     env=harden_env(), check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def _read_info(app: Path) -> dict:
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        raise UpdateError("the update's Info.plist is unreadable") from None
    _require(isinstance(info, dict), "the update's Info.plist is unreadable")
    return info


def verify_app(app: Path, *, requirement: str, version: str, build: str | None = None,
               minimum_system_version: str | None = None,
               system_version: str | Callable[[], str] | None = None,
               run=subprocess.run) -> None:
    """Publisher (codesign -R), Gatekeeper (spctl) and Info.plist-vs-manifest checks; fail closed.

    ``build``/``minimum_system_version`` (when given) must equal the bundle's CFBundleVersion and
    LSMinimumSystemVersion; ``system_version`` (when given) must satisfy that minimum."""
    app = Path(app)
    _require(bool(requirement and requirement.strip()), "the update signature requirement is missing")
    _require(_run(run, ["/usr/bin/codesign", "--verify", "--deep", "--strict",
                        "-R", "=" + requirement, str(app)]),
             "the update isn't signed by the AI Guest List developer")
    _require(_run(run, ["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)]),
             "macOS Gatekeeper rejected the update")
    info = _read_info(app)
    _require(info.get("CFBundleIdentifier") == BUNDLE_ID, "the update is a different app")
    _require(info.get("CFBundleShortVersionString") == version,
             "the update's version doesn't match the release")
    if build is not None:
        _require(info.get("CFBundleVersion") == build,
                 "the update's build doesn't match the release")
    if minimum_system_version is not None:
        _require(info.get("LSMinimumSystemVersion") == minimum_system_version,
                 "the update's minimum macOS doesn't match the release")
    if system_version is not None:
        system = system_version() if callable(system_version) else system_version
        need = minimum_system_version or info.get("LSMinimumSystemVersion")
        _require(isinstance(need, str) and _supported(need, system),
                 f"the update needs macOS {need} or later")


def prepare(data_dir, *, workdir, requirement: str,
            fetch_stream: Callable[[str, Path, int], None] = _default_fetch_stream,
            run=subprocess.run, current: str = __version__,
            target_parent: Path | None = None,
            system_version: str | Callable[[], str] = _system_version) -> Path:
    """Download, check and verify the known update; return the staged ``AI Guest List.app``.

    ``workdir`` must be ours and not group/world writable; staging happens in a fresh 0700 dir
    inside it. ``target_parent`` (the installed app's folder), when given, must have room for the
    candidate copy plus the backup. Phases go downloading → verifying in update.json. Any failure
    sets phase ``failed`` with a short error, removes what was staged and raises UpdateError. On
    success the phase stays ``verifying``; the caller sets ``installing`` when it hands off.
    """
    with _LOCK:
        manifest = _stored_manifest(_load(data_dir))
    if manifest is None or not _newer(manifest["version"], current):
        raise UpdateError("no update is available")
    version = manifest["version"]
    workdir = Path(workdir)
    stage = None
    try:
        system = system_version() if callable(system_version) else system_version
        _require(_supported(manifest["minimum_system_version"], system),
                 f"the update needs macOS {manifest['minimum_system_version']} or later")
        _set_phase_for(data_dir, version, "downloading")
        _check_workdir(workdir)
        _check_space(workdir, manifest["size"], "to download the update")
        stage = Path(tempfile.mkdtemp(prefix="update-", dir=workdir))
        os.chmod(stage, 0o700)
        zip_path = stage / f"AI-Guest-List-v{version}.zip"
        try:
            fetch_stream(manifest["url"], zip_path, manifest["size"])
        except UpdateError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            raise UpdateError("the download failed") from None
        _require(zip_path.is_file(), "the download failed")
        sha, size = _sha256(zip_path)
        _require(size == manifest["size"], "the download has the wrong size")
        _require(sha == manifest["sha256"], "the download is corrupted (checksum mismatch)")

        _set_phase_for(data_dir, version, "verifying")
        unpacked = _inspect_zip(zip_path)
        _check_space(stage, unpacked, "to unpack the update")
        if target_parent is not None:
            _check_space(Path(target_parent), 2 * unpacked, "to install the update")
        extract = stage / "app"
        extract.mkdir(mode=0o700)
        _require(_run(run, ["/usr/bin/ditto", "-x", "-k", str(zip_path), str(extract)]),
                 "couldn't unpack the download")
        zip_path.unlink()
        app = _check_layout(extract)
        verify_app(app, requirement=requirement, version=version, build=manifest["build"],
                   minimum_system_version=manifest["minimum_system_version"],
                   system_version=system, run=run)
        return app
    except UpdateError as exc:
        _fail(data_dir, version, str(exc), stage)
        raise
    except OSError:
        _fail(data_dir, version, "couldn't stage the update", stage)
        raise UpdateError("couldn't stage the update") from None


def _fail(data_dir, version: str, error: str, stage: Path | None) -> None:
    if stage is not None:
        shutil.rmtree(stage, ignore_errors=True)
    try:
        _set_phase_for(data_dir, version, "failed", error)
    except OSError:
        pass


# --- install ------------------------------------------------------------------------------------

ACK_FILE = "update-ack"


def acknowledge(data_dir, current: str = __version__) -> Path:
    """Called by the app at launch: tells a waiting install helper this version started."""
    path = Path(data_dir) / ACK_FILE
    atomic_write_text(path, f"{current}\n")
    return path


def _ere_escape(text: str) -> str:
    """Escape for a POSIX extended regex (pkill/pgrep -f), so the pattern matches only ``text``."""
    return "".join("\\" + c if c in ".[]{}()\\*+?^$|" else c for c in text)


# Runs detached, after the app quits. Every value arrives as a positional arg, never as script
# text. Exit codes: 0 installed and acknowledged; 1 nothing changed (old app reopened); 2 the old
# app never quit; 3 the installed version changed meanwhile; 4 rolled back after the new app never
# acknowledged; 70 a restore failed — the installed app may be missing (backup path is logged).
_INSTALL_SCRIPT = r'''
staged=$1; target=$2; pid=$3; backup=$4; candidate=$5; ack=$6; expected=$7; new=$8
requirement=$9; shift 9
opener=$1; codesign=$2; pkill=$3; pgrep=$4; quit_ticks=$5; ack_ticks=$6; pattern=$7
log() { printf 'ai-guest-list-update: %s\n' "$*" >&2; }
version_of() { /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$1/Contents/Info.plist" 2>/dev/null; }
reopen() { "$opener" "$target" || log "couldn't reopen $target"; }
cleanup() { /bin/rm -rf -- "$staged"; /bin/rmdir -- "${staged%/*}" "${staged%/*/*}" 2>/dev/null; }
give_up() { /bin/rm -rf -- "$candidate"; cleanup; reopen; exit "${1:-1}"; }

/bin/rm -f -- "$ack"
i=0
while /bin/kill -0 "$pid" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge "$quit_ticks" ]; then log "the app didn't quit; nothing changed"; exit 2; fi
  /bin/sleep 0.5
done

installed=$(version_of "$target")
if [ "$installed" != "$expected" ]; then
  log "installed version is '$installed', expected '$expected'; nothing changed"
  cleanup; reopen; exit 3
fi

/bin/rm -rf -- "$candidate"
if ! /usr/bin/ditto "$staged" "$candidate"; then log "copy failed"; give_up 1; fi
if ! "$codesign" --verify --deep --strict -R "=$requirement" "$candidate"; then
  log "the copied app failed signature verification"; give_up 1
fi
if [ "$(version_of "$candidate")" != "$new" ]; then log "the copied app has the wrong version"; give_up 1; fi

if ! /bin/mv -- "$target" "$backup"; then log "couldn't move the installed app aside"; give_up 1; fi
# Checked again on what was actually moved aside: the copy above takes a while, and an upgrade
# that landed meanwhile (Homebrew, say) must not be replaced by this older candidate.
moved=$(version_of "$backup")
if [ "$moved" != "$expected" ]; then
  if ! /bin/mv -- "$backup" "$target"; then
    log "RESTORE FAILED: the previous app is at $backup"; exit 70
  fi
  log "installed version became '$moved' during the copy; nothing changed"
  /bin/rm -rf -- "$candidate"; cleanup; reopen; exit 3
fi
if ! /bin/mv -- "$candidate" "$target"; then
  if ! /bin/mv -- "$backup" "$target"; then
    log "RESTORE FAILED: the previous app is at $backup"; exit 70
  fi
  log "couldn't move the new app into place; restored"; give_up 1
fi

if "$opener" "$target"; then
  i=0
  while [ "$i" -lt "$ack_ticks" ]; do
    if [ "$(/bin/cat -- "$ack" 2>/dev/null)" = "$new" ]; then
      /bin/rm -rf -- "$backup"; cleanup; exit 0
    fi
    i=$((i + 1))
    /bin/sleep 0.5
  done
  log "the new app didn't confirm it started; rolling back"
else
  log "couldn't open the new app; rolling back"
fi

"$pkill" -f "$pattern"
i=0
while "$pgrep" -f "$pattern" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 20 ]; then "$pkill" -9 -f "$pattern"; break; fi
  /bin/sleep 0.5
done
/bin/sleep 0.5
if ! /bin/mv -- "$target" "$candidate"; then
  log "RESTORE FAILED: couldn't move the new app aside; the previous app is at $backup"; exit 70
fi
if ! /bin/mv -- "$backup" "$target"; then
  log "RESTORE FAILED: the previous app is at $backup"; exit 70
fi
/bin/rm -rf -- "$candidate"
cleanup
reopen
exit 4
'''


def install_command(staged: Path, target: Path, pid: int, backup: Path, *,
                    ack: Path, expected_current: str, requirement: str,
                    opener: str = "/usr/bin/open", codesign: str = "/usr/bin/codesign",
                    pkill: str = "/usr/bin/pkill", pgrep: str = "/usr/bin/pgrep",
                    quit_wait_s: float = 60, ack_wait_s: float = 120) -> list[str]:
    """argv for the detached helper that swaps ``target`` for ``staged`` once ``pid`` has exited.

    Transactional: the helper copies ``staged`` to a hidden candidate next to ``target``, verifies
    the candidate (codesign against ``requirement`` and its version), and only then renames
    target→backup and candidate→target. It refuses to touch ``target`` unless that still reports
    ``expected_current``. After opening the new app it waits for the ``ack`` file (written by
    :func:`acknowledge`) to name the new version; only then is the backup deleted. Otherwise it
    stops the new app, puts the backup back and reopens the old one. ``opener``, ``codesign``,
    ``pkill``, ``pgrep`` and the waits exist for tests.
    """
    staged, target, backup, ack = Path(staged), Path(target), Path(backup), Path(ack)

    def refuse(why: str):
        raise UpdateError(f"can't update in place: {why}")

    if not all(p.is_absolute() for p in (staged, target, backup, ack)):
        refuse("paths must be absolute")
    if target.suffix != ".app":
        refuse("the app isn't an .app bundle")
    if "/AppTranslocation/" in str(target) or "/AppTranslocation/" in os.path.realpath(target):
        refuse("macOS is running the app from a temporary location; move it to Applications first")
    if not target.is_dir() or target.is_symlink():
        refuse("the installed app wasn't found")
    if not (target.parent.is_dir() and os.access(target.parent, os.W_OK | os.X_OK)):
        refuse(f"no permission to replace the app in {target.parent}")
    if not staged.is_dir() or staged.suffix != ".app":
        refuse("the downloaded update is missing")
    if backup.exists() or backup.is_symlink():
        refuse("a previous update backup is in the way")
    if backup.parent != target.parent:
        refuse("the backup must sit next to the app")
    candidate = target.parent / f".{target.name}.new-{pid}"
    if candidate.exists() or candidate.is_symlink():
        refuse("a previous update copy is in the way")
    if not ack.parent.is_dir():
        refuse("the data folder is missing")
    if type(pid) is not int or pid <= 0:
        refuse("invalid process id")
    if not (isinstance(requirement, str) and requirement.strip()):
        refuse("the update signature requirement is missing")
    if not isinstance(expected_current, str) or parse_version(expected_current) is None:
        refuse("the installed version is unknown")
    try:
        new_version = _read_info(staged).get("CFBundleShortVersionString")
    except UpdateError:
        new_version = None
    if not isinstance(new_version, str) or not _newer(new_version, expected_current):
        refuse("the downloaded update isn't newer than the installed app")
    pattern = "^" + _ere_escape(f"{target}/Contents/MacOS/")
    return ["/bin/sh", "-c", _INSTALL_SCRIPT, "ai-guest-list-update",
            str(staged), str(target), str(pid), str(backup), str(candidate), str(ack),
            expected_current, new_version, requirement,
            opener, codesign, pkill, pgrep,
            str(max(1, round(quit_wait_s * 2))), str(max(1, round(ack_wait_s * 2))), pattern]
