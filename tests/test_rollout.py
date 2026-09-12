"""Structured limit signals parsed out of Codex's rollout JSONL.

Hermetic: every path is under tmp_path, nothing reads the real ~/.codex, and all mtimes are set
explicitly with os.utime so discovery is tested against fixed numbers rather than the wall clock.
The line-building helpers are exported for the launcher tests that drive a fake session.
"""
import itertools
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from acctsw import paths as P
from acctsw import rollout
from acctsw.util import iso, now

TS = "2026-09-11T14:00:00"          # default rollout timestamp (also the dated path + filename)
BASE = 1_789_000_000                # fixed epoch seconds for deterministic mtimes
RESET_5H = 1_789_152_832
RESET_WEEK = 1_789_500_000
OUT_OF_CREDITS = ("Your workspace is out of credits. "
                  "Ask your workspace owner to refill in order to continue.")


# --- line builders (imported by the launcher tests) ---------------------------------------------

def rollout_line(payload_type, *, timestamp=None, **payload):
    """One rollout line: ``session_meta`` is its own top-level type, everything else is event_msg."""
    ts = timestamp or TS
    if payload_type == "session_meta":
        return json.dumps({"type": "session_meta", "timestamp": ts, "payload": dict(payload)})
    return json.dumps({"type": "event_msg", "timestamp": ts,
                       "payload": {"type": payload_type, **payload}})


def window(used_percent, *, minutes=300, resets_at=None):
    return {"used_percent": used_percent, "window_minutes": minutes, "resets_at": resets_at}


def token_count_line(*, primary=None, secondary=None, reached_type=None, spend=None,
                     has_credits=False, plan_type="team", timestamp=None):
    return rollout_line("token_count", timestamp=timestamp, info={}, rate_limits={
        "limit_id": "premium", "limit_name": None, "primary": primary, "secondary": secondary,
        "credits": {"has_credits": has_credits, "unlimited": False, "balance": None},
        "individual_limit": None, "spend_control_reached": spend, "plan_type": plan_type,
        "rate_limit_reached_type": reached_type})


def task_complete_error_line(code="usage_limit_exceeded", message=OUT_OF_CREDITS, timestamp=None):
    return rollout_line("task_complete", timestamp=timestamp, turn_id="t1", last_agent_message=None,
                        error={"message": message, "codex_error_info": code},
                        started_at=1_789_136_132, completed_at=1_789_136_137, duration_ms=4992)


def write_rollout(dir, *, thread="01a0abcd", cwd="/work/x", ts=TS, lines=()):
    """Create ``<dir>/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl`` with a session_meta header."""
    y, m, d = ts[:10].split("-")
    dest = Path(dir) / y / m / d
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"rollout-{ts.replace(':', '-')}-{thread}.jsonl"
    meta = rollout_line("session_meta", timestamp=ts, id=thread, cwd=cwd,
                        originator="codex-tui", cli_version="0.153.4")
    path.write_text("\n".join([meta, *lines]) + "\n")
    return path


# --- local helpers ------------------------------------------------------------------------------

