"""Headroom integration — the "save credit" toggle.

When enabled, the supervised launcher routes the agent through Headroom
(https://github.com/chopratejas/headroom), which compresses what the agent reads → fewer
tokens → usage limits are hit more slowly. Headroom is a pure data-path wrapper: it never touches
credentials or the keychain.

It's installed into THIS app's venv by `acctsw install` (so it "just works" — no separate install),
and we locate it next to the running interpreter even when the venv's bin isn't on PATH.

Mechanism: we DELIBERATELY do NOT use `headroom install apply`. Its macOS launchd deployment
(`persistent-service`) is broken — silent startup failures (no log paths in the plist), a runner
command resolved via `shutil.which` that isn't reachable in launchd's bare environment, and a
destructive first-apply rollback that erases the deploy dir. See docs/headroom-handover.md. Instead
we run the proxy OURSELVES as a detached, PID-tracked subprocess (start_proxy) — the same launchd-free
path `headroom init` uses (supervisor_kind=NONE) — and hand-write the minimal provider routing into the
tools' own config files (_route_all), which we snapshot/detect/restore exactly as before.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
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


# The output shaper produces the token-savings number; it's env-gated and read live by the proxy.
# Since WE start the proxy (start_proxy), we put this straight into the child's environment — no
# env-less launchd plist to work around anymore.
SHAPER_ENV = {"HEADROOM_OUTPUT_SHAPER": "1"}

PROXY_PORT = 8787                  # Headroom's default; we start `headroom proxy --port 8787`
# Hold out a fraction of conversations UNSHAPED so `headroom output-savings` can report a real
# *measured* A/B number (unbiased shaped-vs-unshaped), not just the synthetic-control "estimate".
# Costs ~this fraction of potential savings — a deliberate trade for a trustworthy figure.
OUTPUT_HOLDOUT = "0.1"
# Best-effort backstop: re-assert the shaper/holdout on the live proxy via /admin/runtime-env. The
# primary path is start_proxy's env (above); this only matters if a proxy we didn't start (e.g. one
# that outlived a crash) is reachable. See push_runtime_knobs().
RUNTIME_KNOBS = {"HEADROOM_OUTPUT_SHAPER": "1", "HEADROOM_OUTPUT_HOLDOUT": OUTPUT_HOLDOUT}


def push_runtime_knobs(port: int = PROXY_PORT, *, knobs: dict | None = None, urlopen=None) -> bool:
    """Hot-sync the live output-shaper knobs to the running proxy via POST /admin/runtime-env — the
    same loopback hot-reload `headroom wrap` uses. Best-effort: returns False (silent) if the proxy
    is unreachable or predates the endpoint. Belt-and-suspenders only: start_proxy already passes
    these in the proxy's env, so this just re-asserts them on a proxy we didn't start."""
    import urllib.error
    import urllib.request
    body = json.dumps(knobs or RUNTIME_KNOBS).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/runtime-env",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(req, timeout=2) as resp:
            resp.read()
        return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


# py2app injects PYTHONHOME/PYTHONPATH (and friends) pointing at the FROZEN app's stripped, zipped
# stdlib. Every Headroom subprocess runs a DIFFERENT interpreter (the managed venv's headroom, or
# `python -m venv`); if these leak in, that interpreter resolves stdlib against the app bundle and dies
# (e.g. `ModuleNotFoundError: No module named 'uuid'`). Strip them so each child python uses its own.
_PY_ENV_STRIP = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")


def harden_env(env: dict | None = None) -> dict:
    """Return env with hardening flags applied + interpreter-redirect vars stripped (so a child python
    uses its own stdlib, not the frozen app's). Does not mutate the input."""
    e = dict(os.environ if env is None else env)
    for k in _PY_ENV_STRIP:
        e.pop(k, None)
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

# Markers our routing leaves in the tool config — used to detect a still-injected (dirty) config.
# Deliberately CONFIG-SYNTAX strings (not prose words like "Headroom" or "headroomlabs", which a
# user could legitimately write): a loose substring would treat such a config as still-routed and
# let the restore backstop overwrite their real edits. `model_provider = "headroom"` is what we write
# into Codex's config.toml; the loopback proxy URL is what we write into Claude's settings.json env
# (ANTHROPIC_BASE_URL) and into the Codex provider block (base_url …/v1) — so both routings detect.
_PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
INJECT_MARKERS = ('model_provider = "headroom"', _PROXY_URL)


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


# --- routing: hand-write minimal provider config so plain codex/claude point at our local proxy ----
# We write just enough to route each tool at 127.0.0.1:PROXY_PORT, using the SAME markers
# _is_injected()/INJECT_MARKERS detect and snapshot_global/restore_global back up. This avoids
# `headroom install apply` (broken launchd deploy — see module docstring); start_proxy runs the proxy.

_CODEX_MARK_START = "# --- acctsw headroom routing ---"
_CODEX_MARK_END = "# --- end acctsw headroom routing ---"
# Top-level Codex keys routing owns. Codex honours a single top-level model_provider/openai_base_url,
# so to route we must REPLACE the user's (not append) — otherwise a user who already set either key
# gets a duplicate top-level key, which is invalid TOML and stops Codex launching. So we strip ANY
# value of these keys before writing ours; the user's original is captured in the pre-routing snapshot
# and restored verbatim on disable (restore_global). They MUST precede any [table] header or TOML
# scopes them into it (headroom #260), so _route_codex writes them as the first lines of the file.
_CODEX_TOP_KEYS = (
    re.compile(r'(?m)^[ \t]*model_provider[ \t]*=.*\r?\n'),
    re.compile(r'(?m)^[ \t]*openai_base_url[ \t]*=.*\r?\n'),
)


def _codex_chatgpt_auth() -> bool:
    """True if Codex authed via ChatGPT OAuth (not an API key). Only then does the provider block get
    `requires_openai_auth = true` (restores the account menu); emitting it for API-key users would
    force an unwanted OAuth login (headroom #406)."""
    try:
        data = json.loads((P.CODEX_HOME / "auth.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    mode = data.get("auth_mode")
    if isinstance(mode, str):
        return mode.lower() == "chatgpt"
    tokens = data.get("tokens")        # older auth.json predates auth_mode → infer from an OAuth acct
    return isinstance(tokens, dict) and bool(str(tokens.get("account_id") or "").strip())


def _strip_codex_routing(content: str) -> str:
    """Remove our managed Codex routing (top-level keys + marker block). Makes _route_codex idempotent
    and serves as the surgical unroute that preserves unrelated user edits."""
    while _CODEX_MARK_START in content and _CODEX_MARK_END in content:
        s = content.index(_CODEX_MARK_START)
        e = content.index(_CODEX_MARK_END, s) + len(_CODEX_MARK_END)
        content = content[:s].rstrip("\n") + ("\n" + content[e:].lstrip("\n"))
    for pat in _CODEX_TOP_KEYS:
        content = pat.sub("", content)
    return content.lstrip("\n")


def _route_codex(port: int = PROXY_PORT) -> None:
    from .util import atomic_write_text
    path = P.CODEX_HOME / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        body = _strip_codex_routing(path.read_text()) if path.exists() else ""
    except OSError:
        body = ""
    requires = "requires_openai_auth = true\n" if _codex_chatgpt_auth() else ""
    head = f'model_provider = "headroom"\nopenai_base_url = "http://127.0.0.1:{port}/v1"\n'
    table = (f"{_CODEX_MARK_START}\n"
             "[model_providers.headroom]\n"
             'name = "Headroom proxy"\n'
             f'base_url = "http://127.0.0.1:{port}/v1"\n'
             "supports_websockets = true\n"
             f"{requires}"
             f"{_CODEX_MARK_END}\n")
    mid = (body.strip() + "\n\n") if body.strip() else ""
    atomic_write_text(path, f"{head}\n{mid}{table}")


def _unroute_codex(port: int = PROXY_PORT) -> None:
    from .util import atomic_write_text
    path = P.CODEX_HOME / "config.toml"
    if not path.exists():
        return
    try:
        stripped = _strip_codex_routing(path.read_text())
    except OSError:
        return
    atomic_write_text(path, stripped if stripped.strip() else "")


def _route_claude(port: int = PROXY_PORT) -> None:
    from .util import atomic_write_text
    path = P.CLAUDE_CONFIG_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
    env = payload.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    # GH#746: with a custom ANTHROPIC_BASE_URL and ENABLE_TOOL_SEARCH unset, Claude Code materializes
    # every MCP/system tool schema into context → overflow. Keep schema deferral on.
    env["ENABLE_TOOL_SEARCH"] = "true"
    payload["env"] = env
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _unroute_claude(port: int = PROXY_PORT) -> None:
    from .util import atomic_write_text
    path = P.CLAUDE_CONFIG_DIR / "settings.json"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    env = payload.get("env")
    if not (isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}"):
        return                                          # not our routing → leave it alone
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ENABLE_TOOL_SEARCH", None)
    if env:
        payload["env"] = env
    else:
        payload.pop("env", None)
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _route_all(port: int = PROXY_PORT) -> None:
    _route_codex(port)
    _route_claude(port)


def _unroute_all(port: int = PROXY_PORT) -> None:
    _unroute_codex(port)
    _unroute_claude(port)


# --- proxy: run `headroom proxy` ourselves as a detached, PID-tracked subprocess -------------------
# Replaces `headroom install apply`'s broken launchd service. We own the child's environment, so the
# output-shaper knobs go in at startup. start_new_session detaches it from the app's process group;
# we track it by PID file and tear it down on toggle-off/quit/health-fail (stop_proxy) so plain
# codex/claude never point at a dead proxy.

def _proxy_pidfile(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-proxy.pid"


def _proxy_logfile(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-proxy.log"


def proxy_ready(port: int = PROXY_PORT, *, urlopen=None, timeout: float = 2.0) -> bool:
    """True iff the proxy answers GET /readyz with ready:true. Cross-process (plain HTTP), so a cx/cl
    launcher in another process can check the GUI-started proxy."""
    import urllib.error
    import urllib.request
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(f"http://127.0.0.1:{port}/readyz", timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("ready"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_busy(port: int = PROXY_PORT) -> bool:
    """True iff something is accepting TCP connections on the loopback port (may not be our proxy)."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _port_listener_pid(port: int = PROXY_PORT) -> int | None:
    """PID of whatever is LISTENing on the loopback port (via lsof), or None if unknown/none."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    return pids[0] if pids else None


def _adopt_proxy_pid(store: Path | None, port: int = PROXY_PORT) -> None:
    """Ensure the pidfile points at the live listener so stop_proxy can kill a proxy we didn't start
    THIS run (e.g. one that outlived a crash). No-op if we already track a live pid — without this,
    start_proxy's idempotent early-return would leave an untracked proxy we can never tear down."""
    pidf = _proxy_pidfile(store)
    try:
        cur = int(pidf.read_text().strip())
    except (OSError, ValueError):
        cur = 0
    if cur > 0 and _pid_alive(cur):
        return
    adopted = _port_listener_pid(port)
    if adopted:
        try:
            pidf.parent.mkdir(parents=True, exist_ok=True)
            pidf.write_text(str(adopted))
        except OSError:
            pass


def start_proxy(store: Path | None = None, *, port: int = PROXY_PORT, popen=None, sleep=None,
                ready_timeout: float = 30.0, clock=None) -> bool:
    """Start the proxy detached + PID-tracked and block until /readyz is healthy (bounded by
    ready_timeout, ~30s). Idempotent: if a proxy already serves the port, adopt its PID (so we can
    still stop it) and return True. Fails fast if the port is held by a non-Headroom process. Returns
    False if headroom is missing or the proxy never becomes ready (caller rolls back)."""
    import time
    _clock = clock or time.monotonic
    if proxy_ready(port):
        _adopt_proxy_pid(store, port)     # so a proxy we didn't start this run is still stoppable
        return True
    if _port_busy(port):                  # bound but not answering /readyz → foreign listener
        _log_full(store, "start_proxy port busy",
                  f"port {port} is held by a non-Headroom process; refusing to start")
        return False
    exe = headroom_path()
    if not exe:
        return False
    _popen = popen or subprocess.Popen
    _sleep = sleep or time.sleep
    env = harden_env()
    env.update(SHAPER_ENV)
    env["HEADROOM_OUTPUT_HOLDOUT"] = OUTPUT_HOLDOUT
    pidf = _proxy_pidfile(store)
    pidf.parent.mkdir(parents=True, exist_ok=True)
    try:
        logfd = os.open(_proxy_logfile(store), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        logfd = None
    sink = logfd if logfd is not None else subprocess.DEVNULL
    try:
        proc = _popen([exe, "proxy", "--host", "127.0.0.1", "--port", str(port),
                       "--mode", "token", "--backend", "anthropic", "--no-telemetry"],
                      env=env, stdout=sink, stderr=sink, start_new_session=True)
    except (OSError, ValueError) as e:
        _log_full(store, "start_proxy failed", str(e))
        return False
    finally:
        if logfd is not None:
            os.close(logfd)
    try:
        pidf.write_text(str(proc.pid))
    except OSError:
        pass
    # Poll until a wall-clock deadline (not attempts*timeout, which could balloon to minutes when each
    # probe hits its own timeout) so start_proxy never blocks the op_lock far longer than advertised.
    deadline = _clock() + ready_timeout
    while _clock() < deadline:
        if proxy_ready(port, timeout=1.0):
            return True
        _sleep(0.5)
    _log_full(store, "start_proxy timeout", f"pid={getattr(proc, 'pid', '?')} never reached /readyz")
    stop_proxy(store)
    return False


def proxy_maybe_running(store: Path | None = None) -> bool:
    """Cheap, subprocess- and network-free check: does our proxy pidfile point at a live process?
    Used by the cx/cl gate to catch an ORPHAN proxy whose routing+backup were already cleaned (a
    graceful-OFF that deletes the backup leaves needs_reconcile() False, so the pidfile is the only
    remaining trace). False positives are harmless — heal() re-verifies under the lock."""
    pidf = _proxy_pidfile(store)
    try:
        pid = int(pidf.read_text().strip())
    except (OSError, ValueError):
        return False
    return pid > 0 and _pid_alive(pid)


def routing_injected() -> bool:
    """True if Headroom routing is currently written into the tools' config (public predicate so
    callers outside this module don't reach into the private _any_injected)."""
    return _any_injected()


def stop_proxy(store: Path | None = None, *, kill=None, sleep=None) -> None:
    """Stop the proxy we started (by PID file) and clear the file. Best-effort; safe if not running."""
    import signal
    import time
    _kill = kill or os.kill
    _sleep = sleep or time.sleep
    pidf = _proxy_pidfile(store)
    try:
        pid = int(pidf.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid > 0 and _pid_alive(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                _kill(pid, sig)
            except OSError:
                break
            _sleep(0.3)
            if not _pid_alive(pid):
                break
    try:
        pidf.unlink()
    except OSError:
        pass


# --- global (app-managed) mode: route plain codex/claude + GUI through Headroom ----------------
# Driven by the app's "save credit" toggle. ON = start our own proxy (start_proxy) + write routing
# into the tools' config (_route_all) + an on-disk config backstop; OFF/quit/health-fail = strip
# routing (_unroute_all) + restore the backstop + stop the proxy (stop_proxy). App-managed: torn down
# on quit/health-fail so codex/claude never point at a dead proxy.
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
    """Run a headroom CLI subcommand with hardening env. Used for `learn` (baseline seeding); the
    proxy/routing paths no longer shell out to `install`."""
    exe = headroom_path()
    if not exe:
        return 1, "headroom not installed"
    try:
        p = run([exe, *args], capture_output=True, text=True, timeout=timeout, env=harden_env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.SubprocessError, OSError) as e:
        return 1, str(e)


def global_running() -> bool:
    """True iff our proxy is up and healthy (GET /readyz returns ready:true). We run the proxy
    ourselves now, so readiness is the source of truth — no `headroom install status` round-trip."""
    return proxy_ready()


def _remove_and_restore(store: Path | None, *, reap_proxy: bool = True) -> tuple[bool, str]:
    """Undo routing (and optionally stop our proxy). Routing REPLACES user keys (codex
    model_provider/openai_base_url, claude ANTHROPIC_BASE_URL/ENABLE_TOOL_SEARCH), so a surgical strip
    can't bring the user's original values back — only the exact pre-routing snapshot can. So restore
    from the snapshot when we have one; fall back to a surgical strip ONLY for an orphan/desync with no
    backup. reap_proxy=False keeps the proxy alive (graceful toggle-OFF: a Claude/Codex session that
    pinned 127.0.0.1:PORT at launch can't be repointed, so killing the proxy would drop it — leaving it
    up lets open sessions drain while new ones, now unrouted, go direct). CALLER MUST HOLD op_lock."""
    try:
        if has_backup(store):
            ok, failures = restore_global(store)      # exact original, incl. any keys routing overwrote
            if not ok:
                _log_full(store, "restore failures", "\n".join(failures))
                return False, "Headroom removed but config restore incomplete (see ~/.account-switcher/headroom.log)"
            if _any_injected():                       # snapshot itself was routed (shouldn't happen) → don't lie
                _log_full(store, "still injected after restore", "snapshot restore left routing")
                return False, "Headroom routing still present after restore (see ~/.account-switcher/headroom.log)"
            return True, "headroom routing removed; config restored from backup"
        # No backup (orphan/desync left by a crash): surgical strip is the best we can do.
        _unroute_all()
        if _any_injected():
            _log_full(store, "still injected, no backup", "surgical unroute left routing and no backup to restore")
            return False, "Headroom routing still present and no backup to restore (see ~/.account-switcher/headroom.log)"
        return True, "headroom routing removed"
    finally:
        if reap_proxy:
            stop_proxy(store)


def global_enable(store: Path | None = None, *, push=None) -> tuple[bool, str]:
    """Route plain codex/claude (+GUI) through Headroom. Verifies the rtk helper, snapshots the
    ORIGINAL config, starts our own proxy and waits for it to be healthy, THEN writes routing — so
    clients are never left pointing at a dead port. Rolls back fully on any failure. Serialized by
    op_lock."""
    with op_lock(store):
        ok_rtk, rtk_msg = verify_rtk(store)           # supply-chain TOFU check — BEFORE the early
        if not ok_rtk:                                # return, so a tampered rtk is caught even when
            return False, f"Headroom rtk integrity check failed — {rtk_msg}"   # routing's already up
        if has_backup(store) and proxy_ready():
            return True, "headroom already on"
        # If config is already injected without a backup (state desync), strip it first so the
        # snapshot we baseline is the user's ORIGINAL, not a routed config. If the strip doesn't
        # clean it, ABORT — baselining a routed config would make a later restore reinstate routing.
        if not has_backup(store) and _any_injected():
            _unroute_all()
            if _any_injected():
                _log_full(store, "global_enable baseline dirty", "config still routed after unroute")
                return False, "couldn't establish a clean Headroom baseline (config already routed)"
        snapshot_global(store)                        # idempotent: keeps the original if already saved
        # Start the proxy and wait for /readyz BEFORE writing any routing — so a client that picks up
        # the config can never hit a dead port.
        if not start_proxy(store):
            _log_full(store, "global_enable failed", "proxy never reached /readyz")
            stop_proxy(store)                         # clean up any half-started proxy + pidfile
            ok, failures = restore_global(store)      # restore the original config exactly
            if not ok:
                _log_full(store, "global_enable rollback restore failures", "\n".join(failures))
                return False, ("couldn't enable Headroom and config restore is incomplete — run "
                               "save-credit again to retry (see ~/.account-switcher/headroom.log)")
            return False, "couldn't start Headroom proxy (see ~/.account-switcher/headroom.log)"
        # Proxy is healthy → write routing into the tools' OWN config files (codex config.toml, claude
        # settings.json) — the files we snapshot/detect/restore.
        try:
            _route_all()
        except OSError as e:
            _log_full(store, "global_enable routing failed", str(e))
            _unroute_all(); stop_proxy(store); restore_global(store)
            return False, "couldn't enable Headroom (see ~/.account-switcher/headroom.log)"
        (push or push_runtime_knobs)()  # re-assert shaper/holdout on the live proxy (best-effort backstop)
        return True, "headroom routing enabled for codex & claude"


def baseline_seeded(store: Path | None = None) -> bool:
    return ((store or P.DATA_DIR) / "headroom-baseline-seeded").exists()


def seed_baseline(store: Path | None = None, *, run=subprocess.run) -> tuple[bool, str]:
    """Seed Headroom's verbosity baseline so output-savings can report a real number (the shaper's
    'estimated' savings are measured against this baseline). Runs `headroom learn --verbosity --apply
    --all` ONCE (guarded by a marker), best-effort and non-fatal — it's a slow, LLM-driven analysis
    of your coding history, so callers run it in the background after enabling save-credit. Returns
    (ok, msg)."""
    marker = (store or P.DATA_DIR) / "headroom-baseline-seeded"
    if marker.exists():
        return True, "baseline already seeded"
    rc, out = _hr_run(["learn", "--verbosity", "--apply", "--all"], run=run, timeout=600)
    if rc != 0:
        _log_full(store, "seed_baseline failed", out)
        return False, "couldn't seed Headroom savings baseline (see ~/.account-switcher/headroom.log)"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("seeded")
    except OSError:
        pass
    return True, "headroom savings baseline seeded"


def global_disable(store: Path | None = None, *, blocking: bool = True,
                   reap_proxy: bool = True) -> tuple[bool, str]:
    """Undo routing (and, by default, stop the proxy). Serialized by op_lock. With blocking=False (quit
    teardown), returns (False, 'busy') immediately rather than waiting on a concurrent op — the next
    launch / cx / cl heal() is a reliable backstop, so quit never freezes on lock acquisition.

    reap_proxy=False = graceful toggle-OFF: unroute (new codex/claude go direct) but LEAVE the proxy
    running, so an already-open session that pinned 127.0.0.1:PORT at launch keeps working instead of
    hitting ConnectionRefused. The proxy is reaped on quit/health-fail (reap_proxy=True) — its real
    lifecycle is the app's, not the toggle's."""
    with op_lock(store, blocking=blocking) as acquired:
        if not acquired:
            return False, "headroom busy (another operation in progress)"
        return _remove_and_restore(store, reap_proxy=reap_proxy)


def reconcile(ctx, *, blocking: bool = True) -> tuple[bool, str]:
    """heal() + reflect the result in the save-credit setting, so the launcher (cx/cl) and the GUI
    poll share ONE recovery policy (instead of each clearing/keeping the setting differently). The
    setting is cleared ONLY on a successful heal (healed=True) — a failed restore leaves it ON so
    recovery keeps retrying. blocking=False (cx/cl) skips healing when the GUI holds the lock (e.g.
    mid-enable), so a launch never hangs waiting on a long enable. ctx is duck-typed:
    data_dir, locked(), load_state()."""
    healed, msg = heal(ctx.data_dir, blocking=blocking)
    if healed:
        with ctx.locked():
            s = ctx.load_state(); s.set_setting("headroom", False); s.save()
    return healed, msg


def needs_reconcile(ctx) -> bool:
    """Cheap, subprocess-free pre-check: is there any reason routing might need healing? Lets the
    cx/cl hot path skip the /readyz round-trip entirely when save-credit was never used (no setting,
    no backup, no on-disk injection)."""
    try:
        if ctx.load_state().settings().get("headroom"):
            return True
    except Exception:
        pass
    return has_backup(ctx.data_dir) or _any_injected()


def heal(store: Path | None = None, *, blocking: bool = True, push=None,
         app_running: bool = True) -> tuple[bool, str]:
    """Serialized self-heal that keys off ACTUAL state, not any setting — so it fixes orphans left by
    a failed enable/disable, a crash, or a force-quit, regardless of how the setting reads.

    Returns (healed, msg) where healed=True ONLY when routing/proxy was found AND successfully cleaned
    up. healed=False covers: lock busy, proxy healthy (app running), config already clean, OR a restore
    that FAILED (config still injected) — so callers never mistake an incomplete restore for success.

    ``app_running`` is the master switch: the proxy's lifecycle belongs to the menubar app, so a
    proxy that is up while the app is GONE is an ORPHAN (a hard-killed app never ran its quit teardown)
    and must be reaped — otherwise it lingers and any session still pinned to its port keeps routing
    through it. cx/cl pass app_running=False (the app is closed when they exec stock); the GUI poll /
    launcher leave the default True.

    Inside op_lock:
      • busy (blocking=False, another op holds it) → (False, "busy"): let that op finish.
      • proxy running, app alive → no-op. Re-checking `proxy_ready` here, under the same lock the
        toggle holds, closes the enable/health-check TOCTOU: a heal that fires while an enable is
        still bringing the proxy up blocks on the lock, then sees it ready and leaves it.
      • proxy running, app GONE → orphan: strip any routing + restore config + reap the proxy.
      • routing injected but proxy dead → remove routing + restore config + stop the proxy.
      • nothing injected → no-op.
    """
    with op_lock(store, blocking=blocking) as acquired:
        if not acquired:
            return False, "busy"
        if proxy_ready():
            if app_running:
                (push or push_runtime_knobs)()  # best-effort: re-assert knobs on a proxy we may not have started
                return False, "healthy"
            # App is gone but the proxy is still up → orphan. Reap it exactly as a graceful quit would
            # have (strip/restore routing if any was left injected, then stop the proxy).
            return _remove_and_restore(store)
        if not (_any_injected() or has_backup(store)):
            return False, "clean"
        ok, msg = _remove_and_restore(store)
        return ok, msg                            # ok=False ⇒ still injected; NOT a successful heal


# Managed on-demand venv for the proxy. The packaged .app is intentionally slim — it does NOT freeze
# the proxy stack (litellm/onnxruntime/transformers are huge native/ML wheels py2app can't freeze
# cleanly, but pip handles them fine). On first enable we create this venv and pip-install
# headroom-ai[proxy] into it; headroom_path() then finds its real `headroom` console script.
def hr_venv_dir() -> Path:
    return P.DATA_DIR / "hr-venv"


def headroom_path() -> str | None:
    """Absolute path to the `headroom` CLI. Checks, in order: the managed on-demand venv (used by the
    packaged .app), then PATH, then the running interpreter's bin dir (the dev source venv)."""
    managed = hr_venv_dir() / "bin" / "headroom"
    if managed.exists():
        return str(managed)
    found = shutil.which("headroom")
    if found:
        return found
    cand = Path(sys.executable).parent / "headroom"
    return str(cand) if cand.exists() else None


def available() -> bool:
    return headroom_path() is not None


def venv_bin_dir() -> str:
    return str(Path(sys.executable).parent)


def _base_python() -> str | None:
    """A real Python >=3.11 to build the managed venv from. Prefer a system python3.11/3.12 (handles
    the native/ML wheels cleanly); fall back to our own interpreter (the dev venv is already 3.11; a
    py2app-frozen python has venv+ensurepip too)."""
    for name in ("python3.11", "python3.12", "python3.13"):
        p = shutil.which(name)
        if p:
            return p
    p = shutil.which("python3")
    if p:
        try:
            out = subprocess.run([p, "-c", "import sys;print(sys.version_info[:2]>=(3,11))"],
                                 capture_output=True, text=True, timeout=15)
            if out.stdout.strip() == "True":
                return p
        except (subprocess.SubprocessError, OSError):
            pass
    return sys.executable


def ensure_installed() -> bool:
    """Make the `headroom` CLI available. No-op if already found. Otherwise create the managed venv
    (~/.account-switcher/hr-venv) and pip-install headroom-ai[proxy] into it — a one-time, ~hundreds-
    of-MB download (the proxy's litellm/onnxruntime/transformers wheels). Best-effort; returns
    availability."""
    if available():
        return True
    base = _base_python()
    if not base:
        return False
    venv = hr_venv_dir()
    vpy = venv / "bin" / "python"
    env = harden_env()        # strip the frozen-app PYTHONHOME/PYTHONPATH so base/venv python is clean
    try:
        if not vpy.exists():
            venv.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([base, "-m", "venv", str(venv)], capture_output=True, timeout=180,
                           check=True, env=env)
        subprocess.run([str(vpy), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                       capture_output=True, timeout=300, env=env)
        subprocess.run([str(vpy), "-m", "pip", "install", "-q", PACKAGE],
                       capture_output=True, timeout=1800, check=True, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        _log_full(None, "ensure_installed failed", str(e))
        return False
    return available()


