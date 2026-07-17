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
import contextlib
import fcntl
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


def _scan(tool: str) -> tuple[bool, bool]:
    """(injected, unknown) for one tool.

    ``injected`` is deliberately defined as "stripping would change this file" rather than as an
    independent marker scan. Detection and removal must agree by construction: anything we can detect
    but not strip would keep `legacy_present()` true forever, re-running the whole migration on every
    launch / cx / cl and reporting "partial" each time. (See INJECT_MARKERS for what routing wrote.)

    ``unknown`` means a file EXISTS but could not be inspected. That is not the same as clean:
    treating it as clean lets cleanup stop the proxy and delete the venv while the unreadable file is
    still routed, stranding the tool on a dead port with nothing left to undo it.
    """
    injected = unknown = False
    for cfg in _touched(tool):
        p = _edit_target(cfg)
        try:
            text = p.read_text()
        except FileNotFoundError:
            continue                            # genuinely not there → nothing to clean
        except UnicodeDecodeError:
            # Not valid UTF-8. `errors="ignore"` would DROP those bytes and the rewrite would then
            # persist the loss. Unknown: don't touch the file, and don't call it clean either.
            unknown = True
            continue
        except OSError:
            # Permission/stat failure. NOT "clean": `Path.exists()` also answers False here, which is
            # how an unreadable-but-routed file would sneak through as clean and get its proxy killed.
            unknown = True
            continue
        if tool == "codex":
            if _strip_codex_routing(text) != text:
                injected = True
        else:
            try:
                if _claude_routed(json.loads(text or "{}")):
                    injected = True
            except ValueError:
                # Malformed JSON may still hold routing we simply can't parse yet — once the user
                # fixes it, a torn-down proxy would leave them pointed at a dead port.
                unknown = True
    return injected, unknown


def _is_injected(tool: str) -> bool:
    return _scan(tool)[0]


def _any_injected() -> bool:
    return any(_is_injected(t) for t in ("codex", "claude"))


def _any_unknown() -> bool:
    """Any routing file we couldn't read? Blocks the "provably clean" teardown path."""
    return any(_scan(t)[1] for t in ("codex", "claude"))


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


# The provider keys `_route_codex` OVERWROTE. It stripped whatever the user had here and wrote its
# own, so their original values survive only in the snapshot — these are the only lines worth taking
# back out of it.
_CODEX_OWNED_KEYS = re.compile(
    r'(?m)^[ \t]*(model_provider|openai_base_url)[ \t]*=.*(?:\r?\n|\Z)')
# The exact values `_route_claude` assigned. Restoring is conditional on the live value STILL being
# these: if the user has since set their own, theirs wins — we only undo what we did.
_CLAUDE_INJECTED = {"ANTHROPIC_BASE_URL": _PROXY_URL, "ENABLE_TOOL_SEARCH": "true"}


def _codex_top_keys(text: str) -> set[str]:
    """Which provider keys already exist at top level in ``text``."""
    hdr = _TOML_TABLE_HDR.search(text)
    head = text[:hdr.start()] if hdr else text
    return {m.group(1) for m in _CODEX_OWNED_KEYS.finditer(head)}


def _original_codex_keys(store: Path | None, present: set[str]) -> str:
    """The user's own top-level provider lines as they were BEFORE routing, from the snapshot —
    EXCLUDING any key that already survives in the live file.

    Restoring unconditionally duplicates keys, which is invalid TOML. Two real ways a key survives:
    the retired writer's strip required a trailing newline, so an original key at EOF was never
    removed and is still in the live file; and the user may have edited the routed value themselves,
    which our exact-value strip (correctly) leaves alone. In both cases the live document already
    answers the question and the snapshot must not overrule it.
    """
    text = _snapshot_text(store, P.CODEX_HOME / "config.toml")
    if text is None:
        return ""
    hdr = _TOML_TABLE_HDR.search(text)
    head = text[:hdr.start()] if hdr else text
    out = []
    for m in _CODEX_OWNED_KEYS.finditer(head):
        if m.group(1) in present:
            continue                                    # live already has it → never duplicate
        # The pattern ends `(?:\r?\n|\Z)`, so a snapshot whose final line has no trailing newline is
        # captured without one — prepending that would WELD it onto the user's first line and write
        # invalid TOML into their config. Normalise line endings while we're here.
        out.append(m.group(0).rstrip("\r\n") + "\n")
    return "".join(out)


