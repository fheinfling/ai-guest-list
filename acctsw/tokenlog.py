"""Offline incremental token index and the v1 usage-history payload.

Only complete appended lines are consumed. Offsets, deduplication and aggregates commit in
one transaction, so a deadline or process exit can never publish an offset ahead of its counts.
"""
from __future__ import annotations

import bisect
import calendar
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, time as daytime, timedelta
from pathlib import Path

from . import rollout, seatledger

SCHEMA_VERSION = "1"
_LOCK = threading.Lock()
_CHUNK = 1 << 20
_WEEK = 7 * 86400
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
 path TEXT PRIMARY KEY, dev INTEGER, ino INTEGER, size INTEGER, offset INTEGER,
 mtime_ns INTEGER, tool TEXT, thread TEXT, model TEXT, creator_user TEXT, creator_account TEXT);
CREATE TABLE IF NOT EXISTS codex_threads (
 thread TEXT PRIMARY KEY, input INTEGER, cached INTEGER, cache_write INTEGER,
 output INTEGER, reasoning INTEGER);
CREATE TABLE IF NOT EXISTS codex_seen (h INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS claude_seen (h INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS usage (
 hour INTEGER, tool TEXT, model TEXT, seat TEXT, input INTEGER, cache_read INTEGER,
 cache_write INTEGER, output INTEGER, PRIMARY KEY(hour,tool,model,seat)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS windows (
 seat TEXT, ends_at INTEGER, peak_pct REAL, observed INTEGER,
 PRIMARY KEY(seat,ends_at)) WITHOUT ROWID;
"""


def _connect(ctx):
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.data_dir / "usage.sqlite"
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    if version is not None and version[0] != SCHEMA_VERSION:
        # Rebuild in the same database transaction. Keeping the inode avoids unlinking a WAL
        # another reader still has open. Every table contains derived, disposable data only.
        for table in ("files", "codex_threads", "codex_seen", "claude_seen", "usage", "windows", "meta"):
            db.execute("DROP TABLE IF EXISTS " + table)
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    db.commit()
    os.chmod(path, 0o600)
    return db


def _timestamp(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            datetime.fromtimestamp(value)  # reject counters masquerading as out-of-range epochs
            return float(value)
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _hash(value):
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big") & ((1 << 63) - 1)


def _discover(ctx, state):
    roots = set(rollout.sessions_roots(ctx, None))
    for email in state.accounts("codex"):
        roots.update(rollout.sessions_roots(ctx, email))
    claude_roots = {ctx._claude_real / "projects"}
    # Key homes include per-terminal pins, which are not enumerated in the seat registry.
    # Inspect only transcript directories; never prepare a home or read its credentials.
    try:
        for home in ctx._homes_root.iterdir():
            if home.is_dir() and not home.is_symlink():
                roots.add(home / "sessions")
                claude_roots.add(home / "projects")
    except OSError:
        pass

    def canonical(paths):
        # Seat homes link `sessions`/`projects` to the shared dirs. Walk each real directory once,
        # under its real path, so a file is never indexed twice or keyed by a seat-home alias.
        out = set()
        for path in paths:
            try:
                out.add(path.resolve())
            except OSError:
                continue
        return out

    roots, claude_roots = canonical(roots), canonical(claude_roots)
    found = {p: "codex" for p in rollout.scan_rollouts(roots)}
    stack = list(claude_roots)
    seen = set()
    while stack:
        directory = stack.pop()
        try:
            resolved = directory.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.name.endswith(".jsonl") and entry.is_file():
                        found[Path(entry.path)] = "claude"
        except OSError:
            continue
    return found


def _ledger(ctx):
    lookup = {}
    tracking = None
    for row in seatledger.read(ctx.data_dir):
        at = _timestamp(row["at"])
        if at is None:
            continue
        if row["tool"] == "claude" and tracking is None:
            tracking = (at, row["at"])
        thread = row.get("thread")
        if row["event"] == "bind" and isinstance(thread, str) and thread:
            lookup.setdefault((row["tool"], thread), []).append((at, row["email"]))
    for binds in lookup.values():
        binds.sort(key=lambda bind: bind[0])
    return lookup, tracking[1] if tracking else None


def _seat(tool, thread, at, ledger, creators, user=None, account=None):
    binds = ledger.get((tool, thread), [])
    # bisect on times only: equal-time binds retain append order (the final hop wins).
    index = bisect.bisect_right(binds, at + 120, key=lambda bind: bind[0]) - 1
    if index >= 0:
        return tool + ":" + binds[index][1]
    if tool == "codex" and user:
        matches = creators.get(user, [])
        if matches:
            email = next((email for email, workspace in matches if account and workspace == account),
                         matches[0][0])
            return "codex:" + email
    return tool + ":?"


def _window(db, seat, end, peak):
    end = _timestamp(end)
    if (end is None or isinstance(peak, bool) or not isinstance(peak, (int, float))
            or not math.isfinite(peak)):
        return
    db.execute("""INSERT INTO windows VALUES (?,?,?,1) ON CONFLICT(seat,ends_at)
                  DO UPDATE SET peak_pct=max(peak_pct,excluded.peak_pct), observed=1""",
               (seat, int(end), float(peak)))


def _add(db, at, tool, model, seat, counts):
    if not any(counts):
        return
    db.execute("""INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(hour,tool,model,seat) DO UPDATE SET
        input=input+excluded.input, cache_read=cache_read+excluded.cache_read,
        cache_write=cache_write+excluded.cache_write, output=output+excluded.output""",
               (int(at // 3600), tool, model, seat, *counts))


def _counts(usage, keys):
    values = [usage.get(key, 0) for key in keys]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise ValueError("invalid token counters")
    return values


def _line(db, raw, file, ledger, creators):
    tool = file["tool"]
    if tool == "claude":
        if b'"usage"' not in raw:
            return
    elif not any(key in raw for key in (b'token_count', b'turn_context', b'session_meta')):
        return
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return
        at = _timestamp(obj.get("timestamp"))
        if tool == "claude":
            message = obj.get("message")
            if obj.get("type") != "assistant" or not isinstance(message, dict):
                return
            usage, model = message.get("usage"), message.get("model")
            mid, request = message.get("id"), obj.get("requestId")
            if (not isinstance(usage, dict) or not isinstance(model, str)
                    or model == "<synthetic>" or not isinstance(mid, str)
                    or not isinstance(request, str) or at is None):
                return
            counts = _counts(usage, ("input_tokens", "cache_read_input_tokens",
                                     "cache_creation_input_tokens", "output_tokens"))
            h = _hash(mid + "|" + request)
            if not db.execute("INSERT OR IGNORE INTO claude_seen VALUES (?)", (h,)).rowcount:
                return
            seat = _seat(tool, obj.get("sessionId"), at, ledger, creators)
            _add(db, at, tool, model, seat, counts)
            return
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        kind = obj.get("type")
        if kind == "session_meta":
            for source, target in (("id", "thread"), ("creator_user_id", "creator_user"),
                                   ("creator_account_id", "creator_account")):
                value = payload.get(source)
                if isinstance(value, str):
                    file[target] = value
            if file.get("creator_user") and at is not None:
                db.execute("""INSERT INTO meta VALUES ('tracking_codex',?)
                    ON CONFLICT(key) DO UPDATE SET value=CAST(min(CAST(value AS REAL),
                    CAST(excluded.value AS REAL)) AS TEXT)""", (str(at),))
            return
        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                file["model"] = payload["model"]
            return
        if kind != "event_msg" or payload.get("type") != "token_count" or at is None:
            return
        thread = file.get("thread")
        if not thread:
            return
        seat = _seat(tool, thread, at, ledger, creators,
                     file.get("creator_user"), file.get("creator_account"))
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            for window in limits.values():
                if isinstance(window, dict) and window.get("window_minutes") == 10080:
                    _window(db, seat, window.get("resets_at"), window.get("used_percent"))
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(usage, dict):
            return
        total = _counts(usage, ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                "output_tokens", "reasoning_output_tokens"))
        # A copied history can contain a smaller total than the current high-water mark.
        # Dedupe the original event before deciding that a lower total is a process reset.
        h = _hash(json.dumps([thread, at, total], separators=(",", ":")))
        if not db.execute("INSERT OR IGNORE INTO codex_seen VALUES (?)", (h,)).rowcount:
            return
        old = db.execute("SELECT input,cached,cache_write,output,reasoning FROM codex_threads"
                         " WHERE thread=?", (thread,)).fetchone()
        reset = old is not None and total[0] + total[3] < old[0] + old[3]
        delta = total if old is None or reset else [max(0, v - p) for v, p in zip(total, old)]
        db.execute("INSERT OR REPLACE INTO codex_threads VALUES (?,?,?,?,?,?)", (thread, *total))
        # Codex's input includes its cached input and output includes reasoning, so reasoning
        # (delta[4]) is not added again; it lives only in the thread's high-water mark.
        fresh = max(0, delta[0] - delta[1] - delta[2])
        _add(db, at, tool, file.get("model") or "unknown", seat,
             [fresh, delta[1], delta[2], delta[3]])
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        # Bad records must not poison a growing file or consume an incomplete final line.
        return


def refresh(ctx, *, budget_s: float):
    if not _LOCK.acquire(blocking=False):
        return
    db = None
    try:
        deadline = time.monotonic() + max(0, budget_s)
        state = ctx.load_state()
        ledger, _ = _ledger(ctx)
        creators = {}
        for email, seat in sorted(state.accounts("codex").items()):
            user = seat.get("account_id")
            if isinstance(user, str):
                creators.setdefault(user, []).append((email, seat.get("workspace_id")))
        db = _connect(ctx)
        for tool in ("codex", "claude"):
            seats = list(state.accounts(tool).items()) + [
                (seat["id"], seat) for seat in state.data.get("keys", {}).values()
                if seat.get("harness") == tool]
            for email, seat in seats:
                window = ((seat.get("usage") or {}).get("windows") or {}).get("weekly")
                if isinstance(window, dict):
                    _window(db, tool + ":" + email, window.get("resets_at"), window.get("used_pct"))
        discovered = _discover(ctx, state)
        files = []
        db.row_factory = sqlite3.Row
        for path, tool in discovered.items():
            try:
                st = path.stat()
            except OSError:
                continue
            previous = db.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
            if (previous is None or previous["dev"] != st.st_dev or previous["ino"] != st.st_ino
                    or st.st_size < previous["offset"]):
                file = dict(path=str(path), dev=st.st_dev, ino=st.st_ino, size=st.st_size,
                            offset=0, mtime_ns=st.st_mtime_ns, tool=tool, thread=None,
                            model=None, creator_user=None, creator_account=None)
            else:
                file = dict(previous)
                file.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            files.append(file)
            _save_file(db, file)
        present = {file["path"] for file in files}
        for row in db.execute("SELECT path FROM files").fetchall():
            if row[0] not in present:
                db.execute("DELETE FROM files WHERE path=?", (row[0],))
        db.commit()
        # New files and old appends are both eligible, independent of their dated directory.
        for file in files:
            if time.monotonic() >= deadline:
                break
            try:
                with open(file["path"], "rb") as stream:
                    stream.seek(file["offset"])
                    while file["offset"] < file["size"] and time.monotonic() < deadline:
                        raw = stream.read(min(_CHUNK, file["size"] - file["offset"]))
                        cut = raw.rfind(b"\n") + 1
                        if cut == 0:
                            # Very large records are uncommon, but cannot strand the index.
                            tail = stream.readline()
                            if not tail.endswith(b"\n"):
                                break
                            raw += tail
                            cut = len(raw)
                        for line in raw[:cut].splitlines():
                            _line(db, line, file, ledger, creators)
                        file["offset"] += cut
                        stream.seek(file["offset"])
                        _save_file(db, file)
                        db.commit()
            except OSError:
                continue
    finally:
        if db is not None:
            db.close()
        _LOCK.release()


def _save_file(db, file):
    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               tuple(file[key] for key in ("path", "dev", "ino", "size", "offset", "mtime_ns",
                                           "tool", "thread", "model", "creator_user", "creator_account")))


# "Done" means the history backlog is read, not that every byte is: live transcripts grow all the
# time, and an exact test would keep the view in its "reading history…" state (and polling) for as
# long as any session is writing. Each history request reads the fresh tail anyway.
_BACKLOG = 8 << 20


def _progress(db):
    total, consumed = db.execute("SELECT coalesce(sum(size),0),coalesce(sum(offset),0) FROM files").fetchone()
    done = total - consumed <= _BACKLOG
    return {"done": done, "pct": 100 if done else min(99, int(100 * consumed / total))}


def index_progress(ctx):
    db = _connect(ctx)
    try:
        return _progress(db)
    finally:
        db.close()


def _now():
    return datetime.now().astimezone()


def _midnight(day):
    # Resolve each local date separately: datetime.astimezone() returns a fixed-offset tzinfo.
    return datetime.combine(day, daytime()).astimezone()


def _month(day, offset, anchor=1):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    month += 1
    return date(year, month, min(anchor, calendar.monthrange(year, month)[1]))


def _calendar_bounds(now):
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    return {
        "week": [(_midnight(monday - timedelta(days=7*i)),
                  _midnight(monday + timedelta(days=7*(1-i)))) for i in range(13)],
        "month": [(_midnight(_month(today, -i)), _midnight(_month(today, 1-i))) for i in range(6)],
    }


def _label(start, end, month=False):
    if month:
        return _MONTHS[start.month - 1]
    last = (end - timedelta(days=1)).date()
    return f"{_MONTHS[start.month-1][:3]} {start.day} – {_MONTHS[last.month-1][:3]} {last.day}"


def _model_key(model):
    if model.startswith("claude-"):
        tier = next((i for i, word in enumerate(("opus", "sonnet", "haiku")) if word in model), 3)
        return (0, tier, model)
    return (1, 0, model)


def _model_label(model):
    match = re.fullmatch(r"claude-(opus|sonnet|haiku)-(\d+(?:-\d+)*?)(?:-\d{8})?", model)
    return match[1].title() + " " + match[2].replace("-", ".") if match else model


def _billing(ctx, seat, raw, now, bounds):
    if seat["kind"] == "key":
        return {"source": "calendar", "renews_day": 1, "bounds": bounds["month"]}
    anchor, source = None, None
    if seat["tool"] == "codex":
        billing = raw.get("billing") or {}
        anchor, source = billing.get("renews_at"), "codex_token"
    elif seat["kind"] == "sub":
        try:
            email = seat["key"].split(":", 1)[1]
            profile = json.loads((ctx.claude_home(email) / ".claude.json").read_bytes())
            oauth = profile.get("oauthAccount") or {}
            address = oauth.get("emailAddress")
            if isinstance(address, str) and address.casefold() == email.casefold():
                anchor, source = oauth.get("subscriptionCreatedAt"), "claude_profile"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if not isinstance(anchor, str) or _timestamp(anchor) is None:
        return None
    # Keep the original anchor day even in February. Time-of-day is deliberately ignored.
    day = datetime.fromisoformat(anchor.replace("Z", "+00:00")).day
    today = now.date()
    offset = 0 if _midnight(_month(today, 0, day)).timestamp() <= now.timestamp() else -1
    return {"source": source, "renews_day": day,
            "bounds": [(_midnight(_month(today, offset-i, day)),
                        _midnight(_month(today, offset+1-i, day))) for i in range(6)]}


def _own_weeks(db, seat, now, calendar_bounds):
    rows = db.execute("SELECT ends_at,peak_pct FROM windows WHERE seat=? AND observed=1 ORDER BY ends_at",
                      (seat["key"],)).fetchall()
    merged = []
    for end, peak in rows:
        if merged and end - merged[-1][0] <= 1800:
            merged[-1] = (end, max(peak, merged[-1][1]))
        else:
            merged.append((end, peak))
    if not merged:
        return [(a, b, True, None) for a, b in calendar_bounds["week"]]
    at = now.timestamp()
    end = min((end for end, _ in merged if at < end <= at + _WEEK), default=None)
    if end is None:
        anchor = min(merged, key=lambda row: abs(row[0] - at))[0]
        end = anchor + (math.floor((at - anchor) / _WEEK) + 1) * _WEEK

    def snap(value):
        closest = min(merged, key=lambda row: abs(row[0] - value))
        return closest[0] if abs(closest[0] - value) <= 12 * 3600 else value

    ends = [end]
    for _ in range(13):
        ends.append(snap(ends[-1] - _WEEK))
    observed = {end: peak for end, peak in merged}
    return [(datetime.fromtimestamp(ends[i+1]).astimezone(), datetime.fromtimestamp(ends[i]).astimezone(),
             ends[i] not in observed or ends[i+1] not in observed,
             observed.get(ends[i]) if seat["kind"] != "key" else None) for i in range(13)]


def history(ctx) -> dict:
    now = _now()
    bounds = _calendar_bounds(now)
    state = ctx.load_state()
    seats, raw_seats = [], {}
    for tool in ("codex", "claude"):
        for email, raw in state.accounts(tool).items():
            key = tool + ":" + email
            seats.append(dict(key=key, tool=tool, name=raw.get("name") or email,
                              plan=raw.get("plan") or "", kind="sub"))
            raw_seats[key] = raw
        for raw in state.data.get("keys", {}).values():
            if raw.get("harness") == tool:
                seats.append(dict(key=tool + ":" + raw["id"], tool=tool,
                                  name=raw.get("label") or raw["id"], plan="api key", kind="key"))
    db = _connect(ctx)
    try:
        db.execute("BEGIN")  # one consistent WAL snapshot while a background refresh commits
        known = {seat["key"] for seat in seats}
        used = [row[0] for row in db.execute("SELECT DISTINCT seat FROM usage ORDER BY seat")]
        for key in used:
            if key not in known and not key.endswith(":?"):
                tool, email = key.split(":", 1)
                seats.append(dict(key=key, tool=tool, name=email, plan="", kind="removed"))
        for tool in ("codex", "claude"):
            if tool + ":?" in used:
                seats.append(dict(key=tool + ":?", tool=tool, name="account unknown", plan="",
                                  kind="unattributed",
                                  note="before per-seat tracking began, or run outside ai guest list"))
        own_bounds = {}
        for seat in seats:
            if seat["kind"] != "unattributed":
                own_bounds[seat["key"]] = (
                    _own_weeks(db, seat, now, bounds),
                    _billing(ctx, seat, raw_seats.get(seat["key"], {}), now, bounds))
        earliest = min(a.timestamp() for periods in bounds.values() for a, _ in periods)
        for weeks, billing in own_bounds.values():
            earliest = min(earliest, min(a.timestamp() for a, *_ in weeks))
            if billing:
                earliest = min(earliest, min(a.timestamp() for a, _ in billing["bounds"]))
        rows = db.execute("SELECT hour,seat,model,input,cache_read,cache_write,output FROM usage"
                          " WHERE hour>=? AND hour<=?", (int(earliest // 3600), int(now.timestamp() // 3600))).fetchall()
        model_ids = sorted({row[2] for row in rows}, key=_model_key)
        models = [dict(id=model, label=_model_label(model), slot=i+1) for i, model in enumerate(model_ids[:7])]
        if len(model_ids) > 7:
            models.append(dict(id="other", label="other models", slot=0))
        visible = set(model_ids[:7])

        def aggregate(start, end, seat_key=None):
            result = {}
            lo, hi = start.timestamp(), end.timestamp()
            # UTC hour buckets align with whole-hour local midnights. Half-hour zones have
            # an unavoidable partial-hour boundary: include a bucket by its UTC start time.
            for hour, key, model, *counts in rows:
                if lo <= hour * 3600 < hi and (seat_key is None or key == seat_key):
                    target = result if seat_key else result.setdefault(key, {})
                    target_model = model if model in visible else "other"
                    old = target.setdefault(target_model, [0, 0, 0, 0])
                    for i, value in enumerate(counts):
                        old[i] += value
            return result

        def period(start, end, current, month=False):
            prev = None
            if current:
                previous = bounds["month" if month else "week"][1][0]
                cutoff = datetime.fromtimestamp(previous.timestamp() + now.timestamp() - start.timestamp()).astimezone()
                prev = aggregate(previous, cutoff)
            return {"from": start.isoformat(), "to": end.isoformat(), "label": _label(start, end, month),
                    "current": current, "by": aggregate(start, min(end, now)), "prev_same_point": prev}

        def own_period(start, end, approx, peak, key):
            return {"from": start.isoformat(), "to": end.isoformat(), "label": _label(start, end),
                    "current": start.timestamp() <= now.timestamp() < end.timestamp(),
                    "approx": approx, "limit_pct": peak, "by": aggregate(start, min(end, now), key)}

        own = {}
        for key, (weeks, billing) in own_bounds.items():
            own[key] = {"week": [own_period(a, b, approx, peak, key) for a, b, approx, peak in weeks],
                        "month": None if billing is None else {
                            "source": billing["source"], "renews_day": billing["renews_day"],
                            "periods": [own_period(a, b, billing["source"] == "calendar", None, key)
                                        for a, b in billing["bounds"]]}}
        tracking = db.execute("SELECT value FROM meta WHERE key='tracking_codex'").fetchone()
        _, claude_tracking = _ledger(ctx)
        return {"v": 1, "generated_at": now.isoformat(), "index": _progress(db),
                "tracking": {"claude": claude_tracking, "codex": None if tracking is None else
                             datetime.fromtimestamp(float(tracking[0])).astimezone().isoformat()},
                "models": models, "seats": seats, "calendar": {
                    kind: [period(a, b, i == 0, kind == "month") for i, (a, b) in enumerate(periods)]
                    for kind, periods in bounds.items()}, "own": own}
    finally:
        db.close()