def _touch(path, seconds):
    ns = int(seconds * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


def _fast_clock():
    """A clock that jumps 10s per call, so the attach rate-limit never hides a rescan in a test."""
    ticks = itertools.count(0.0, 10.0)
    return lambda: next(ticks)


def _watcher(root, *, cwd="/work/x", started_at=None, before=None):
    return rollout.RolloutWatcher([root], cwd=cwd, started_at=started_at or now(),
                                  before=before or {}, clock=_fast_clock())


def _append(path, text):
    with open(path, "ab") as f:
        f.write(text if isinstance(text, bytes) else text.encode())


def _obj(line):
    return json.loads(line)


# --- classify -----------------------------------------------------------------------------------

def test_classify_reached_type_is_a_hard_limit():
    sig = rollout.classify(_obj(token_count_line(
        primary=window(78.0, resets_at=RESET_5H),
        reached_type="workspace_member_credits_depleted", timestamp="2026-09-11T14:05:00Z")))
    assert sig.kind == "limit"
    assert sig.hard is True
    assert sig.detail == "workspace_member_credits_depleted"
    assert sig.reset_at.startswith("2026-")
    assert sig.at == "2026-09-11T14:05:00Z"


def test_classify_has_credits_false_alone_is_not_a_limit():
    sig = rollout.classify(_obj(token_count_line(
        primary=window(2.0, resets_at=RESET_5H), has_credits=False, reached_type=None)))
    assert sig.kind == "healthy"
    assert sig.hard is False


def test_classify_task_complete_usage_limit_exceeded():
    sig = rollout.classify(_obj(task_complete_error_line()))
    assert sig.kind == "limit"
    assert sig.hard is True
    assert sig.reset_at is None
    assert sig.detail == OUT_OF_CREDITS


def test_classify_window_at_100_uses_the_later_reset():
    sig = rollout.classify(_obj(token_count_line(
        primary=window(100.0, resets_at=RESET_5H),
        secondary=window(100.0, minutes=10080, resets_at=RESET_WEEK))))
    assert sig.kind == "limit"
    assert sig.hard is False
    assert sig.detail == "window at 100%"
    assert sig.reset_at == rollout._reset_iso(RESET_WEEK)
    assert sig.reset_at > rollout._reset_iso(RESET_5H)


def test_classify_spend_control_reached():
    sig = rollout.classify(_obj(token_count_line(primary=window(40.0), spend=True)))
    assert sig.kind == "limit"
    assert sig.hard is True
    assert sig.detail == "spend control reached"


def test_classify_ignores_other_events():
    ignored = [
        rollout_line("task_started", turn_id="t1"),
        rollout_line("item_completed", item={"id": "1"}),
        rollout_line("turn_aborted", reason="interrupted"),
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
        rollout_line("session_meta", id="t", cwd="/work/x"),
        rollout_line("token_count", rate_limits=None, info={}),
        token_count_line(primary=None, secondary=None),
    ]
    assert [rollout.classify(_obj(line)) for line in ignored] == [None] * len(ignored)


# --- find_session_file --------------------------------------------------------------------------

def test_find_session_file_prefers_cwd_match(tmp_path):
    root = tmp_path / "sessions"
    files = [write_rollout(root, thread="a", cwd="/other/1"),
             write_rollout(root, thread="b", cwd="/work/x"),
             write_rollout(root, thread="c", cwd="/other/2")]
    for p in files:
        _touch(p, BASE)
    before = rollout.scan_rollouts([root])
    for p in files:
        _touch(p, BASE + 10)
    got, unambiguous = rollout.find_session_file([root], before=before, cwd="/work/x",
                                                 since_ns=int((BASE + 5) * 1e9))
    assert got == files[1]
    assert unambiguous is True


def test_find_session_file_picks_resumed_old_dated_file(tmp_path):
    root = tmp_path / "sessions"
    old = write_rollout(root, thread="old", cwd="/work/x", ts="2026-09-06T09:00:00")
    new = write_rollout(root, thread="new", cwd="/elsewhere", ts="2026-09-11T14:00:00")
    _touch(old, BASE)
    _touch(new, BASE)
    before = rollout.scan_rollouts([root])
    _touch(old, BASE + 20)  # the resumed thread appends to its ORIGINAL dated file
    _touch(new, BASE + 30)  # a newer-dated file, but another cwd
    got, unambiguous = rollout.find_session_file([root], before=before, cwd="/work/x",
                                                 since_ns=int((BASE + 5) * 1e9))
    assert got == old
    assert unambiguous is True


def test_find_session_file_ignores_files_untouched_since_spawn(tmp_path):
    root = tmp_path / "sessions"
    stale = write_rollout(root, thread="stale", cwd="/work/x")
    _touch(stale, BASE)
    before = rollout.scan_rollouts([root])
    ancient = write_rollout(root, thread="ancient", cwd="/work/x", ts="2026-09-06T09:00:00")
    _touch(ancient, BASE - 3600)  # never in `before`, but far older than the spawn
    assert rollout.find_session_file([root], before=before, cwd="/work/x",
                                     since_ns=int((BASE + 5) * 1e9)) == (None, False)


def test_find_session_file_two_cwd_matches_is_ambiguous(tmp_path):
    root = tmp_path / "sessions"
    first = write_rollout(root, thread="one", cwd="/work/x")
    second = write_rollout(root, thread="two", cwd="/work/x")
    _touch(first, BASE + 10)
    _touch(second, BASE + 20)
    got, unambiguous = rollout.find_session_file([root], before={}, cwd="/work/x",
                                                 since_ns=int((BASE + 5) * 1e9))
    assert got == second  # most recently modified wins
    assert unambiguous is False


def test_find_session_file_single_foreign_candidate_attaches_ambiguously(tmp_path):
    root = tmp_path / "sessions"
    foreign = write_rollout(root, thread="resumed", cwd="/somewhere/else")
    _touch(foreign, BASE + 10)
    assert rollout.find_session_file([root], before={}, cwd="/work/x",
                                     since_ns=int((BASE + 5) * 1e9)) == (foreign, False)


# --- watcher ------------------------------------------------------------------------------------

def test_watcher_skips_events_older_than_session_start(tmp_path):
    root = tmp_path / "sessions"
    started = now()
    old = iso(started - timedelta(days=5))
    path = write_rollout(root, ts="2026-09-06T09:00:00", lines=[
        task_complete_error_line(timestamp=old),
        token_count_line(primary=window(100.0), timestamp=old)])
    watcher = _watcher(root, started_at=started)
    assert watcher.poll() == []
    assert watcher.attached == path
    _append(path, task_complete_error_line(timestamp=iso(started + timedelta(seconds=1))) + "\n")
    signals = watcher.poll()
    assert [s.kind for s in signals] == ["limit"]
    assert signals[0].hard is True


def test_watcher_reads_only_appended_bytes(tmp_path):
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    path = write_rollout(root, lines=[task_complete_error_line(timestamp=ts)])
    watcher = _watcher(root, started_at=started)
    assert len(watcher.poll()) == 1
    assert watcher.poll() == []
    _append(path, token_count_line(primary=window(3.0), timestamp=ts) + "\n")
    assert [s.kind for s in watcher.poll()] == ["healthy"]
    assert watcher.poll() == []


def test_watcher_handles_truncation_and_inode_change(tmp_path):
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    path = write_rollout(root, lines=[token_count_line(primary=window(5.0), timestamp=ts)])
    watcher = _watcher(root, started_at=started)
    assert [s.kind for s in watcher.poll()] == ["healthy"]

    path.write_text(rollout_line("session_meta", id="t", cwd="/work/x") + "\n"
                    + task_complete_error_line(timestamp=ts) + "\n")  # truncate + rewrite
    assert [s.kind for s in watcher.poll()] == ["limit"]

    replacement = path.with_suffix(".new")
    replacement.write_text(rollout_line("session_meta", id="t", cwd="/work/x") + "\n"
                           + token_count_line(primary=window(100.0), timestamp=ts) + "\n")
    os.replace(replacement, path)  # a different inode under the same name
    signals = watcher.poll()
    assert [s.kind for s in signals] == ["limit"]
    assert signals[0].hard is False


def test_watcher_ignores_partial_trailing_line(tmp_path):
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    path = write_rollout(root)
    watcher = _watcher(root, started_at=started)
    assert watcher.poll() == []
    line = task_complete_error_line(timestamp=ts)
    _append(path, line[:20])
    assert watcher.poll() == []
    _append(path, line[20:] + "\n")
    assert [s.kind for s in watcher.poll()] == ["limit"]


def test_watcher_survives_missing_sessions_dir(tmp_path):
    watcher = rollout.RolloutWatcher([tmp_path / "nope" / "sessions", tmp_path / "also-gone"],
                                     cwd="/work/x", started_at=now(), before={},
                                     clock=_fast_clock())
    assert watcher.poll() == []
    assert watcher.poll() == []
    assert watcher.attached is None
    assert watcher.unambiguous is False
    watcher.close()


def test_watcher_attach_scan_is_rate_limited(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rollout, "find_session_file",
                        lambda *a, **kw: (calls.append(1), (None, False))[1])
    clock = [1000.0]
    watcher = rollout.RolloutWatcher([tmp_path], cwd="/work/x", started_at=now(), before={},
                                     clock=lambda: clock[0])
    for _ in range(5):
        watcher.poll()
    assert len(calls) == 1
    clock[0] += rollout.ATTACH_SCAN_INTERVAL_S + 0.01
    watcher.poll()
    watcher.poll()
    assert len(calls) == 2


def test_sessions_roots_uses_context_not_module_paths(ctx, tmp_path, monkeypatch):
    sentinel = Path("/sentinel/never-read/sessions")
    monkeypatch.setattr(P, "CODEX_SESSIONS", sentinel)
    roots = rollout.sessions_roots(ctx, "a@x.com")
    assert roots
    assert sentinel not in roots
    for root in roots:
        assert str(root).startswith(str(tmp_path.resolve()))
        assert root.name == "sessions"
    assert rollout.sessions_roots(ctx, None) == [(ctx._codex_real / "sessions").resolve()]


def test_watcher_poll_never_raises_on_garbage_lines(tmp_path):
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    path = write_rollout(root)
    watcher = _watcher(root, started_at=started)
    assert watcher.poll() == []
    _append(path, b"\x00\xff\xfe not utf-8 at all \x80\n")
    _append(path, b"not json {{{\n")
    _append(path, b"[1, 2, 3]\n")
    _append(path, b'"a bare string"\n')
    _append(path, b'{"type": "event_msg", "payload": 7}\n')
    _append(path, b"\n")
    assert watcher.poll() == []
    _append(path, task_complete_error_line(timestamp=ts) + "\n")
    assert [s.kind for s in watcher.poll()] == ["limit"]


def test_watcher_reattaches_to_cwd_match_after_foreign_attach(tmp_path):
    """A provisional attachment must be correctable. With only a stranger's session moving, the
    single-foreign-candidate fallback attaches to it — and the caller then stops trusting the stdout
    banner. So the moment OUR child writes its first line, discovery has to notice and switch, or a
    wrong guess would silence both sources for the rest of the session."""
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    foreign = write_rollout(root, thread="stranger", cwd="/somewhere/else")
    watcher = _watcher(root, started_at=started)

    assert watcher.poll() == []          # nothing said yet, but it is all that is moving...
    assert watcher.attached == foreign   # ...so the fallback holds it, provisionally
    assert watcher.unambiguous is False

    mine = write_rollout(root, thread="ours", cwd="/work/x",
                         lines=[task_complete_error_line(timestamp=ts)])
    signals = watcher.poll()

    assert watcher.attached == mine
    assert watcher.unambiguous is True
    assert [s.kind for s in signals] == ["limit"]


def test_watcher_keeps_its_cwd_match_when_a_rival_session_appears(tmp_path):
    """The re-scan may only ever IMPROVE the guess. Two sessions share this directory, so our
    attachment stays ambiguous and discovery keeps running — but swapping between two equally
    plausible cwd matches would replay each new file's tail and re-deliver limits that were already
    judged. Holding a cwd match, an equally ambiguous rival is no improvement."""
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    rival = write_rollout(root, thread="rival", cwd="/work/x")
    mine = write_rollout(root, thread="ours", cwd="/work/x")
    _touch(rival, time.time() + 5)
    _touch(mine, time.time() + 10)       # newest wins the first, ambiguous attach
    watcher = _watcher(root, started_at=started)
    assert watcher.poll() == []
    assert watcher.attached == mine and watcher.unambiguous is False

    _append(rival, task_complete_error_line(timestamp=ts) + "\n")
    _touch(rival, time.time() + 30)      # now the newest, so max(mtime) alone would prefer it

    assert watcher.poll() == []          # the rival's limit never reaches us
    assert watcher.attached == mine


# --- discovery: the defensive edges -------------------------------------------------------------

def test_sessions_roots_skips_a_root_that_cannot_be_resolved(ctx, monkeypatch):
    """A per-account CODEX_HOME whose ``sessions`` entry is a broken symlink loop raises on resolve.
    One unusable home must not cost us the OTHER root — the real ~/.codex is where a symlinked home
    points anyway, so dropping the whole list would blind the watcher for the entire session."""
    doomed = Path(ctx.codex_home("a@x.com")) / "sessions"
    real_resolve = Path.resolve

    def flaky(self, *a, **kw):
        if self == doomed:
            raise OSError("ELOOP: too many levels of symbolic links")
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(rollout.Path, "resolve", flaky)
    assert rollout.sessions_roots(ctx, "a@x.com") == [real_resolve(ctx._codex_real / "sessions")]


def test_scan_rollouts_visits_a_repeated_root_once(tmp_path):
    """A per-account home usually SYMLINKS its sessions dir to the shared one, so the two roots can
    collapse to the same real directory. Walking it twice would double every stat for nothing."""
    root = tmp_path / "sessions"
    path = write_rollout(root)
    _touch(path, BASE)
    once = rollout.scan_rollouts([root])
    assert once == {path: int(BASE * 1_000_000_000)}
    assert rollout.scan_rollouts([root, root, root / "." ]) == once


class _RaisingEntry:
    """A scandir entry whose metadata calls fail — a file deleted between listing and stat, or one
    inside a directory we may list but not read."""

    def __init__(self, entry, fail):
        self._entry, self._fail = entry, fail
        self.name, self.path = entry.name, entry.path

    def is_dir(self, follow_symlinks=True):
        if self._fail == "is_dir":
            raise OSError("ENOENT: vanished between listing and stat")
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks=True):
        if self._fail == "stat":
            raise OSError("ENOENT: vanished between listing and stat")
        return self._entry.stat(follow_symlinks=follow_symlinks)