def _unroute_codex(store: Path | None = None) -> None:
    from .util import atomic_write_text
    path = _edit_target(P.CODEX_HOME / "config.toml")
    if not path.exists():
        return
    try:
        content = path.read_text()      # strict, matching _scan: bad bytes are "unknown", not ours
    except (OSError, UnicodeDecodeError):
        return
    stripped = _strip_codex_routing(content)
    if stripped == content:
        return                                          # nothing of ours in here — don't touch the file
    # Three-way merge, NOT a whole-file restore: keep the CURRENT file (every edit the user has made
    # since enabling), drop our routing, and take back from the snapshot only the provider keys our
    # routing overwrote AND that the live file no longer carries. Replaying the whole snapshot would
    # revert months of unrelated edits — and delete the file outright if it was recorded "absent".
    merged = _original_codex_keys(store, _codex_top_keys(stripped)) + stripped
    atomic_write_text(path, merged if merged.strip() else "")


def _claude_routed(payload) -> bool:
    """Is this settings payload carrying OUR routing? The single predicate behind both detection and
    removal — a non-dict payload that merely CONTAINS the proxy URL is not routing we can strip, and
    must not be reported as dirty (it would never converge)."""
    if not isinstance(payload, dict):
        return False
    env = payload.get("env")
    return isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == _PROXY_URL


def _original_claude_env(store: Path | None, path: Path) -> dict:
    """The user's own values for the env keys routing overwrote, as they were BEFORE routing."""
    text = _snapshot_text(store, path)
    if text is None:
        return {}
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        return {}
    env = payload.get("env") if isinstance(payload, dict) else None
    if not isinstance(env, dict):
        return {}
    return {k: env[k] for k in _CLAUDE_INJECTED if k in env}


def _unroute_claude_file(path: Path, store: Path | None = None) -> None:
    from .util import atomic_write_text
    orig_path = path
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
    # Three-way merge, per key. `_route_claude` assigned into env unconditionally, so the user's own
    # value survives only in the snapshot — but blanket pop-then-restore also discards any value they
    # set THEMSELVES since. Only undo a key that still holds exactly what we injected; if they have
    # since changed it, that is their decision and it stands.
    original = _original_claude_env(store, orig_path)
    for k, injected in _CLAUDE_INJECTED.items():
        if env.get(k) != injected:
            continue                                    # not ours any more → leave it alone
        env.pop(k, None)
        if k in original:
            env[k] = original[k]
    if env:
        payload["env"] = env
    else:
        payload.pop("env", None)
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _unroute_claude(store: Path | None = None) -> None:
    for path in _touched("claude"):     # settings.json + settings.local.json
        _unroute_claude_file(path, store)


def _unroute_all(store: Path | None = None) -> None:
    _unroute_codex(store)
    _unroute_claude(store)


# --- the pre-routing config snapshot the old enable path saved -------------------------------------

