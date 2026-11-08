"""Subprocess environment hygiene — nothing to do with any single feature.

Every child process we spawn (the supervised codex/claude launchers, the Terminal login flow) must
run with a CLEAN interpreter environment. py2app injects PYTHONHOME/PYTHONPATH (and friends) pointing
at the FROZEN app's stripped, zipped stdlib; if those leak into a DIFFERENT interpreter (a system
python, a tool's own venv) it resolves stdlib against the app bundle and dies (e.g.
`ModuleNotFoundError: No module named 'uuid'`). Strip them so each child uses its own stdlib.

(These helpers formerly lived in the now-removed `headroom` module; they are launcher/terminal
infrastructure, not compression, so they live here.)
"""
from __future__ import annotations

import os
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from collections.abc import MutableMapping

# Interpreter-redirect vars py2app sets on the frozen app; must not leak into a child interpreter.
_PY_ENV_STRIP = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")

# Belt-and-suspenders: opt out of any third-party telemetry a spawned tool might honor. Local-only.
HARDENING_ENV = {"DO_NOT_TRACK": "1"}

SEAT_ENV_MARKER = "ACCTSW_SEAT_ENV"
SEAT_CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
SEAT_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", *SEAT_CA_VARS)


def shed_seat_env(environ: MutableMapping[str, str]) -> bool:
    """Undo an inherited SeatSwap environment in place, preserving subsequent user/boot edits.

    New sessions carry original values and digests of their overrides (never another copy of
    the seat proxy secret). Older sessions can only be recognized structurally; their user's
    original proxy/CA values are unrecoverable, so remove our proxy and session CA paths only.
    """
    marker = environ.pop(SEAT_ENV_MARKER, None)
    try:
        saved = json.loads(marker) if marker is not None else None
    except (ValueError, TypeError):
        saved = None
    records = {}
    if isinstance(saved, dict):
        for name, record in saved.items():
            if (name in SEAT_ENV_VARS and isinstance(record, dict)
                    and "original" in record
                    and (record["original"] is None or isinstance(record["original"], str))
                    and isinstance(record.get("digest"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", record["digest"])):
                records[name] = record
    if records:
        for name, record in records.items():
            current = environ.get(name)
            if current is None or hashlib.sha256(current.encode()).hexdigest() != record["digest"]:
                continue
            if record["original"] is None:
                environ.pop(name, None)
            else:
                environ[name] = record["original"]
        return True

    # No usable marker: the upgrade path for children of 1.6–1.8.0 sessions.
    changed = marker is not None
    proxies = set()
    for name in ("HTTPS_PROXY", "https_proxy"):
        value = environ.get(name)
        try:
            parts = urlsplit(value or "")
            if (parts.scheme != "http" or parts.username != "acctsw"
                    or parts.hostname not in ("127.0.0.1", "localhost", "::1")):
                continue
            parts.port  # reject malformed ports as well as malformed IPv6 URLs
        except ValueError:
            continue
        proxies.add(value)
        environ.pop(name, None)
        changed = True
    for name in ("ALL_PROXY", "all_proxy"):
        if environ.get(name) in proxies:
            environ.pop(name, None)
            changed = True
    from .sessionca import TEMP_DIR_PREFIX
    for name in SEAT_CA_VARS:
        value = environ.get(name)
        if value and Path(value).parent.name.startswith(TEMP_DIR_PREFIX):
            environ.pop(name, None)
            changed = True
    return changed


def harden_env(env: dict | None = None) -> dict:
    """Return env with the interpreter-redirect vars stripped (so a child python uses its own stdlib,
    not the frozen app's) + local-only hardening flags applied. Does not mutate the input."""
    e = dict(os.environ if env is None else env)
    for k in _PY_ENV_STRIP:
        e.pop(k, None)
    e.update(HARDENING_ENV)
    return e


# --- process-start identity (PID-reuse guard) --------------------------------------------------
#
# Both heartbeats (appalive, session) pin a PID to a process by recording `ps -o lstart=`. That
# string is LOCALE-FORMATTED: the menubar app runs under the C locale and writes
# "Sat Aug 29 16:36:54 2026", while a user's de_DE/fr_FR shell gets "Sa. 29 Aug. 16:36:54 2026"
# for the very same instant. Comparing them raw made every non-English shell conclude the PID had
# been recycled, so cx/cl exec'd the stock tool and supervision never ran. One shared implementation
# lives here so the two heartbeats can never drift apart again.

_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}")


# Every env var that can steer how a child formats dates/messages. LC_ALL outranks the rest, but we
# drop them all so nothing is left to argue with — including LANGUAGE, which gettext honours ahead of
# LANG on glibc systems.
_LOCALE_ENV_STRIP = ("LANG", "LANGUAGE")


def _c_locale_env() -> dict:
    """os.environ with every locale override removed and the C locale forced, so `ps` formats dates
    the same way for every user."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("LC_") and k not in _LOCALE_ENV_STRIP}
    e["LC_ALL"] = "C"
    e["LANG"] = "C"
    return e


def proc_start(pid: int) -> str | None:
    """The process's absolute start-time (a stable per-process identity that survives PID reuse),
    always in the C locale. Returns "" if no such process, or None if `ps` itself couldn't run."""
    if pid <= 0:
        return ""
    try:
        # The C locale stabilizes ps's format; malformed diagnostics must not break decoding.
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=2, env=_c_locale_env())
    except Exception:
        return None  # ps unavailable → caller falls back to bare liveness
    return r.stdout.strip()  # empty when the PID is not running


def _lstart_key(s: str) -> tuple[int, str, str] | None:
    """The locale-INDEPENDENT parts of an lstart string: (day-of-month, HH:MM:SS, year). Every
    locale renders those three identically; the weekday/month names and the FIELD ORDER are what
    move (de "Fr. 11 Sep. 23:16:24 2026", ru "пятница, 11 сентября 2026 г. 23:16:24"), so each field
    is found by shape anywhere in the string rather than by position.

    None whenever any field is missing or ambiguous — ja/zh render the date as "9/11", with no bare
    day number — and then callers fall back to demanding exact equality. Guessing is not an option
    here: a wrong match would claim a RECYCLED pid is still our process."""
    parts = s.split()
    clocks = [p for p in parts if _TIME_RE.fullmatch(p)]
    years = [p for p in parts if len(p) == 4 and p.isdigit()]
    days = [p for p in parts if p.isdigit() and 1 <= int(p) <= 31 and p not in years]
    if len(clocks) != 1 or len(years) != 1 or len(days) != 1:
        return None
    return int(days[0]), clocks[0], years[0]


def same_proc_start(stored: str, fresh: str) -> bool:
    """Do two lstart strings name the same process start? Exact match normally; otherwise fall back
    to the locale-independent parts.

    The fallback exists purely for the UPGRADE PATH: an app launched before this fix still holds a
    heartbeat written in whatever locale it ran under, and it keeps that value until it restarts.
    Without the fallback the first run after updating would still look like a recycled PID.
    Cost of the looser compare: two starts exactly one month apart, to the second, would collide —
    and only while a pre-fix heartbeat is on disk. Rejecting a live app is the far worse failure."""
    if stored == fresh:
        return True
    a, b = _lstart_key(stored), _lstart_key(fresh)
    return a is not None and a == b