def test_scan_rollouts_skips_entries_whose_metadata_raises(tmp_path, monkeypatch):
    """The sessions tree is written by another process: a file can disappear between the directory
    listing and the stat that follows it. That entry is simply absent from the result — a scan that
    raised would take down the watcher, and with it the whole supervisor."""
    root = tmp_path / "sessions"
    good = write_rollout(root, thread="good")
    gone = write_rollout(root, thread="gone")
    doomed_dir = write_rollout(root, thread="x", ts="2026-09-06T09:00:00").parent
    _touch(good, BASE)

    real_scandir = os.scandir

    class _Fake:
        def __init__(self, d):
            self._it = real_scandir(d)

        def __enter__(self):
            return [_RaisingEntry(e, "stat" if e.path == str(gone) else
                                 ("is_dir" if e.path == str(doomed_dir) else None))
                    for e in self._it]

        def __exit__(self, *exc):
            self._it.close()
            return False

    monkeypatch.setattr(rollout.os, "scandir", _Fake)
    found = rollout.scan_rollouts([root])
    assert list(found) == [good]          # the unreadable file and the unreadable dir are just absent


def test_session_meta_returns_none_for_anything_that_is_not_a_header(tmp_path):
    """Line 1 is read blind: the file may be empty, half-written, not a rollout at all, or not even
    a file. Every one of those is "we don't know whose session this is", never an exception."""
    empty = tmp_path / "rollout-empty.jsonl"
    empty.write_text("")
    not_a_dict = tmp_path / "rollout-list.jsonl"
    not_a_dict.write_text("[1, 2, 3]\n")
    wrong_type = tmp_path / "rollout-other.jsonl"
    wrong_type.write_text(rollout_line("token_count", rate_limits=None, info={}) + "\n")
    bad_payload = tmp_path / "rollout-payload.jsonl"
    bad_payload.write_text(json.dumps({"type": "session_meta", "payload": "just a string"}) + "\n")

    for path in (tmp_path, empty, not_a_dict, wrong_type, bad_payload):
        assert rollout.session_meta(path) is None

    header = write_rollout(tmp_path / "sessions", cwd="/work/x")
    assert rollout.session_meta(header)["cwd"] == "/work/x"