def _global_backup(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-global-backup"


def has_backup(store: Path | None = None) -> bool:
    """Is there a snapshot with anything usable in it? An EMPTY manifest is not a backup — treating
    it as one keeps legacy_present() true forever and re-runs the migration on every launch."""
    try:
        manifest = json.loads((_global_backup(store) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(manifest) if isinstance(manifest, dict) else False


_VALID_KINDS = ("file", "symlink", "absent")

# The ONLY files the old routing ever WROTE: `_route_codex` → ~/.codex/config.toml, `_route_claude`
# → ~/.claude/settings.json. The snapshot, however, captured the wider "files `headroom wrap` may
# touch" set — AGENTS.md, CLAUDE.md, .mcp.json, settings.local.json. Restoring those is pure
# regression: routing never modified them, so their CURRENT state is already the correct one.
# Replaying a months-old snapshot over them silently reverts the user's work, and an entry recorded
# as "absent" (the file did not exist when save-credit was first enabled) DELETES a CLAUDE.md /
# AGENTS.md they have written since. Restore is therefore restricted to what we actually broke.
_ROUTING_OWNED_NAMES = frozenset({"config.toml", "settings.json"})


def _snapshot_text(store: Path | None, want: Path) -> str | None:
    """The ORIGINAL text of ``want`` from the snapshot, or None if absent/unusable.

    Read-only by design. The snapshot is NOT replayed onto disk: it captured whole files at the
    moment save-credit was first enabled, so writing one back reverts every unrelated edit the user
    has made since — and an entry recorded "absent" would delete a file they created later. It is
    consulted only for the handful of keys routing overwrote; the live file supplies everything else.
    """
    bdir = _global_backup(store)
    mf = bdir / "manifest.json"
    try:
        manifest = json.loads(mf.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    for i, path_s in manifest.items():
        if not isinstance(path_s, str) or Path(path_s) != want:
            continue
        if not re.fullmatch(r"\d+", str(i)):
            return None     # the id indexes a sibling file; "../../x" would read outside the backup
        try:
            entry = json.loads((bdir / f"{i}.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("kind") != "file":
            return None                                 # absent/symlink → no original text to take
        # The snapshot recorded the path in the entry too; require agreement so a corrupted manifest
        # can't hand us another file's contents to merge into this one.
        if entry.get("path") != path_s:
            return None
        b64 = entry.get("b64")
        if not isinstance(b64, str):
            return None
        try:
            # validate=True: the default silently DISCARDS non-alphabet chars, so a corrupt payload
            # would decode to b"" and read as "the user had no provider keys".
            return base64.b64decode(b64, validate=True).decode("utf-8", errors="ignore")
        except ValueError:
            return None
    return None


# --- stopping an orphaned proxy by its PID file ----------------------------------------------------

def _proxy_pidfile(store: Path | None) -> Path:
    return (store or P.DATA_DIR) / "headroom-proxy.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                        # ESRCH — definitively gone
    except PermissionError:
        return True                         # EPERM — it EXISTS, just isn't ours to signal
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
    # The proxy was a pip console script, so it ran via its shebang and `ps` shows the INTERPRETER as
    # argv[0]: `<python> .../hr-venv/bin/headroom proxy --host ...`. Requiring argv[0] itself to be
    # `headroom` would classify the real proxy as foreign — dropping its PID file and deleting its
    # venv while it kept running. Find the `headroom` token wherever it sits, then require `proxy` as
    # a later argument, which still rejects bystanders like `tail -f headroom-proxy.log`.
    for idx, tok in enumerate(parts):
        # EXACT basename, not a substring: `headroom-worker`/`headroom-proxy.log` are not our binary,
        # and a substring match would SIGKILL one holding a recycled PID.
        if Path(tok).name.lower() not in ("headroom", "headroom.py"):
            continue
        # And it must plausibly BE the executable: argv[0], or a path to it. A bare `headroom` word
        # sitting in someone's arguments (`python worker.py headroom proxy`) is not our process.
        if idx != 0 and "/" not in tok:
            continue
        return any(a.lower() == "proxy" for a in parts[idx + 1:])
    return False


def _proxy_pid(store: Path | None) -> int:
    """The tracked PID: 0 if there is no PID file at all, -1 if one EXISTS but is unusable.

    The distinction matters. The old writer wrote this file non-atomically, so a hard kill could
    leave it empty or truncated while the proxy itself is alive. Collapsing that to "no proxy" makes
    cleanup delete the venv out from under a live process and forget it forever; -1 keeps it tracked.
    """
    try:
        raw = _proxy_pidfile(store).read_text()
    except FileNotFoundError:
        return 0
    except OSError:
        return -1
    try:
        pid = int(raw.strip())
    except ValueError:
        return -1
    return pid if pid > 0 else -1


def _forget_proxy(store: Path | None) -> None:
    try:
        _proxy_pidfile(store).unlink()
    except OSError:
        pass


def stop_proxy(store: Path | None = None, *, kill=None, sleep=None) -> bool:
    """Stop the old proxy (by PID file). Returns True only when the proxy is CONFIRMED gone (dead,
    never there, or the PID definitively belongs to someone else); False when we could not verify or
    could not stop it.

    A False result means the caller must keep BOTH the PID file and the venv: dropping them while a
    real proxy is alive leaves it listening forever with nothing left to track or reap it by. Kills
    only a PID that is alive AND verifiably our proxy, re-checked immediately before every signal so
    a number recycled during the wait is never hit.
    """
    import signal
    import time
    _kill = kill or os.kill
    _sleep = sleep or time.sleep
    pid = _proxy_pid(store)
    if pid == -1:
        return False        # PID file exists but is unusable → a live proxy may be untrackable; keep
    if pid == 0 or not _pid_alive(pid):
        _forget_proxy(store)
        return True                                     # nothing tracked, or already dead
    ident = _pid_is_proxy(pid)
    if ident is None:
        return False                                    # can't tell → keep tracking, retry later
    if ident is False:
        _forget_proxy(store)
        return True                                     # stale PID owned by someone else → let go
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _pid_alive(pid):
            break
        if _pid_is_proxy(pid) is not True:
            return False                                # identity changed mid-flight → do not signal
        try:
            _kill(pid, sig)
        except OSError:
            return False                                # couldn't signal → keep tracking
        _sleep(0.3)
    if _pid_alive(pid) and _pid_is_proxy(pid) is not False:
        return False                                    # survived → keep tracking
    _forget_proxy(store)
    return True


# --- the managed venv + bookkeeping the old feature created ----------------------------------------

def hr_venv_dir(store: Path | None = None) -> Path:
    return (store or P.DATA_DIR) / "hr-venv"


def _oplock_file(store: Path | None) -> Path:
    """The lock the OLD build took around its own enable/disable operations. The migration takes it
    too, so an old menubar still running through an upgrade cannot re-inject routing in the gap
    between our scan and our teardown — it would come back with the snapshot and proxy already gone.
    Deliberately NOT swept: another process may be holding this inode."""
    return (store or P.DATA_DIR) / ".headroom-oplock"


def _leftover_files(store: Path | None) -> list[Path]:
    """Inert bookkeeping the old feature left behind. NB: the proxy PID file is deliberately NOT
    here — `stop_proxy` owns its lifecycle and keeps it when it cannot prove the PID is ours, so
    sweeping it here would delete the only handle on a possibly-live proxy and make it untrackable.
    Nor is the oplock: deleting a lock file out from under a holder defeats the lock."""
    d = store or P.DATA_DIR
    return [
        d / "headroom-proxy.log", d / "headroom.log",
        d / "rtk.sha256", d / "headroom-baseline-seeded",
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
    if _proxy_pid(store) != 0 or hr_venv_dir(store).exists():
        return True          # != 0 covers -1: an existing-but-unusable PID file still needs reaping
    return any(p.exists() for p in _leftover_files(store))


class _Busy(Exception):
    """Another process holds the migration lock."""


@contextlib.contextmanager
def _oplocked(store: Path | None):
    """NON-BLOCKING exclusive hold of the legacy operation lock.

    Non-blocking on purpose: this sits on the `cx`/`cl` launch path, and the whole point of the app
    being the master switch is that launching never stalls. If another process is already migrating,
    we simply let it — the work is idempotent and its result is the same as ours would be.
    """
    d = store or P.DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = open(_oplock_file(d), "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise _Busy() from e                        # EWOULDBLOCK: genuinely held by someone else
        # Any OTHER OSError (EINTR, locks unsupported on this fs) is a real failure, not contention —
        # it propagates to the caller's "deferred" path rather than masquerading as a clean result.
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def cleanup_legacy(ctx) -> tuple[bool, str]:
    """One-time, idempotent teardown of the retired Headroom feature. Strips any leftover routing
    (restoring the exact pre-routing config from the snapshot when present), stops an orphaned proxy,
    deletes the managed venv + bookkeeping, and clears the old `headroom`/`savings_level` settings.
    ``ctx`` is duck-typed: ``data_dir``, ``locked()``, ``load_state()``. Returns (did_work, msg).

    The migration serialises on the LEGACY oplock, not on ctx.locked(). Two reasons: the old build
    took that same lock around its enable/disable, so an old menubar still running through an upgrade
    can't re-inject routing in the gap between our scan and our teardown; and ctx.locked() is the
    hot state lock that usage polls hold across network calls — blocking on it here would stall
    `cx`/`cl` at launch for as long as a poll takes. We take ctx.locked() only for the state write.
    """
    store = _data_dir(ctx)
    if not legacy_present(store):
        return False, "clean"
    try:
        with _oplocked(store):
            return _cleanup_locked(ctx, store)
    except _Busy:
        # Someone else is mid-migration (or an old app is mid-operation). Theirs will finish it;
        # never block the launch waiting. NOT "clean" — nothing here verified the config, and callers
        # (notably uninstall --purge) must not read this as "safe to delete the recovery data".
        return False, "another process is cleaning up legacy Headroom"
    except Exception as e:
        # Migration blew up. Report "deferred", never "clean": nothing was torn down, so a later
        # launch must retry — and callers must not be told the config is fine.
        _log(store, "cleanup could not run", repr(e))
        return False, "deferred legacy Headroom cleanup (will retry on the next launch)"


def _cleanup_locked(ctx, store: Path) -> tuple[bool, str]:
    if not legacy_present(store):
        return False, "clean"                           # another process won the race and finished
    # 1. Undo routing by MERGING, not replaying: strip our keys from the live file and take back only
    # the values routing overwrote from the snapshot. Everything else in the file is the user's.
    if _any_injected():
        _unroute_all(store)
    # 2. Tear down the proxy + venv + snapshot ONLY once the configs are provably clean. Stopping the
    # proxy while routing survives would point the tools at a dead port — strictly worse than leaving
    # a live proxy up until the next launch can finish unrouting. "Provably" excludes `unknown`: a
    # file that exists but can't be read is not clean, and treating it as clean strands it on a dead
    # port. The snapshot holds the only copy of the provider values routing overwrote, so it outlives
    # any incomplete run. legacy_present() stays true either way, so cleanup is retried.
    injected, unknown = (_any_injected(), _any_unknown())
    routing_clean = not injected and not unknown
    if routing_clean:
        # The venv is what a surviving proxy is RUNNING FROM — delete it only once the process is
        # confirmed gone, or we strand a live proxy with its executable pulled out from under it.
        proxy_gone = stop_proxy(store)
        shutil.rmtree(_global_backup(store), ignore_errors=True)
        if proxy_gone:
            shutil.rmtree(hr_venv_dir(store), ignore_errors=True)
            for p in _leftover_files(store):
                try:
                    p.unlink()
                except OSError:
                    pass
    # 3. clear the old settings so the (removed) toggle can't linger as truthy metadata. This is the
    # ONLY part that needs the state lock, and it's a quick read-modify-write — exactly what
    # ctx.locked() is for. (We hold the oplock, not the state lock, so there's no self-deadlock.)
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
    # 4. Report honestly. If anything is still routed/unreadable (restore failed, or a strip couldn't
    # write), say so rather than claim a clean removal — legacy_present() stays true (backup kept /
    # still injected) so the next launch / cx / cl retries.
    if not legacy_present(store):
        return True, "removed legacy Headroom routing, proxy, and files"
    _log(store, "cleanup incomplete", f"injected={injected} unknown={unknown}")
    return True, "partially cleaned up legacy Headroom (will retry on the next launch)"


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
