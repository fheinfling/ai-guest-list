"""Legacy Headroom cleanup — a one-time, idempotent migration.

Earlier versions offered a "save credit" toggle that routed plain `codex`/`claude` (and the GUI)
through a local Headroom compression proxy. Measuring it on real workloads (see
`docs/SECURITY-headroom.md` and the retire-Headroom plan) showed the compression was ~1-3%
cache-adjusted on Claude and a rare truncation guardrail on Codex — not worth a wire-path ML proxy
that kept turning itself off. The feature was removed.

This module remains ONLY to clean up after it on machines that used it. `cleanup_legacy` strips any
leftover provider routing from `~/.codex/config.toml` and `~/.claude/settings.json` (restoring the
user's exact pre-routing config from the snapshot when present, else a surgical unroute), stops an
orphaned proxy by its PID file, and deletes the managed venv + bookkeeping. It is safe to call on
every app launch / `cx` / `cl` run; `legacy_present` is the cheap gate that keeps it off the hot path
once there's nothing left to clean.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import paths as P

PROXY_PORT = 8787                                  # the port the old proxy bound; only used for markers
_PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
# Config-syntax strings our old routing wrote — used to detect a still-injected (dirty) config.
INJECT_MARKERS = ('model_provider = "headroom"', _PROXY_URL)


# --- detecting/stripping the old provider routing --------------------------------------------------

def _config_dir(tool: str) -> Path:
    return P.CODEX_HOME if tool == "codex" else P.CLAUDE_CONFIG_DIR


def _touched(tool: str) -> list[Path]:
    """The files our app-managed routing actually wrote — so detection (`_is_injected`) covers exactly
    what cleanup (`_unroute_*`) strips, never a superset it can't fix. Codex routing went into
    config.toml; Claude routing into settings.json AND settings.local.json (the latter overrides the
    former, so a marker there would keep Claude pointed at the dead proxy)."""
    d = _config_dir(tool)
    if tool == "codex":
        return [d / "config.toml"]
    return [d / "settings.json", d / "settings.local.json"]


def _edit_target(path: Path) -> Path:
    """The file to actually edit. `atomic_write_*` swaps the path via `os.replace`, which would
    replace a SYMLINK with a regular file — silently breaking a dotfiles setup and leaving the real
    file still routed. Follow the link and edit its target so the link survives."""
    try:
        if path.is_symlink():
            return path.resolve()
    except OSError:
        pass
    return path


def _is_injected(tool: str) -> bool:
    """Is there routing here that cleanup can actually REMOVE?

    Deliberately defined as "stripping would change this file" rather than as an independent marker
    scan. Detection and removal must agree by construction: anything we can detect but not strip
    would keep `legacy_present()` true forever, re-running the whole migration on every launch / cx /
    cl and reporting "partial" each time. (See INJECT_MARKERS for what the old routing wrote.)
    """
    for cfg in _touched(tool):
        try:
            text = _edit_target(cfg).read_text(errors="ignore")
        except OSError:
            continue
        if tool == "codex":
            if _strip_codex_routing(text) != text:
                return True
        else:
            try:
                if _claude_routed(json.loads(text or "{}")):
                    return True
            except ValueError:
                continue                        # unparseable JSON → nothing we can safely strip
    return False


def _any_injected() -> bool:
    return any(_is_injected(t) for t in ("codex", "claude"))


_CODEX_MARK_START = "# --- acctsw headroom routing ---"
_CODEX_MARK_END = "# --- end acctsw headroom routing ---"
# Top-level Codex keys the old routing wrote (OUTSIDE the marker block, so they need their own
# strip). Matched on OUR EXACT VALUES, never on the key alone: a user's own `model_provider` /
# `openai_base_url` must survive. This runs on every launch/cx/cl for anyone with leftover state,
# and `_unroute_all` fires whenever EITHER tool is injected — so a key-only pattern would delete a
# real `model_provider = "openai"` from a config that never routed through Headroom at all.
# A trailing comment (or EOF with no final newline) must still strip. `_is_injected` is defined as
# "stripping would change this file", so a pattern that can't remove one of these would leave
# cleanup permanently "partial" — re-running the full migration on every launch/cx/cl forever.
_EOL = r'[ \t]*(?:#[^\n]*)?(?:\r?\n|\Z)'
# Matched on our EXACT written values (`_route_codex` wrote `"headroom"` and the proxy URL + "/v1"),
# never on the key alone: a user's own model_provider / openai_base_url must survive.
_CODEX_TOP_KEYS = (
    re.compile(r'(?m)^[ \t]*model_provider[ \t]*=[ \t]*"headroom"' + _EOL),
    re.compile(r'(?m)^[ \t]*openai_base_url[ \t]*=[ \t]*"'
               + re.escape(f"{_PROXY_URL}/v1") + r'"' + _EOL),
)
# A TOML key belongs to the table it sits under, so `model_provider` inside `[profiles.work]` is
# `profiles.work.model_provider` — a DIFFERENT key we never wrote. `_route_codex` always wrote its
# keys as the first lines of the file, so only the region above the first table header is ours.
_TOML_TABLE_HDR = re.compile(r'(?m)^[ \t]*\[')


def _strip_codex_routing(content: str) -> str:
    """Remove our managed Codex routing. Returns ``content`` UNCHANGED (byte-for-byte) when there is
    nothing of ours to strip — `_is_injected` is defined as "this function changes the file", so any
    cosmetic reformatting here would flag every user's config as routed and rewrite it."""
    out = content
    while True:
        s = out.find(_CODEX_MARK_START)
        # Search for END only AFTER start: a hand-edited/corrupted config can hold the markers out of
        # order, and `index(END, s)` would raise ValueError straight through cleanup_legacy. An
        # unpaired/reversed marker is left alone for the top-key strip rather than blowing up.
        e = out.find(_CODEX_MARK_END, s) if s != -1 else -1
        if s == -1 or e == -1:
            break
        e += len(_CODEX_MARK_END)
        out = out[:s].rstrip("\n") + ("\n" + out[e:].lstrip("\n"))
    # Only the region above the first table header holds top-level keys — anything below belongs to
    # a table and is not the key we wrote.
    hdr = _TOML_TABLE_HDR.search(out)
    head, tail = (out[:hdr.start()], out[hdr.start():]) if hdr else (out, "")
    for pat in _CODEX_TOP_KEYS:
        head = pat.sub("", head)
    out = head + tail
    if out == content:
        return content                                  # nothing of ours — do not touch this file
    return out.lstrip("\n")