def test_find_session_file_skips_a_file_that_has_not_moved_since_the_snapshot(tmp_path):
    """Discovery is snapshot-then-diff: a session in OUR cwd that was already idle when we spawned
    is not our child, however recent its mtime. Only files that actually MOVED are candidates."""
    root = tmp_path / "sessions"
    idle = write_rollout(root, thread="idle", cwd="/work/x")
    _touch(idle, BASE + 10)                       # newer than the spawn floor...
    before = rollout.scan_rollouts([root])        # ...but recorded, and it never advances again
    assert rollout.find_session_file([root], before=before, cwd="/work/x",
                                     since_ns=int((BASE + 5) * 1e9)) == (None, False)


# --- classification: malformed windows ----------------------------------------------------------

def test_classify_ignores_a_used_percent_that_is_not_a_number():
    """``used_percent: true`` would read as 1.0 under a bare int check (bool IS an int in Python),
    and a string would raise on compare. Neither says anything about the window, so neither may
    produce a verdict — "uninformative" is not "healthy" and certainly not "limited"."""
    for junk in (True, False, "12", "100", None, {"pct": 100}):
        line = token_count_line(primary={"used_percent": junk, "window_minutes": 300,
                                         "resets_at": RESET_5H})
        assert rollout.classify(_obj(line)) is None


