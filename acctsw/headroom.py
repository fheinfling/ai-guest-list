"""Headroom integration — the "save credit" toggle.

When enabled, the supervised launcher routes the agent through Headroom
(https://github.com/headroomlabs-ai/headroom), which compresses what the agent reads → fewer
tokens → usage limits are hit more slowly. Headroom is a pure data-path wrapper: it never touches
credentials or the keychain.

It's installed into THIS app's venv by `acctsw install` (so it "just works" — no separate install),
and we locate it next to the running interpreter even when the venv's bin isn't on PATH.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import paths as P

PINNED_VERSION = "0.27.0"                       # pin: audited build (see docs/SECURITY-headroom.md)
PACKAGE = f"headroom-ai[proxy]=={PINNED_VERSION}"

# Hardening env applied to every wrapped session: telemetry off, no third-party tracing/analytics,
# stay local-only. (All of headroom's cloud features are already opt-in via unset keys; this is
# belt-and-suspenders so nothing can phone home even if a default ever changes.)
HARDENING_ENV = {
    "HEADROOM_TELEMETRY": "off",     # anonymous usage telemetry (already off by default)
    "LITELLM_TELEMETRY": "False",    # disable litellm's own telemetry
    "DO_NOT_TRACK": "1",             # honored by litellm + others
}


def harden_env(env: dict | None = None) -> dict:
    """Return env with hardening flags applied (does not mutate the input)."""
    e = dict(os.environ if env is None else env)
    e.update(HARDENING_ENV)
    return e


def rtk_path() -> Path:
    return Path.home() / ".headroom" / "bin" / "rtk"


def verify_rtk(record_dir: Path | None) -> tuple[bool, str]:
    """TOFU checksum-pin the runtime-downloaded `rtk` binary.

    Records rtk's sha256 on first sight; on later runs, verifies it's unchanged. Returns
    (ok, message). ok=False means rtk changed unexpectedly (possible supply-chain tamper) → caller
    should refuse to run with Headroom until the user re-confirms.
    """
    import hashlib
    from .util import sha256_text
    rtk = rtk_path()
    if not rtk.exists():
        return True, "rtk not present yet (downloaded on first wrap)"
    raw = rtk.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    store = (record_dir or (Path.home() / ".account-switcher")) / "rtk.sha256"
    if store.exists():
        recorded = store.read_text().strip()
        if recorded == digest:
            return True, "rtk checksum verified"
        # An older version recorded sha256 of the hex STRING of the bytes; transparently migrate that
        # to the raw-bytes digest so an upgrade isn't mistaken for a supply-chain tamper.
        if recorded == sha256_text(raw.hex()):
            store.write_text(digest)
            return True, "rtk checksum migrated to new format"
        return False, f"rtk checksum changed ({recorded[:12]}… → {digest[:12]}…)"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(digest)
    return True, f"rtk checksum recorded ({digest[:12]}…)"

# Markers Headroom leaves in the tool config — used to detect a still-injected (dirty) config.
# Deliberately CONFIG-SYNTAX strings (not prose words like "Headroom" or "headroomlabs", which a
# user could legitimately write): a loose substring would treat such a config as still-routed and
# let the restore backstop overwrite their real edits. (Claude's exact settings.json marker is
# confirmed against a live install in M8 — see docs/VERIFY.md; add it here once known.)
INJECT_MARKERS = ('model_provider = "headroom"', "headroom:rtk-instructions")


def _config_dir(tool: str) -> Path:
    return P.CODEX_HOME if tool == "codex" else P.CLAUDE_CONFIG_DIR


def _touched(tool: str) -> list[Path]:
    """Files `headroom wrap <tool>` mutates. (Codex verified live; Claude best-effort superset.)"""
    d = _config_dir(tool)
    if tool == "codex":
        return [d / "config.toml", d / "AGENTS.md"]
    return [d / "CLAUDE.md", d / "settings.json", d / "settings.local.json", d / ".mcp.json"]


def _is_injected(tool: str) -> bool:
    """True if ANY file Headroom touches for this tool still carries an injection marker — not just
    the first (Claude routing lands in settings.json, not CLAUDE.md), so we never mistake a
    still-routed config for clean and delete the only backup."""
    for cfg in _touched(tool):
        try:
            text = cfg.read_text(errors="ignore")   # ValueError/UnicodeDecodeError-safe (markers ASCII)
        except OSError:
            continue
        if any(m in text for m in INJECT_MARKERS):
            return True
    return False


def _any_injected() -> bool:
    return any(_is_injected(t) for t in ("codex", "claude"))


# --- global (app-managed) mode: route plain codex/claude + GUI through Headroom ----------------
# Driven by the app's "save credit" toggle. ON = `headroom install apply` (inject routing + run the
# proxy) + an on-disk config backstop; OFF/quit/health-fail = `headroom install remove` + restore
# the backstop. App-managed: removed on quit/health-fail so codex/claude never point at a dead proxy.
# All ops are serialized by an flock op-lock so concurrent toggle/poll/recovery can't interleave.

def _global_backup(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-global-backup"


def _global_files() -> list[Path]:
    return _touched("codex") + _touched("claude")


def _log_full(store: Path | None, label: str, text: str) -> None:
    """Append full headroom output to a private 0600 log for debugging (append, not read+rewrite, so
    repeated logging stays O(1) per call)."""
    from .util import now, iso
    try:
        p = (store or P.DATA_DIR) / "headroom.log"   # ~/.account-switcher/headroom.log (matches msgs)
        p.parent.mkdir(parents=True, exist_ok=True)
        # create 0600 up-front so the log is never briefly world-readable
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"\n[{iso(now())}] {label}\n{text}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


@contextlib.contextmanager
def op_lock(store: Path | None = None, *, blocking: bool = True):
    """Exclusive cross-process lock for the whole enable/disable/recover operation (apply + snapshot
    + restore), so a background toggle, usage poll, launch recovery, and quit teardown can't race.

    Yields True if the lock was acquired, False if ``blocking=False`` and another op holds it. The
    blocking callers (enable/disable/heal) ignore the value (it's always True for them); only quit
    teardown uses blocking=False so it can never freeze the UI waiting on a slow background op."""
    import fcntl
    # The lock file lives BESIDE the backup dir, never inside it — restore_global/_rm_backup rmtree
    # the backup dir, which would otherwise swap the lock's inode mid-hold and break serialization.
    base = _global_backup(store).parent
    base.mkdir(parents=True, exist_ok=True)
    f = open(base / ".headroom-oplock", "w")
    acquired = False
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _rm_backup(store: Path | None = None) -> None:
    shutil.rmtree(_global_backup(store), ignore_errors=True)


def has_backup(store: Path | None = None) -> bool:
    return (_global_backup(store) / "manifest.json").exists()


def snapshot_global(store: Path | None = None) -> None:
    """Snapshot the user's ORIGINAL config (bytes + mode + symlink target). Idempotent: never
    overwrites an existing backup, so a second enable can't capture the already-routed config."""
    from .util import atomic_write_text
    bdir = _global_backup(store)
    if (bdir / "manifest.json").exists():
        return  # original already captured — do NOT re-baseline a possibly-routed config
    bdir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for i, p in enumerate(_global_files()):
        if p.is_symlink():
            entry = {"kind": "symlink", "target": os.readlink(p)}
        elif p.exists():
            entry = {"kind": "file", "b64": base64.b64encode(p.read_bytes()).decode(),
                     "mode": stat.S_IMODE(p.lstat().st_mode)}
        else:
            entry = {"kind": "absent"}
        entry["path"] = str(p)
        (bdir / f"{i}.json").write_text(json.dumps(entry))
        manifest[str(i)] = str(p)
    atomic_write_text(bdir / "manifest.json", json.dumps(manifest))


def restore_global(store: Path | None = None) -> tuple[bool, list[str]]:
    """Atomically restore every snapshotted path to its ORIGINAL state (bytes/mode/symlink/absent).
    Returns (ok, failures). The backup is deleted ONLY if every path restored cleanly."""
    from .util import atomic_write_bytes
    bdir = _global_backup(store)
    mf = bdir / "manifest.json"
    if not mf.exists():
        return True, []
    try:
        manifest = json.loads(mf.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"manifest unreadable: {e}"]
    failures: list[str] = []
    for i, path_s in manifest.items():
        p = Path(path_s)
        try:
            entry = json.loads((bdir / f"{i}.json").read_text())
            if p.is_symlink() or p.exists():
                p.unlink()
            if entry["kind"] == "symlink":
                p.symlink_to(entry["target"])
            elif entry["kind"] == "file":
                atomic_write_bytes(p, base64.b64decode(entry["b64"]), mode=entry.get("mode", 0o600))
            # kind == "absent" → leave it removed
        except OSError as e:
            failures.append(f"{p}: {e}")
    if not failures:
        shutil.rmtree(bdir, ignore_errors=True)
    return (not failures), failures


def _hr_run(args: list, *, run=subprocess.run, timeout: float = 120):
    exe = headroom_path()
    if not exe:
        return 1, "headroom not installed"
    try:
        p = run([exe, *args], capture_output=True, text=True, timeout=timeout,
                env=harden_env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.SubprocessError, OSError) as e:
        return 1, str(e)


def global_running(*, run=subprocess.run) -> bool:
    """True only if the persistent deployment is actually running (robust to 'not running'/'stopped'
    substrings in the status output)."""
    rc, out = _hr_run(["install", "status"], run=run, timeout=20)
    if rc != 0:
        return False
    low = out.lower()
    # "down" states. NB: deliberately NOT bare "error" — a healthy status line can say "errors: 0".
    if any(s in low for s in ("not running", "stopped", "not installed", "no deployment",
                              "not deployed", "failed", "crashed", " dead")):
        return False
    # accept the common ways a healthy proxy is reported (real CLI wording confirmed at M8 live test)
    return any(s in low for s in ("running", "active", "listening", "healthy", "serving", " up"))


def _remove_and_restore(store: Path | None, *, run=subprocess.run,
                        timeout: float = 45) -> tuple[bool, str]:
    """Undo routing + stop the proxy. Prefer Headroom's own surgical `install remove` (which
    preserves any user edits made while routing was on); fall back to our full snapshot restore ONLY
    if remove failed or left injection markers. CALLER MUST HOLD op_lock (never takes it itself, so
    it can be reused by global_disable and heal without deadlocking — flock isn't reentrant)."""
    rc, out = _hr_run(["install", "remove"], run=run, timeout=timeout)
    if rc == 0 and not _any_injected():
        _rm_backup(store)                             # clean removal; keep user edits
        return True, "headroom routing removed"
    _log_full(store, "remove incomplete", out)
    ok, failures = restore_global(store)              # backstop: exact restore; backup kept on failure
    if not ok:
        _log_full(store, "restore failures", "\n".join(failures))
        return False, "Headroom removed but config restore incomplete (see ~/.account-switcher/headroom.log)"
    if _any_injected():
        # remove failed AND there was no backup to restore from → config is still routed. Don't
        # report success: heal/reconcile must keep the setting on and keep trying, not clear it.
        _log_full(store, "still injected after restore", "no backup; install remove left routing")
        return False, "Headroom routing still present and no backup to restore (see ~/.account-switcher/headroom.log)"
    return True, "headroom routing removed; config restored from backup"


def global_enable(store: Path | None = None, *, run=subprocess.run) -> tuple[bool, str]:
    """Route plain codex/claude (+GUI) through Headroom. Verifies the rtk helper, snapshots the
    ORIGINAL config, applies, then VERIFIES the proxy is actually running — rolling back fully on any
    failure so clients are never left pointing at a dead proxy. Serialized by op_lock."""
    with op_lock(store):
        ok_rtk, rtk_msg = verify_rtk(store)           # supply-chain TOFU check — BEFORE the early
        if not ok_rtk:                                # return, so a tampered rtk is caught even when
            return False, f"Headroom rtk integrity check failed — {rtk_msg}"   # routing's already up
        if has_backup(store) and global_running(run=run):
            return True, "headroom already on"
        # If config is already injected without a backup (state desync), strip it first so the
        # snapshot we baseline is the user's ORIGINAL, not a routed config. If the strip doesn't
        # clean it, ABORT — baselining a routed config would make a later restore reinstate routing.
        if not has_backup(store) and _any_injected():
            _hr_run(["install", "remove"], run=run)
            if _any_injected():
                _log_full(store, "global_enable baseline dirty", "config still injected after remove")
                return False, "couldn't establish a clean Headroom baseline (config already routed)"
        snapshot_global(store)                        # idempotent: keeps the original if already saved
        rc, out = _hr_run(["install", "apply", "--providers", "auto"], run=run)
        if rc != 0 or not global_running(run=run):    # apply failed OR proxy didn't come up healthy
            _log_full(store, "global_enable failed", out)
            _hr_run(["install", "remove"], run=run)   # undo any partial routing
            ok, failures = restore_global(store)      # restore the original config exactly
            if not ok:
                _log_full(store, "global_enable rollback restore failures", "\n".join(failures))
                return False, ("couldn't enable Headroom and config restore is incomplete — run "
                               "save-credit again to retry (see ~/.account-switcher/headroom.log)")
            return False, "couldn't enable Headroom (see ~/.account-switcher/headroom.log)"
        return True, "headroom routing enabled for codex & claude"


def global_disable(store: Path | None = None, *, run=subprocess.run,
                   timeout: float = 45, blocking: bool = True) -> tuple[bool, str]:
    """Undo routing + stop the proxy. Serialized by op_lock. With blocking=False (quit teardown),
    returns (False, 'busy') immediately rather than waiting on a concurrent op — the next launch /
    cx / cl heal() is a reliable backstop, so quit never freezes on lock acquisition."""
    with op_lock(store, blocking=blocking) as acquired:
        if not acquired:
            return False, "headroom busy (another operation in progress)"
        return _remove_and_restore(store, run=run, timeout=timeout)


def reconcile(ctx, *, blocking: bool = True) -> tuple[bool, str]:
    """heal() + reflect the result in the save-credit setting, so the launcher (cx/cl) and the GUI
    poll share ONE recovery policy (instead of each clearing/keeping the setting differently). The
    setting is cleared ONLY on a successful heal (healed=True) — a failed restore leaves it ON so
    recovery keeps retrying. blocking=False (cx/cl) skips healing when the GUI holds the lock (e.g.
    mid-enable), so a launch never hangs waiting on a long `install apply`. ctx is duck-typed:
    data_dir, locked(), load_state()."""
    healed, msg = heal(ctx.data_dir, blocking=blocking)
    if healed:
        with ctx.locked():
            s = ctx.load_state(); s.set_setting("headroom", False); s.save()
    return healed, msg


def needs_reconcile(ctx) -> bool:
    """Cheap, subprocess-free pre-check: is there any reason routing might need healing? Lets the
    cx/cl hot path skip the `headroom install status` round-trip entirely when save-credit was never
    used (no setting, no backup, no on-disk injection)."""
    try:
        if ctx.load_state().settings().get("headroom"):
            return True
    except Exception:
        pass
    return has_backup(ctx.data_dir) or _any_injected()


def heal(store: Path | None = None, *, run=subprocess.run,
         blocking: bool = True) -> tuple[bool, str]:
    """Serialized self-heal that keys off ACTUAL state, not any setting — so it fixes orphans left by
    a failed enable/disable, a crash, or a force-quit, regardless of how the setting reads.

    Returns (healed, msg) where healed=True ONLY when dead routing was found AND successfully removed
    + restored. healed=False covers: lock busy, proxy healthy, config already clean, OR a restore
    that FAILED (config still injected) — so callers never mistake an incomplete restore for success.

    Inside op_lock:
      • busy (blocking=False, another op holds it) → (False, "busy"): let that op finish.
      • proxy running → no-op. Re-checking `global_running` here, under the same lock the toggle
        holds, closes the enable/health-check TOCTOU: a heal that fires while an enable is still
        bringing the proxy up blocks on the lock, then sees it running and leaves it.
      • routing injected but proxy dead → remove routing + restore config.
      • nothing injected → no-op.
    """
    with op_lock(store, blocking=blocking) as acquired:
        if not acquired:
            return False, "busy"
        if global_running(run=run):
            return False, "healthy"
        if not (_any_injected() or has_backup(store)):
            return False, "clean"
        ok, msg = _remove_and_restore(store, run=run)
        return ok, msg                            # ok=False ⇒ still injected; NOT a successful heal


def headroom_path() -> str | None:
    """Absolute path to the `headroom` CLI: PATH first, then this venv's bin dir."""
    found = shutil.which("headroom")
    if found:
        return found
    cand = Path(sys.executable).parent / "headroom"
    return str(cand) if cand.exists() else None


def available() -> bool:
    return headroom_path() is not None


def venv_bin_dir() -> str:
    return str(Path(sys.executable).parent)


def ensure_installed() -> bool:
    """Best-effort: install headroom into the current venv if missing. Returns availability."""
    if available():
        return True
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", PACKAGE],
                       capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        pass
    return available()


