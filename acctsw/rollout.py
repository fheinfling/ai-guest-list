"""Structured limit signals read from Codex's own rollout log (the session JSONL).

Scraping the TUI's stdout is a guess: the model can *talk about* rate limits, and a redraw repeats
the same prose every frame. Codex itself writes the ground truth to
``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl`` — one JSON object per line, among
them ``token_count`` events carrying the live rate-limit windows and ``task_complete`` events
carrying the server's own error code. Reading that file turns "probably limited" into a fact, with
the reset time attached.

Two field-verified quirks drive the design:

* A RESUMED thread keeps appending to its ORIGINAL dated file — a 2026-09-06 path was seen with a
  2026-09-11 mtime. Discovery is therefore purely mtime-based (snapshot the tree before spawning,
  diff after) and never trusts the date in the path.
* ``credits.has_credits: false`` appears on perfectly healthy sessions (2%, 7%, 11% used). It is a
  plan attribute, not a limit signal, and must never be read as one.

Everything here is defensive: the file is written by another process and may be rotated, truncated
or caught half-written mid-line, and its schema is undocumented. Nothing raises on malformed input —
a watcher that dies on a stray byte would take the supervisor down with it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import usage as usage_mod
from .util import parse_iso

# Error codes the server itself attaches to a failed turn when the seat is out.
LIMIT_ERROR_CODES = frozenset({"usage_limit_exceeded", "rate_limit_exceeded"})

# On attach we re-read this much of the tail: the child may already have written the decisive event
# in the moment between spawn and discovery, and a limit event missed is a session stuck at 100%.
# Replayed history is harmless because every line is gated on its own timestamp.
TAIL_BYTES = 262_144
MAX_TICK_BYTES = 1 << 20      # per-poll read cap, so one huge append can't stall the supervisor
ATTACH_SCAN_INTERVAL_S = 1.0  # while unattached, walk the sessions tree at most this often

# Filesystem mtimes and our own wall clock have different granularity, and the child writes its
# first line a moment after we snapshot; allow this much slop before calling a file "untouched".
_SINCE_SLACK_NS = 2_000_000_000


@dataclass(frozen=True)
class RolloutSignal:
    """One decision-grade fact extracted from a single rollout line.

    ``hard`` separates "billing/entitlement is exhausted" (credits, spend control, the server's own
    limit error) from a plain window at 100%: only the latter comes back on its own at ``reset_at``.
    """
    kind: str                    # "limit" | "healthy"
    hard: bool = False
    reset_at: str | None = None  # ISO 8601, when the event carries one
    detail: str = ""             # human phrase for notifications / limit_detail
    at: str | None = None        # the line's own timestamp


# --- discovery ----------------------------------------------------------------------------------

def sessions_roots(ctx, email: str | None) -> list[Path]:
    """Where this seat's rollouts can appear: its per-account CODEX_HOME plus the real ~/.codex.

    Both, because a per-account home symlinks ``sessions`` to the shared dir — but only usually:
    an un-migrated or hand-edited home may have a real directory of its own. Resolved so the two
    collapse to one entry when the symlink is in place. Deliberately NOT ``paths.CODEX_SESSIONS``:
    that module-level constant ignores the Context and would point tests at the developer's
    real ~/.codex.
    """
    out: list[Path] = []
    bases = ([ctx.codex_home(email)] if email else []) + [ctx._codex_real]
    for base in bases:
        try:
            root = (Path(base) / "sessions").resolve()
        except OSError:
            continue
        if root not in out:
            out.append(root)
    return out


def scan_rollouts(roots: Iterable[Path]) -> dict[Path, int]:
    """``{rollout path: st_mtime_ns}`` for every rollout file under ``roots`` (recursive).

    ``os.scandir`` rather than ``rglob``: a busy user has ~1000 session files and this runs once per
    spawn and (until attached) once per second. Missing or unreadable directories are simply absent
    from the result — a fresh install has no sessions dir at all.
    """
    found: dict[Path, int] = {}
    stack = [Path(r) for r in roots]
    seen: set[Path] = set()
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                            found[Path(entry.path)] = entry.stat().st_mtime_ns
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def session_meta(path: Path) -> dict | None:
    """The ``session_meta`` payload from line 1 (it carries the thread id and the session's cwd).

    Only the first line is read: that is where Codex writes it, and the rest of the file can be
    megabytes. Returns None for anything unexpected — an empty file, a half-written first line, or
    a file that is not a rollout at all.
    """
    try:
        with open(path, "rb") as f:
            line = f.readline(1 << 20)
        obj = json.loads(line.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else None


def find_session_file(roots: Iterable[Path], *, before: dict[Path, int], cwd: str,
                      since_ns: int) -> tuple[Path | None, bool]:
    """Which rollout file is OUR child writing? — snapshot-then-diff, then match on cwd.

    ``before`` is a ``scan_rollouts`` taken just before spawning, so candidates are files that
    appeared, or grew, since then (which is how a resumed thread's old-dated file is caught). Among
    those, the one whose ``session_meta.cwd`` equals ours is the child. The second element says
    whether the match was unique: with two sessions running in the same directory we still attach
    (the newest wins) but the caller must not act destructively on an ambiguous read.

    The single-foreign-candidate fallback covers ``codex resume --last`` on a thread first started
    somewhere else: exactly one file is moving, so it is ours despite the cwd mismatch.
    """
    floor = since_ns - _SINCE_SLACK_NS
    matches: list[tuple[Path, int]] = []
    others: list[tuple[Path, int]] = []
    for path, mtime in scan_rollouts(roots).items():
        if mtime < floor:
            continue
        prev = before.get(path)
        if prev is not None and mtime <= prev:
            continue
        meta = session_meta(path)
        (matches if meta and meta.get("cwd") == cwd else others).append((path, mtime))
    if matches:
        best = max(matches, key=lambda pm: pm[1])[0]
        return best, len(matches) == 1
    if len(others) == 1:
        return others[0][0], False
    return None, False


# --- classification -----------------------------------------------------------------------------

def _pct(window: Any) -> float | None:
    value = window.get("used_percent") if isinstance(window, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reset_iso(value: Any) -> str | None:
    """A window's ``resets_at`` as ISO 8601. Rollouts write epoch seconds; a string is passed on."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def classify(obj: dict) -> RolloutSignal | None:
    """Turn one parsed rollout line into a signal, or None when it says nothing about limits.

    Pure and total: every branch tolerates missing keys, wrong types and ``rate_limits: null``.
    "Says nothing" is the common case — most lines are tool calls and token records — and is
    deliberately distinct from "healthy", which is a positive statement used to dismiss a
    false-positive stdout match.
    """
    if not isinstance(obj, dict) or obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    at = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
    kind = payload.get("type")

    if kind == "task_complete":
        error = payload.get("error")
        code = error.get("codex_error_info") if isinstance(error, dict) else None
        if isinstance(code, str) and code in LIMIT_ERROR_CODES:
            message = error.get("message")
            detail = message.strip() if isinstance(message, str) and message.strip() else code
            return RolloutSignal("limit", hard=True, detail=detail, at=at)
        return None

    if kind != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    primary, secondary = limits.get("primary"), limits.get("secondary")

    reached = limits.get("rate_limit_reached_type")
    if isinstance(reached, str) and reached.strip():
        # The server's own verdict (e.g. "workspace_member_credits_depleted"): entitlement, not a
        # window, so it is hard even when a primary window happens to carry a reset time.
        return RolloutSignal("limit", hard=True, reset_at=_reset_iso(
            primary.get("resets_at") if isinstance(primary, dict) else None),
            detail=reached.strip(), at=at)
    if limits.get("spend_control_reached"):
        return RolloutSignal("limit", hard=True, detail="spend control reached", at=at)

    windows = [w for w in (primary, secondary) if isinstance(w, dict)]
    pcts = [p for p in (_pct(w) for w in windows) if p is not None]
    maxed = [w for w in windows if (_pct(w) or 0.0) >= usage_mod.LIMIT_PCT]
    if maxed:
        # Both windows maxed → blocked until the LATER reset; taking min() would send the launcher
        # back to a still-capped seat (same rule as usage._limit_reset).
        resets = [r for r in (_reset_iso(w.get("resets_at")) for w in maxed) if r]
        return RolloutSignal("limit", hard=False, reset_at=max(resets) if resets else None,
                             detail="window at 100%", at=at)
    if pcts and max(pcts) < usage_mod.FALSE_ALARM_MAX_PCT:
        return RolloutSignal("healthy", at=at)
    return None  # windows still null (a fresh session reports none) — uninformative, not healthy


# --- the watcher --------------------------------------------------------------------------------

class RolloutWatcher:
    """Tails the rollout file of one supervised child, emitting signals as lines land.

    Attaching is lazy: the file does not exist until the child writes its first line, so each poll
    retries discovery (rate-limited, since it walks the whole sessions tree). Once attached it reads
    only the bytes appended since the last poll, so a signal is delivered exactly once, and it
    re-attaches from the tail if the file is truncated or replaced underneath it.
    """

    def __init__(self, roots: Iterable[Path], *, cwd: str, started_at: datetime,
                 before: dict[Path, int], clock: Callable[[], float] = time.monotonic):
        self._roots = [Path(r) for r in roots]
        self._cwd = cwd
        self._started_at = started_at
        self._before = dict(before)
        self._clock = clock
        self._since_ns = int(started_at.timestamp() * 1_000_000_000)
        self._next_scan: float | None = None
        self._inode: int | None = None
        self._offset = 0
        self._partial = b""
        self.attached: Path | None = None
        self.meta: dict = {}
        self.unambiguous = False

    # --- attach ---------------------------------------------------------------------------------
    def _try_attach(self) -> None:
        """Find our child's rollout — and keep looking while the answer is only a guess.

        An ambiguous attachment (two sessions in one directory, or the single-foreign-candidate
        fallback taken because our child had not written yet) is provisional: the file may not be
        ours at all. Since the caller treats an attachment as authority — it stops trusting the
        stdout banner — a wrong guess must be correctable, so discovery keeps running at the same
        rate limit until a file we can actually claim shows up.
        """
        now_t = self._clock()
        if self._next_scan is not None and now_t < self._next_scan:
            return
        self._next_scan = now_t + ATTACH_SCAN_INTERVAL_S
        path, unambiguous = find_session_file(self._roots, before=self._before, cwd=self._cwd,
                                              since_ns=self._since_ns)
        if path is None:
            return
        if path == self.attached:
            # Same file as before: only the confidence can have changed (the rival session went
            # quiet). Never re-seek — that would replay the tail and re-deliver signals we handled.
            self.unambiguous = unambiguous
            return
        if (self.attached is not None and not unambiguous
                and self.meta.get("cwd") == self._cwd):
            return  # we already hold a cwd match; another equally ambiguous guess is no improvement
        self.attached = path
        self.unambiguous = unambiguous
        self.meta = session_meta(path) or {}
        self._seek_tail(path)

    def _seek_tail(self, path: Path) -> None:
        """Position at the start of the last ``TAIL_BYTES``, aligned to a line boundary."""
        self._partial = b""
        st = os.stat(path)
        self._inode = st.st_ino
        start = max(0, st.st_size - TAIL_BYTES)
        if start:
            with open(path, "rb") as f:
                f.seek(start)
                head = f.read(TAIL_BYTES)
            cut = head.find(b"\n")
            start = st.st_size if cut < 0 else start + cut + 1
        self._offset = start

    # --- read -----------------------------------------------------------------------------------
    def _honoured(self, signal: RolloutSignal) -> bool:
        """Is this line from the CURRENT session? — the gate that makes the tail re-read safe.

        A resumed thread's file still holds the ``usage_limit_exceeded`` that ended the previous
        run; replaying it would hop seats the instant the session starts. Lines with no usable
        timestamp are honoured: we only ever read from the attach offset onwards, so they are at
        worst one tail window old, and dropping them would lose real signals on a schema change.
        """
        at = parse_iso(signal.at)
        return at is None or at >= self._started_at

    def _line_signal(self, raw: bytes) -> RolloutSignal | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            signal = classify(json.loads(raw.decode("utf-8")))
        except Exception:
            return None
        return signal if signal is not None and self._honoured(signal) else None

    def poll(self, *, force_attach: bool = False) -> list[RolloutSignal]:
        """Signals from bytes appended since the previous poll (never raises; [] when unattached)."""
        try:
            if force_attach:
                # A child that just exited may have created/flushed its session during the
                # discovery cooldown. Handoffs need one final lookup before choosing a thread.
                self._next_scan = None
            if self.attached is None or not self.unambiguous:
                self._try_attach()   # unattached, or attached only provisionally — keep looking
            if self.attached is None:
                return []
            return self._read()
        except Exception:
            return []

    def _read(self) -> list[RolloutSignal]:
        path = self.attached
        st = os.stat(path)
        if st.st_ino != self._inode or st.st_size < self._offset:
            self._seek_tail(path)  # rotated or truncated: our offset means nothing in the new file
            st = os.stat(path)
        if st.st_size <= self._offset:
            return []
        with open(path, "rb") as f:
            f.seek(self._offset)
            data = f.read(min(st.st_size - self._offset, MAX_TICK_BYTES))
        self._offset += len(data)
        chunks = (self._partial + data).split(b"\n")
        self._partial = chunks.pop()  # a line still being written; completed by a later poll
        out = []
        for raw in chunks:
            signal = self._line_signal(raw)
            if signal is not None:
                out.append(signal)
        return out

    def close(self) -> None:
        """No-op by construction: each poll opens and closes the file, so nothing outlives it."""
        self._partial = b""