def test_classify_passes_a_string_resets_at_through_untouched():
    """Rollouts write ``resets_at`` as epoch seconds, but the schema is undocumented and a build
    that switches to ISO must keep working — a string is already what the caller wants. An EMPTY
    string is not a time, though, and must not be stamped on a seat as its unlock moment."""
    iso_reset = rollout.classify(_obj(token_count_line(
        primary=window(78.0, resets_at="2026-09-12T00:00:00Z"),
        reached_type="workspace_member_credits_depleted")))
    assert iso_reset.reset_at == "2026-09-12T00:00:00Z"

    blank = rollout.classify(_obj(token_count_line(
        primary=window(78.0, resets_at=""), reached_type="workspace_member_credits_depleted")))
    assert blank.kind == "limit" and blank.reset_at is None


def test_classify_drops_an_out_of_range_resets_at():
    """An epoch the platform cannot turn into a datetime (a garbled or millisecond-scaled field)
    must leave the limit standing WITHOUT a reset time, not raise inside the watcher."""
    for junk in (1e20, -1e20, 1_789_000_000_000):
        sig = rollout.classify(_obj(token_count_line(
            primary=window(100.0, resets_at=junk), secondary=None)))
        assert sig.kind == "limit" and sig.hard is False
        assert sig.reset_at is None


def test_classify_task_complete_without_a_limit_error_says_nothing():
    """Most turns finish fine, and the ones that fail usually fail for reasons that are none of our
    business (a tool error, a cancelled turn). Only the server's own limit codes are a signal."""
    ignored = [
        rollout_line("task_complete", turn_id="t1", last_agent_message="done", error=None),
        rollout_line("task_complete", turn_id="t1", error={"message": "boom",
                                                           "codex_error_info": "other"}),
        rollout_line("task_complete", turn_id="t1", error="not even a dict"),
        task_complete_error_line(code="internal_server_error"),
    ]
    assert [rollout.classify(_obj(line)) for line in ignored] == [None] * len(ignored)


# --- watcher: attaching to a file already in flight ---------------------------------------------

def test_watcher_upgrades_confidence_on_the_same_file_without_replaying_it(tmp_path):
    """A provisional attachment that later proves unique must only flip the FLAG. Two sessions share
    this directory, so we attach ambiguously; when the rival's log is gone the next scan names the
    same file, now unique. Re-seeking there would replay the tail and hand the caller a limit it has
    already acted on — a second, unearned hop on one event."""
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    rival = write_rollout(root, thread="rival", cwd="/work/x")
    mine = write_rollout(root, thread="ours", cwd="/work/x",
                         lines=[task_complete_error_line(timestamp=ts)])
    _touch(rival, time.time() + 5)
    _touch(mine, time.time() + 10)       # newest wins the first, ambiguous attach

    watcher = _watcher(root, started_at=started)
    assert [s.kind for s in watcher.poll()] == ["limit"]
    assert watcher.attached == mine and watcher.unambiguous is False

    rival.unlink()                       # the other session's log is pruned away

    assert watcher.poll() == []          # the limit is NOT delivered a second time...
    assert watcher.attached == mine
    assert watcher.unambiguous is True   # ...but we now know the file is ours