def _unroute_codex() -> None:
    from .util import atomic_write_text
    path = _edit_target(P.CODEX_HOME / "config.toml")
    if not path.exists():
        return
    try:
        content = path.read_text()
    except OSError:
        return
    stripped = _strip_codex_routing(content)
    if stripped == content:
        return                                          # nothing of ours in here — don't touch the file
    atomic_write_text(path, stripped if stripped.strip() else "")


def _claude_routed(payload) -> bool:
    """Is this settings payload carrying OUR routing? The single predicate behind both detection and
    removal — a non-dict payload that merely CONTAINS the proxy URL is not routing we can strip, and
    must not be reported as dirty (it would never converge)."""
    if not isinstance(payload, dict):
        return False
    env = payload.get("env")
    return isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == _PROXY_URL


def _unroute_claude_file(path: Path) -> None:
    from .util import atomic_write_text
    path = _edit_target(path)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return
    if not _claude_routed(payload):
        return                                          # not our routing → leave it alone
    env = payload["env"]
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ENABLE_TOOL_SEARCH", None)
    if env:
        payload["env"] = env
    else:
        payload.pop("env", None)
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _unroute_claude() -> None:
    for path in _touched("claude"):     # settings.json + settings.local.json
        _unroute_claude_file(path)


def _unroute_all() -> None:
    _unroute_codex()
    _unroute_claude()


