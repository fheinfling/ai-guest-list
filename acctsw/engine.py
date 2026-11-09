"""Which engine is this, and is another one sharing our store older?

The menubar app, the ``acctsw`` wrapper in ``~/.local/bin`` and every running ``cx``/``cl``
supervisor share one ``~/.account-switcher``. They are not guaranteed to be the same code: a
source checkout that was never pulled, a second copy of the app, or a hand-written wrapper can
leave terminals on an engine several releases behind the app. An old engine writing credentials in
an old format signs every session out, and nothing about that is visible from either side.

This module is the one place that knows how engines identify themselves (``__version__`` plus the
``--capabilities`` feature list) and how two identities compare. It is stdlib-only and never
touches the Keychain or state, so a probe of a foreign wrapper cannot have side effects here.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import __version__

# Feature handshake (``acctsw --capabilities``). Development builds can share a version while
# exposing different options, so callers that need a feature check for it, not for a version.
# ``engine-id-v1``: stamps its version into session heartbeats and state.json, and honours the
# shared-state writer guard (state.writer_block).
# ``engine-restart-v1``: its supervisors can restart themselves in the same terminal, resuming the
# conversation, once idle after a newer engine is installed. Whether one will is its ``self_restart``.
CAPABILITIES = ("run-key-v1", "isolated-signin-v1", "engine-id-v1", "engine-restart-v1")

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text) -> tuple[int, ...] | None:
    """``"1.4.2"`` → ``(1, 4, 2)``; tolerant of an ``acctsw `` prefix or a dev suffix.
    None for anything without a dotted number (callers treat that as unknown, never as older)."""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.search(text)
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()  # 1.4 == 1.4.0
    return tuple(parts)


def older_than(version, reference: str = __version__) -> bool:
    """True only when ``version`` is KNOWN and strictly older than ``reference``."""
    mine, theirs = parse_version(reference), parse_version(version)
    return mine is not None and theirs is not None and theirs < mine


def identity() -> dict:
    """This engine's self-description, as printed by ``acctsw --capabilities``."""
    return {"capabilities": list(CAPABILITIES), "version": __version__}


def compare(probe: dict | None) -> dict | None:
    """How a probed engine relates to this one: None when it is current (or unknowable).

    ``older`` — a lower version; ``missing`` — same version but lacking a feature this engine has
    (a development checkout behind the app's). A NEWER engine is not drift from our side: this
    engine is the stale one, which the state writer guard handles.
    """
    if not probe:
        return None
    version = probe.get("version")
    if parse_version(version) is None:
        return None
    caps = probe.get("capabilities")
    missing = sorted(set(CAPABILITIES) - set(caps if isinstance(caps, list) else []))
    if older_than(version):
        return {"reason": "older", "version": version, "missing": missing}
    if parse_version(version) == parse_version(__version__) and missing:
        return {"reason": "missing", "version": version, "missing": missing}
    return None


def probe(exe, *, timeout: float = 15.0) -> dict | None:
    """Ask the engine behind ``exe`` (e.g. ``~/.local/bin/acctsw``) who it is.

    Runs ``exe --capabilities`` and, for engines from before that handshake reported a version,
    ``exe --version``. Both exit in argparse before any Context, Keychain or store access, so this
    is safe to run against a wrapper we did not write. The environment is hardened (no leaked
    PYTHONHOME/PYTHONPATH from the app bundle), so we probe what a terminal would run. Returns
    ``{"version", "capabilities"}`` or None when the engine can't be identified (missing, not
    executable, crashed, timed out) — unknown is never reported as drift.
    """
    from .procenv import harden_env

    def run(flag: str) -> str | None:
        try:
            result = subprocess.run([str(exe), flag], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    env=harden_env(), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return result.stdout if result.returncode == 0 else None

    if not Path(exe).is_file():
        return None
    caps: list = []
    out = run("--capabilities")
    if out is not None:
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            caps = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else []
            if isinstance(payload.get("version"), str):
                return {"version": payload["version"], "capabilities": caps}
    out = run("--version")
    if out is None:
        return None
    m = re.search(r"acctsw\s+(\S+)", out)
    if not m or parse_version(m.group(1)) is None:
        return None
    return {"version": m.group(1), "capabilities": caps}


def _restarts_itself(session: dict) -> bool:
    return session.get("self_restart") is True


def drift_warnings(wrapper: dict | None, sessions: list[dict],
                   writer_block: str | None = None) -> list[dict]:
    """User-facing warnings for the popover/notifications, one per distinct problem.

    ``wrapper`` is install.wrapper_drift(), ``sessions`` session.stale_engines(), ``writer_block``
    the state guard's refusal for THIS engine. Each warning is ``{"kind", "text"}``; the wrapper
    one also names the bridge ``action`` that replaces it with this app's engine (with consent).
    Pure, so the exact wording is testable and identical for the banner and the toast.
    ``severity`` "hard" is a "needs you" alarm (banner, header count, toast); "soft" is a calm
    notice (banner only). A session that will restart itself (``self_restart``) gets nothing; an
    older session that writes logins safely is soft; app/wrapper drift and unsafe sessions are hard.
    """
    out = []
    if writer_block:
        out.append({"kind": "app", "severity": "hard", "text": f"AI Guest List is out of date: {writer_block}"})
    if wrapper:
        via = f" (via {wrapper['exe']})" if wrapper.get("exe") else ""
        out.append({
            "kind": "wrapper",
            "severity": "hard",
            "action": "adopt_engine",
            "text": (f"terminals run an older engine: {wrapper['path']}{via} is acctsw "
                     f"{wrapper['version']}, this app is {wrapper.get('app_version') or __version__}. "
                     "An older engine can sign your sessions out. Update it (in a source checkout: "
                     "git pull, then `acctsw install`), or switch terminals to this app's engine."),
        })
    manual = [s for s in sessions if not _restarts_itself(s)]
    if manual:
        # Only an engine below the credential floor, or too old to say, writes logins unsafely.
        # Anything else is a calm notice: it works, it just misses the latest switching.
        from .state import MIN_CREDENTIAL_WRITER
        unsafe, safe = [], []
        for s in manual:
            if not s.get("engine") or older_than(s["engine"], MIN_CREDENTIAL_WRITER):
                unsafe.append(s)
            else:
                safe.append(s)
        # Apply the name cap after putting the terminals needing action first; keep each order.
        manual = unsafe + safe
        why = ("so a seat switch can't sign you out" if unsafe
               else "when convenient to get the latest switching")
        if len(manual) == 1:
            s = manual[0]
            where = _session_where(s)
            text = (f"A {_terminal(s)}{f' ({where})' if where else ''} runs "
                    f"{_engine_words(s)}. Restart it {why}.")
        else:
            named = [_session_name(s) for s in manual[:SESSIONS_NAMED]]
            if len(manual) > SESSIONS_NAMED:
                named.append(f"+{len(manual) - SESSIONS_NAMED} more")
            text = (f"{len(manual)} terminals run an older engine than this app ({__version__}): "
                    f"{', '.join(named)}. Restart them {why}.")
        # Tool/PID identifies a terminal only for its process lifetime: PID reuse during this app
        # run can suppress a later toast. started_at is not a process identity: it moves on hops.
        out.append({"kind": "sessions", "severity": "hard" if unsafe else "soft", "text": text,
                    "notify_ids": sorted(f"{s.get('tool', '')}:{s['pid']}" for s in unsafe
                                         if isinstance(s.get("pid"), int))})
    return out


SESSIONS_NAMED = 2   # name this many stale terminals, then "+N more"
_TOOL_LABEL = {"claude": "Claude", "codex": "Codex"}


def _terminal(s: dict) -> str:
    label = _TOOL_LABEL.get(s.get("tool"), "")
    email = s.get("email") if isinstance(s.get("email"), str) else ""
    return f"{label + ' ' if label else ''}terminal{f' on {email}' if email else ''}"


def _engine_words(s: dict) -> str:
    return f"engine {s['engine']}" if s.get("engine") else "an older engine"


def _session_name(s: dict) -> str:
    details = ", ".join(filter(None, [_session_where(s), _engine_words(s)]))
    return f"{_terminal(s)} ({details})"


def _session_where(s: dict) -> str:
    """'started 19:23 in ~/Work/now' from the heartbeat, in local time; either half may be absent."""
    from .util import parse_iso
    parts = []
    started = parse_iso(s.get("started_at")) if isinstance(s.get("started_at"), str) else None
    if started is not None:
        parts.append(f"started {started.astimezone():%H:%M}")
    cwd = s.get("cwd")
    if isinstance(cwd, str) and cwd:
        home = Path.home()
        path = Path(cwd)
        try:
            rel = path.relative_to(home)
            shown = "~" if str(rel) == "." else f"~/{rel}"
        except ValueError:
            shown = cwd
        parts.append(f"in {shown}")
    return " ".join(parts)