def _pad(line, size):
    """``line`` padded with trailing spaces to exactly ``size`` bytes including its newline (JSON
    ignores the padding), so a test can place a known byte layout under the tail window."""
    fill = size - len(line.encode()) - 1
    assert fill >= 0
    return line + " " * fill


def test_watcher_attach_reads_only_the_tail_of_a_long_running_session(tmp_path, monkeypatch):
    """A resumed thread keeps appending to its ORIGINAL file, which can be megabytes — and still
    holds the limit that ended the previous run. Attach re-reads only the last TAIL_BYTES, cut
    FORWARD to the next line boundary so the partial line the window starts in is never parsed."""
    monkeypatch.setattr(rollout, "TAIL_BYTES", 4096)
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    buried = task_complete_error_line(timestamp=ts)   # inside this session, but far from the end
    filler = [_pad(token_count_line(primary=window(4.0), timestamp=ts), 1024) for _ in range(20)]
    path = write_rollout(root, lines=[buried, *filler])
    assert path.stat().st_size > 4096 * 4

    watcher = _watcher(root, started_at=started)
    # The 4096-byte window covers four 1024-byte lines; the first is the one we land inside, so it
    # is skipped and exactly three whole lines are read. The buried limit is far outside it.
    assert [s.kind for s in watcher.poll()] == ["healthy"] * 3

    _append(path, task_complete_error_line(timestamp=ts) + "\n")
    assert [s.kind for s in watcher.poll()] == ["limit"]   # and it tails normally from there


def test_watcher_poll_swallows_an_error_that_is_not_an_oserror(tmp_path, monkeypatch):
    """``poll`` is called from the supervisor's heartbeat, so ANY exception escaping it would take
    down the session it is meant to protect. The inner reads guard OSError; this is the outer net
    for everything else a filesystem layer or a schema change can throw."""
    root = tmp_path / "sessions"
    started = now()
    ts = iso(started + timedelta(seconds=1))
    path = write_rollout(root, lines=[task_complete_error_line(timestamp=ts)])
    watcher = _watcher(root, started_at=started)
    assert [s.kind for s in watcher.poll()] == ["limit"]

    real_stat = os.stat

    def boom(target, *a, **kw):
        if not isinstance(target, int) and str(target) == str(path):
            raise RuntimeError("not an OSError, and still not worth killing a session over")
        return real_stat(target, *a, **kw)

    monkeypatch.setattr(rollout.os, "stat", boom)
    assert watcher.poll() == []