# --- the pre-routing config snapshot the old enable path saved -------------------------------------

def _global_backup(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-global-backup"


def has_backup(store: Path | None = None) -> bool:
    return (_global_backup(store) / "manifest.json").exists()


_VALID_KINDS = ("file", "symlink", "absent")

# The ONLY files the old routing ever WROTE: `_route_codex` → ~/.codex/config.toml, `_route_claude`
# → ~/.claude/settings.json. The snapshot, however, captured the wider "files `headroom wrap` may
# touch" set — AGENTS.md, CLAUDE.md, .mcp.json, settings.local.json. Restoring those is pure
# regression: routing never modified them, so their CURRENT state is already the correct one.
# Replaying a months-old snapshot over them silently reverts the user's work, and an entry recorded
# as "absent" (the file did not exist when save-credit was first enabled) DELETES a CLAUDE.md /
# AGENTS.md they have written since. Restore is therefore restricted to what we actually broke.
_ROUTING_OWNED_NAMES = frozenset({"config.toml", "settings.json"})


def _restore_entry(bdir: Path, i: str, p: Path) -> None:
    """Restore ONE snapshotted path, or raise leaving the target untouched.

    Every field is validated and every payload decoded BEFORE the target is touched. An entry we
    can't fully understand must leave the user's config exactly as it is: deleting first and
    discovering the entry is unusable afterwards destroys the only copy (the snapshot IS the backup).
    """
    from .util import atomic_write_bytes
    entry = json.loads((bdir / f"{i}.json").read_text())
    if not isinstance(entry, dict):
        raise ValueError(f"entry is {type(entry).__name__}, expected an object")
    kind = entry.get("kind")
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind {kind!r}")           # never fall through to a bare unlink
    data: bytes | None = None
    mode = 0o600
    target = ""
    if kind == "file":
        b64 = entry.get("b64")
        if not isinstance(b64, str):
            raise ValueError("file entry has no b64 payload")
        # validate=True: the default silently DISCARDS non-alphabet chars, so a corrupt payload
        # decodes to b"" and we'd cheerfully write an empty config over the user's real one.
        data = base64.b64decode(b64, validate=True)
        mode = entry.get("mode", 0o600)
        if not isinstance(mode, int):
            raise ValueError(f"bad mode {mode!r}")
    elif kind == "symlink":
        target = entry.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("symlink entry has no target")
    # --- validated; only now do we mutate ---
    if kind == "file":
        atomic_write_bytes(p, data, mode=mode)               # atomic replace; no unlink-first window
        return
    if p.is_symlink() or p.exists():
        p.unlink()
    if kind == "symlink":
        p.symlink_to(target)
    # kind == "absent" → leave it removed


def restore_global(store: Path | None = None) -> tuple[bool, list[str]]:
    """Restore every snapshotted path to its ORIGINAL state (bytes/mode/symlink/absent). Returns
    (ok, failures); the backup is deleted only if every path restored cleanly."""
    from .util import atomic_write_text
    bdir = _global_backup(store)
    mf = bdir / "manifest.json"
    if not mf.exists():
        return True, []
    try:
        manifest = json.loads(mf.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"manifest unreadable: {e}"]
    if not isinstance(manifest, dict):
        return False, [f"manifest malformed: expected an object, got {type(manifest).__name__}"]
    failures: list[str] = []
    done: list[str] = []
    for i, path_s in manifest.items():
        if not isinstance(path_s, str) or not path_s:
            failures.append(f"entry {i}: bad path {path_s!r}")
            continue
        p = Path(path_s)
        if p.name not in _ROUTING_OWNED_NAMES:
            done.append(i)          # never routed → current state is correct; do not revert/delete
            continue
        # A corrupt entry (disk-full or a hard kill mid-snapshot) must degrade to a recorded failure,
        # NOT an exception: this runs on the cx/cl path, where escaping would stop the user launching
        # codex/claude at all. ValueError covers JSONDecodeError + base64's binascii.Error.
        try:
            _restore_entry(bdir, i, p)
        except (OSError, ValueError, KeyError, TypeError) as e:
            failures.append(f"{p}: {e}")
            continue
        done.append(i)
    if not failures:
        shutil.rmtree(bdir, ignore_errors=True)
        return True, []
    # Partial restore: drop what we already applied so a retry can only replay what actually FAILED.
    # Replaying a succeeded entry would overwrite whatever the user edited in the meantime — every
    # launch, forever, since the retained backup is re-read each time.
    remaining = {i: path for i, path in manifest.items() if i not in done}
    try:
        atomic_write_text(mf, json.dumps(remaining, indent=2) + "\n")
        for i in done:
            (bdir / f"{i}.json").unlink(missing_ok=True)
    except OSError as e:
        failures.append(f"could not prune restored entries: {e}")
    return False, failures


# --- stopping an orphaned proxy by its PID file ----------------------------------------------------

def _proxy_pidfile(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-proxy.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_is_proxy(pid: int) -> bool | None:
    """Identity guard against PID REUSE: is `pid` really a Headroom proxy? True / False / None where
    None means "couldn't determine" (ps unavailable) — the caller must treat that differently from a
    definite "not ours", or it drops the PID file and loses track of a possibly-live proxy forever.

    Matched on ARGV SHAPE, not a substring: the executable must be `headroom` and `proxy` must be its
    own argument. A plain `"headroom" in cmd and "proxy" in cmd` also matches innocent bystanders
    like `tail -f headroom-proxy.log`, and we must never kill one.
    """
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None                                     # unknown — do NOT signal, do NOT forget
    out = proc.stdout.strip()
    if not out:
        return False                                    # no such process → nothing to protect
    parts = out.split()
    exe = Path(parts[0]).name.lower()
    if "headroom" not in exe:
        return False
    return any(a.lower() == "proxy" for a in parts[1:])


def _proxy_pid(store: Path | None) -> int:
    try:
        return int(_proxy_pidfile(store).read_text().strip())
    except (OSError, ValueError):
        return 0


def stop_proxy(store: Path | None = None, *, kill=None, sleep=None) -> None:
    """Stop the old proxy (by PID file) and clear the file. Best-effort; safe if not running. Kills
    ONLY if the PID is alive AND is really a Headroom proxy (never a recycled/unrelated PID)."""
    import signal
    import time
    _kill = kill or os.kill
    _sleep = sleep or time.sleep
    pid = _proxy_pid(store)
    if pid > 0 and _pid_alive(pid) and _pid_is_proxy(pid) is None:
        return          # can't verify identity → don't signal, and KEEP the pidfile so we retry later
    if pid > 0 and _pid_alive(pid) and _pid_is_proxy(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            # Re-verify before EVERY signal: the PID can die and be recycled during the wait, and
            # escalating to SIGKILL on liveness alone would kill whatever inherited the number.
            if not (_pid_alive(pid) and _pid_is_proxy(pid) is True):
                break
            try:
                _kill(pid, sig)
            except OSError:
                break
            _sleep(0.3)
    try:
        _proxy_pidfile(store).unlink()
    except OSError:
        pass


# --- the managed venv + bookkeeping the old feature created ----------------------------------------

def hr_venv_dir(store: Path | None = None) -> Path:
    return (store or P.DATA_DIR) / "hr-venv"


def _leftover_files(store: Path | None) -> list[Path]:
    """Inert bookkeeping the old feature left behind. NB: the proxy PID file is deliberately NOT
    here — `stop_proxy` owns its lifecycle and keeps it when it cannot prove the PID is ours, so
    sweeping it here would delete the only handle on a possibly-live proxy and make it untrackable."""
    d = store or P.DATA_DIR
    return [
        d / "headroom-proxy.log", d / "headroom.log",
        d / "rtk.sha256", d / "headroom-baseline-seeded", d / ".headroom-oplock",
    ]


# --- public migration API --------------------------------------------------------------------------

def _data_dir(ctx_or_store) -> Path:
    """Accept a Context (``.data_dir``) or a plain store Path."""
    return getattr(ctx_or_store, "data_dir", ctx_or_store) or P.DATA_DIR


def legacy_present(ctx_or_store=None) -> bool:
    """Cheap, subprocess-free check: is there any leftover Headroom state to clean? Lets the launch /
    cx / cl hot path skip cleanup entirely once nothing remains (the common case going forward)."""
    store = _data_dir(ctx_or_store)
    if has_backup(store) or _any_injected():
        return True
    if _proxy_pid(store) > 0 or hr_venv_dir(store).exists():
        return True
    return any(p.exists() for p in _leftover_files(store))


def cleanup_legacy(ctx) -> tuple[bool, str]:
    """One-time, idempotent teardown of the retired Headroom feature. Strips any leftover routing
    (restoring the exact pre-routing config from the snapshot when present), stops an orphaned proxy,
    deletes the managed venv + bookkeeping, and clears the old `headroom`/`savings_level` settings.
    ``ctx`` is duck-typed: ``data_dir``, ``locked()``, ``load_state()``. Returns (did_work, msg)."""
    store = _data_dir(ctx)
    if not legacy_present(store):
        return False, "clean"
    # 1. undo routing: exact restore from snapshot beats a surgical strip (routing REPLACED user keys).
    restore_ok = True
    if has_backup(store):
        restore_ok, failures = restore_global(store)   # deletes the backup itself ONLY on full success
        if not restore_ok:
            _log(store, "restore failed", "\n".join(failures))
    if _any_injected():
        _unroute_all()
    # 2. Tear down the proxy + venv ONLY once the configs are provably clean. Stopping the proxy
    # while routing survives would point the tools at a dead port — strictly worse than leaving a
    # live proxy up until the next launch can finish unrouting. KEEP the config backup too: it's the
    # only exact copy of the user's original provider settings (our routing overwrote them), so a
    # later launch can retry. legacy_present() stays true either way, so cleanup is retried.
    routing_clean = restore_ok and not _any_injected()
    if routing_clean:
        stop_proxy(store)
        shutil.rmtree(_global_backup(store), ignore_errors=True)
        shutil.rmtree(hr_venv_dir(store), ignore_errors=True)
        for p in _leftover_files(store):
            try:
                p.unlink()
            except OSError:
                pass
    # 3. clear the old settings so the (removed) toggle can't linger as truthy metadata.
    try:
        with ctx.locked():
            s = ctx.load_state()
            changed = False
            for k in ("headroom", "savings_level", "headroom_event"):
                if k in s.settings():
                    s.settings().pop(k, None); changed = True
                if k in s.data:
                    s.data.pop(k, None); changed = True
            if changed:
                s.save()
    except Exception:
        pass
    # 4. Report honestly. If the config is still routed (restore failed, or a strip couldn't write),
    # say so rather than claim a clean removal — legacy_present() stays true (backup kept / still
    # injected) so the next launch / cx / cl retries.
    if not restore_ok or _any_injected():
        _log(store, "cleanup incomplete", f"restore_ok={restore_ok} injected={_any_injected()}")
        return True, "partially cleaned up legacy Headroom (config restore incomplete — will retry)"
    return True, "removed legacy Headroom routing, proxy, and files"


def _log(store: Path | None, label: str, text: str) -> None:
    from .util import now, iso
    try:
        p = (store or P.DATA_DIR) / "headroom-cleanup.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"\n[{iso(now())}] {label}\n{text}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass
