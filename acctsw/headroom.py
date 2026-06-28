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
    import os as _os
    e = dict(_os.environ if env is None else env)
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
    from .util import sha256_text
    rtk = rtk_path()
    if not rtk.exists():
        return True, "rtk not present yet (downloaded on first wrap)"
    digest = sha256_text(rtk.read_bytes().hex())
    store = (record_dir or (Path.home() / ".account-switcher")) / "rtk.sha256"
    if store.exists():
        recorded = store.read_text().strip()
        if recorded != digest:
            return False, f"rtk checksum changed ({recorded[:12]}… → {digest[:12]}…)"
        return True, "rtk checksum verified"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(digest)
    return True, f"rtk checksum recorded ({digest[:12]}…)"

# Markers Headroom leaves in the tool config — used to detect a still-injected (dirty) config.
INJECT_MARKERS = ("Headroom", 'model_provider = "headroom"', "headroom:rtk-instructions")


def _config_dir(tool: str) -> Path:
    return P.CODEX_HOME if tool == "codex" else P.CLAUDE_CONFIG_DIR


def _touched(tool: str) -> list[Path]:
    """Files `headroom wrap <tool>` mutates. (Codex verified live; Claude best-effort superset.)"""
    d = _config_dir(tool)
    if tool == "codex":
        return [d / "config.toml", d / "AGENTS.md"]
    return [d / "CLAUDE.md", d / "settings.json", d / "settings.local.json", d / ".mcp.json"]


def _store_dir(store: Path | None) -> Path:
    return (store or (P.DATA_DIR / "headroom-backup"))


def _is_injected(tool: str) -> bool:
    cfg = _touched(tool)[0]
    try:
        text = cfg.read_text()
    except OSError:
        return False
    return any(m in text for m in INJECT_MARKERS)


# --- global (app-managed) mode: route plain codex/claude + GUI through Headroom ----------------
# Driven by the app's "save credit" toggle. ON = `headroom install apply` (inject routing + run the
# proxy) + an on-disk config backstop; OFF/quit/health-fail = `headroom install remove` + restore
# the backstop. App-managed: removed on quit/health-fail so codex/claude never point at a dead proxy.
# All ops are serialized by an flock op-lock so concurrent toggle/poll/recovery can't interleave.

def _global_backup(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-global-backup"


def _global_files() -> list[Path]:
    return _touched("codex") + _touched("claude")


def _redact(text: str) -> str:
    """Drop anything token/key/path-shaped before a message can reach the UI/state."""
    import re
    return re.sub(r"(sk-[A-Za-z0-9_-]+|rt\.[A-Za-z0-9_.-]+|/Users/[^\s]+|[A-Za-z0-9_-]{32,})",
                  "[redacted]", text)


def _log_full(store: Path | None, label: str, text: str) -> None:
    """Write full (unredacted) headroom output to a private 0600 log for debugging."""
    from .util import atomic_write_text
    from .util import now, iso
    try:
        p = _store_dir(store).parent / "headroom.log"
        prev = p.read_text() if p.exists() else ""
        atomic_write_text(p, f"{prev}\n[{iso(now())}] {label}\n{text}\n", mode=0o600)
    except OSError:
        pass


@contextlib.contextmanager
def op_lock(store: Path | None = None):
    """Exclusive cross-process lock for the whole enable/disable/recover operation (apply + snapshot
    + restore), so a background toggle, usage poll, launch recovery, and quit teardown can't race."""
    import fcntl
    bdir = _global_backup(store)
    bdir.mkdir(parents=True, exist_ok=True)
    f = open(bdir / ".oplock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


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
        import shutil as _sh
        _sh.rmtree(bdir, ignore_errors=True)
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
    if any(s in low for s in ("not running", "stopped", "not installed", "no deployment", "error")):
        return False
    return "running" in low


def global_enable(store: Path | None = None, *, run=subprocess.run) -> tuple[bool, str]:
    """Route plain codex/claude (+GUI) through Headroom. Verifies the rtk helper, snapshots the
    ORIGINAL config, applies, then VERIFIES the proxy is actually running — rolling back fully on any
    failure so clients are never left pointing at a dead proxy. Serialized by op_lock."""
    with op_lock(store):
        if has_backup(store) and global_running(run=run):
            return True, "headroom already on"
        ok_rtk, rtk_msg = verify_rtk(store)           # supply-chain TOFU check (kept on the live path)
        if not ok_rtk:
            return False, f"Headroom rtk integrity check failed — {rtk_msg}"
        # If config is already injected without a backup (state desync), strip it first so the
        # snapshot we baseline is the user's ORIGINAL, not a routed config.
        if not has_backup(store) and any(_is_injected(t) for t in ("codex", "claude")):
            _hr_run(["install", "remove"], run=run)
        snapshot_global(store)                        # idempotent: keeps the original if already saved
        rc, out = _hr_run(["install", "apply", "--providers", "auto"], run=run)
        if rc != 0 or not global_running(run=run):    # apply failed OR proxy didn't come up healthy
            _log_full(store, "global_enable failed", out)
            _hr_run(["install", "remove"], run=run)   # undo any partial routing
            restore_global(store)                     # restore the original config exactly
            return False, "couldn't enable Headroom (see ~/.account-switcher/headroom.log)"
        return True, "headroom routing enabled for codex & claude"


def global_disable(store: Path | None = None, *, run=subprocess.run,
                   timeout: float = 45) -> tuple[bool, str]:
    """Undo routing + stop the proxy. Prefer Headroom's own surgical `install remove` (which
    preserves any user edits made while routing was on); fall back to our full snapshot restore
    ONLY if remove failed or left injection markers. Serialized by op_lock."""
    with op_lock(store):
        rc, out = _hr_run(["install", "remove"], run=run, timeout=timeout)
        still_injected = any(_is_injected(t) for t in ("codex", "claude"))
        if rc == 0 and not still_injected:
            import shutil as _sh
            _sh.rmtree(_global_backup(store), ignore_errors=True)   # clean removal; keep user edits
            return True, "headroom routing removed"
        _log_full(store, "global_disable remove incomplete", out)
        ok, failures = restore_global(store)          # backstop: exact restore; backup kept on failure
        if not ok:
            _log_full(store, "global_disable restore failures", "\n".join(failures))
            return False, "Headroom removed but config restore incomplete (see ~/.account-switcher/headroom.log)"
        return True, "headroom routing removed; config restored from backup"


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


