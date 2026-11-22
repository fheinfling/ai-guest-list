"""The supervised-session heartbeat rejects stale and recycled PIDs."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from acctsw import procenv, session
from acctsw.util import write_json


def _path(ctx, tool="codex"):
    return ctx.data_dir / f"session-{tool}.json"


def test_active_session_none_when_missing(ctx):
    assert session.active_session(ctx.data_dir, "codex") is None


def test_mark_session_then_active(ctx):
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    active = session.active_session(ctx.data_dir, "codex")
    assert active is not None
    assert active["email"] == "a@x.com"
    assert active["pid"] == os.getpid()
    assert isinstance(active["started_at"], str)
    assert stat.S_IMODE(_path(ctx).stat().st_mode) == 0o600


@pytest.mark.parametrize("blocker", [None, "ambiguous_cwd", "busy_output"])
def test_restart_blocker_is_written_and_exposed_in_session_records(ctx, monkeypatch, blocker):
    import json
    monkeypatch.setattr(session, "RESTART_BLOCKER", blocker, raising=False)
    monkeypatch.setattr(session, "__version__", "1.6.1")
    session.mark_session(ctx.data_dir, "claude", "a@x.com")
    assert json.loads(_path(ctx, "claude").read_text())["restart_blocker"] == blocker
    assert session.active_session(ctx.data_dir, "claude")["restart_blocker"] == blocker
    if blocker:
        assert session.stale_engines(ctx.data_dir)[0]["restart_blocker"] == blocker


def test_mark_session_updates_active_seat(ctx):
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    session.mark_session(ctx.data_dir, "codex", "b@x.com")
    assert session.active_session(ctx.data_dir, "codex")["email"] == "b@x.com"


def test_diagnostics_preserve_launch_hop_and_unknown_metadata_in_own_record(ctx, monkeypatch):
    monkeypatch.setattr(session, "SELF_RESTART", True)
    monkeypatch.setattr(session, "RESTART_BLOCKER", None)
    session.mark_session(ctx.data_dir, "claude", "billed@x.com", home="home@x.com",
                         pin="launch-id", key_seat={"id": "key", "label": "fixture",
                                                    "provider": "anthropic", "model": "opus"})
    paths = [_path(ctx, "claude"),
             ctx.data_dir / f"session-claude-{os.getpid()}.json"]
    before = []
    for path in paths:
        data = json.loads(path.read_text())
        data.update(tty="/dev/ttys-fixture", end_requested=True, future_field={"keep": 1})
        write_json(path, data)
        before.append(data)
    monkeypatch.setattr(session, "SELF_RESTART", False)
    monkeypatch.setattr(session, "RESTART_BLOCKER", "ambiguous_cwd")
    session.update_restart_diagnostics(ctx.data_dir, "claude")
    assert json.loads(paths[0].read_text()) == before[0]
    assert json.loads(paths[1].read_text()) == {
        **before[1], "self_restart": False, "restart_blocker": "ambiguous_cwd"}
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_diagnostics_cannot_overwrite_a_hop_after_legacy_ownership_check(ctx, monkeypatch):
    ours, other = os.getpid(), os.getpid() + 1
    monkeypatch.setattr(session, "_proc_start", lambda _pid: "Stable Start")
    monkeypatch.setattr(session, "_alive", lambda _pid: True)
    session.mark_session(ctx.data_dir, "claude", "ours@x.com")
    legacy = _path(ctx, "claude")
    write = session.write_json
    hopped, writes = [], []

    def hop():
        hopped.append(True)
        with monkeypatch.context() as sibling_patch:
            sibling_patch.setattr(session.os, "getpid", lambda: other)
            session.mark_session(ctx.data_dir, "claude", "new-seat@x", home="old-seat@x")
        assert json.loads(legacy.read_text())["email"] == "new-seat@x"

    def write_after_hop(path, data, **kwargs):
        if os.getpid() == ours:
            writes.append(path)
            if path == legacy and not hopped:
                # P already read/checked the mirror; Q completes its hop before P replaces it.
                hop()
        write(path, data, **kwargs)

    monkeypatch.setattr(session, "write_json", write_after_hop)
    monkeypatch.setattr(session, "RESTART_BLOCKER", "app_not_running")
    session.update_restart_diagnostics(ctx.data_dir, "claude")
    if not hopped:
        # A per-process-only writer never enters that legacy read/modify/write window.
        hop()
    other_record = json.loads(session._process_file(ctx.data_dir, "claude", other).read_text())
    assert json.loads(legacy.read_text()) == other_record
    assert other_record["pid"] == other
    assert other_record["email"] == "new-seat@x"
    assert other_record["home"] == "old-seat@x"
    own = session._process_file(ctx.data_dir, "claude", ours)
    assert json.loads(own.read_text())["restart_blocker"] == "app_not_running"
    assert writes == [own]


@pytest.mark.parametrize("own_record", [None, "{", "[]", '{"pid": -1}'])
def test_diagnostics_never_fall_back_to_legacy_when_own_record_is_unusable(
        ctx, monkeypatch, own_record):
    session.mark_session(ctx.data_dir, "claude", "ours@x.com")
    own = session._process_file(ctx.data_dir, "claude", os.getpid())
    if own_record is None:
        own.unlink()
    else:
        own.write_text(own_record)
    legacy = _path(ctx, "claude").read_bytes()
    monkeypatch.setattr(session, "RESTART_BLOCKER", "draft")
    session.update_restart_diagnostics(ctx.data_dir, "claude")
    assert _path(ctx, "claude").read_bytes() == legacy
    if own_record is None:
        assert not own.exists()
    else:
        assert own.read_text() == own_record


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("process_first", [False, True])
@pytest.mark.parametrize("legacy_started_at", ["2026-09-30", "2026-10-01"])
def test_readers_prefer_per_process_diagnostics_over_same_pid_legacy(
        ctx, monkeypatch, strict, process_first, legacy_started_at):
    monkeypatch.setattr(session, "__version__", "1.6.1")
    monkeypatch.setattr(session, "_proc_start", lambda _pid: "Stable Start")
    monkeypatch.setattr(session, "_alive", lambda _pid: True)
    monkeypatch.setattr(session, "SELF_RESTART", True)
    monkeypatch.setattr(session, "RESTART_BLOCKER", None)
    session.mark_session(ctx.data_dir, "claude", "ours@x.com")
    legacy, own = _path(ctx, "claude"), session._process_file(ctx.data_dir, "claude", os.getpid())
    data = json.loads(own.read_text())
    write_json(own, {**data, "started_at": "2026-10-01",
                     "self_restart": False, "restart_blocker": "ambiguous_cwd"})
    write_json(legacy, {**data, "started_at": legacy_started_at})
    if process_first:
        files = session._session_files
        monkeypatch.setattr(session, "_session_files",
                            lambda *a, **kw: list(reversed(files(*a, **kw))))
    active, = session.active_sessions(ctx.data_dir, "claude", strict=strict)
    newest = session.active_session(ctx.data_dir, "claude")
    stale, = session.stale_engines(ctx.data_dir)
    cached_stale, = session.stale_engines(ctx.data_dir, {"claude": [active], "codex": []})
    for record in (active, newest, stale, cached_stale):
        assert record["self_restart"] is False
        assert record["restart_blocker"] == "ambiguous_cwd"
        assert record["started_at"] == "2026-10-01"


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("process_first", [False, True])
def test_upgrade_preserved_legacy_owner_can_publish_a_later_hop(
        ctx, monkeypatch, strict, process_first):
    from acctsw import accounts
    home, billed, sibling = "home@x.com", "billed@x.com", "sibling@x.com"
    state = ctx.load_state()
    for email in (home, billed, sibling):
        state.upsert_seat("claude", email)
    state.save()
    monkeypatch.setattr(session, "_alive", lambda _pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda _pid: "Stable Start")
    legacy = _path(ctx, "claude")
    old = {"pid": 101, "process_start": "Stable Start", "email": home,
           "started_at": "2026-10-01", "cwd": "/work", "self_restart": False}
    write_json(legacy, old)
    with monkeypatch.context() as other:
        other.setattr(session.os, "getpid", lambda: 202)
        session.mark_session(ctx.data_dir, "claude", sibling)
    snapshot = session._process_file(ctx.data_dir, "claude", 101)
    assert json.loads(snapshot.read_text()) == old  # Production upgrade preservation.
    hopped = {**old, "email": billed, "home": home, "started_at": "2026-10-02"}
    write_json(legacy, hopped)  # The legacy-only owner still writes only its shared heartbeat.
    if process_first:
        files = session._session_files
        monkeypatch.setattr(session, "_session_files",
                            lambda *a, **kw: list(reversed(files(*a, **kw))))
    observed = next(s for s in session.active_sessions(ctx.data_dir, "claude", strict=strict)
                    if s["pid"] == 101)
    assert observed["email"] == billed
    assert observed["home"] == home
    assert observed["started_at"] == "2026-10-02"
    views = {s["email"]: s for s in accounts.list_seats(
        ctx.load_state(), "claude", data_dir=ctx.data_dir)}
    assert views[billed]["in_session"] is True
    assert views[home]["in_session"] is False


def test_diagnostics_leave_another_supervisors_legacy_mirror_unchanged(ctx, monkeypatch):
    session.mark_session(ctx.data_dir, "claude", "ours@x.com")
    legacy = _path(ctx, "claude")
    other = {**json.loads(legacy.read_text()), "pid": os.getpid() + 1, "email": "other@x.com"}
    write_json(legacy, other)
    before = legacy.read_bytes()
    monkeypatch.setattr(session, "RESTART_BLOCKER", "draft")
    session.update_restart_diagnostics(ctx.data_dir, "claude")
    assert legacy.read_bytes() == before
    own = ctx.data_dir / f"session-claude-{os.getpid()}.json"
    assert json.loads(own.read_text())["restart_blocker"] == "draft"


def test_mark_session_caches_process_start(ctx, monkeypatch):
    pid = os.getpid()
    session._START_CACHE.pop(pid, None)
    calls = []
    monkeypatch.setattr(session, "_proc_start", lambda p: calls.append(p) or "Stable Start")
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    session.mark_session(ctx.data_dir, "codex", "b@x.com")
    assert calls == [pid]
    session._START_CACHE.pop(pid, None)


def test_clear_session_is_idempotent(ctx):
    session.mark_session(ctx.data_dir, "claude", "c@x.com")
    session.clear_session(ctx.data_dir, "claude")
    session.clear_session(ctx.data_dir, "claude")
    assert session.active_session(ctx.data_dir, "claude") is None


def test_dead_pid_reads_as_no_session(ctx):
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead.wait()
    write_json(_path(ctx), {
        "email": "gone@x.com",
        "pid": dead.pid,
        "process_start": "irrelevant",
        "started_at": "2026-01-01T00:00:00+00:00",
    })
    assert session.active_session(ctx.data_dir, "codex") is None
    assert not _path(ctx).exists()


def test_recycled_pid_reads_as_no_session(ctx, monkeypatch):
    """A live PID with the wrong recorded start-time is another process, not this session."""
    monkeypatch.setattr(session, "_proc_start", lambda pid: "The Real Start Time")
    write_json(_path(ctx), {
        "email": "stale@x.com",
        "pid": os.getpid(),
        "process_start": "Not The Real Start Time",
        "started_at": "2026-01-01T00:00:00+00:00",
    })
    assert session.active_session(ctx.data_dir, "codex") is None
    assert not _path(ctx).exists()


def test_session_shares_the_locale_normalized_proc_start(ctx):
    """Both heartbeats must read start-times through ONE implementation, or they drift apart again
    and only one of them survives a non-English locale."""
    assert session._proc_start is procenv.proc_start


def test_active_session_accepts_a_heartbeat_written_in_another_locale(ctx, monkeypatch):
    """Upgrade path: a supervisor started before the fix still holds a German-formatted start-time.
    Same instant, different spelling → still this session, NOT a recycled PID."""
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Sat Aug 29 16:36:54 2026")
    write_json(_path(ctx), {
        "email": "a@x.com",
        "pid": os.getpid(),
        "process_start": "Sa. 29 Aug. 16:36:54 2026",
        "started_at": "2026-01-01T00:00:00+00:00",
    })
    active = session.active_session(ctx.data_dir, "codex")
    assert active is not None and active["email"] == "a@x.com"


def test_active_session_rejects_a_different_start_time_across_locales(ctx, monkeypatch):
    """The tolerant compare must not blunt the PID-reuse guard."""
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Sat Aug 30 09:00:00 2026")
    write_json(_path(ctx), {
        "email": "stale@x.com",
        "pid": os.getpid(),
        "process_start": "Sat Aug 29 16:36:54 2026",
        "started_at": "2026-01-01T00:00:00+00:00",
    })
    assert session.active_session(ctx.data_dir, "codex") is None


def test_garbage_session_reads_as_none(ctx):
    _path(ctx).write_text("{not json")
    assert session.active_session(ctx.data_dir, "codex") is None
    assert not _path(ctx).exists()


@pytest.mark.parametrize("process_start", ["Stable Start", None])
@pytest.mark.parametrize("filename", ["session-codex.json", "session-codex-101.json"])
def test_read_session_preserves_live_record_even_without_ps(ctx, monkeypatch, process_start, filename):
    monkeypatch.setattr(session, "_alive", lambda pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: process_start)
    path = ctx.data_dir / filename
    data = {"email": "a@x.com", "pid": 101, "process_start": "Stable Start",
            "started_at": "2026-09-14T08:47:00+00:00"}
    write_json(path, data)
    before = path.read_bytes()
    assert session._read_session(path) == {k: data[k] for k in ("email", "pid", "started_at")}
    assert path.read_bytes() == before


def test_read_session_prunes_dead_process_record(ctx, monkeypatch):
    monkeypatch.setattr(session, "_alive", lambda pid: False)
    path = ctx.data_dir / "session-codex-101.json"
    write_json(path, {"email": "gone@x.com", "pid": 101, "started_at": "yesterday"})
    assert session._read_session(path) is None
    assert not path.exists()


def test_read_session_cleanup_is_best_effort(ctx, monkeypatch):
    monkeypatch.setattr(session, "_alive", lambda pid: False)
    write_json(_path(ctx), {"email": "gone@x.com", "pid": 101, "started_at": "yesterday"})

    def denied(path):
        raise PermissionError("read-only store")

    monkeypatch.setattr(Path, "unlink", denied)
    assert session._read_session(_path(ctx)) is None
    assert _path(ctx).exists()


def test_read_session_keeps_concurrent_legacy_replacement(ctx, monkeypatch):
    """Pruning an old mirror must not delete the new supervisor that replaced it during ps."""
    write_json(_path(ctx), {"email": "old@x.com", "pid": 101, "process_start": "Old Start",
                           "started_at": "yesterday"})
    replacement = {"email": "new@x.com", "pid": 202, "started_at": "today"}
    monkeypatch.setattr(session, "_alive", lambda pid: True)

    def recycled(pid):
        write_json(_path(ctx), replacement)
        return "Recycled Start"

    monkeypatch.setattr(session, "_proc_start", recycled)
    assert session._read_session(_path(ctx)) is None
    assert 'new@x.com' in _path(ctx).read_text()


@pytest.mark.parametrize("old_alive", [True, False])
def test_mark_session_upgrades_live_legacy_only(ctx, monkeypatch, old_alive):
    """Dead mirrors may be pruned during upgrade; live owners still get their hard-link snapshot."""
    monkeypatch.setattr(session, "_START_CACHE", {})
    monkeypatch.setattr(session.os, "getpid", lambda: 202)
    monkeypatch.setattr(session, "_alive", lambda pid: old_alive if pid == 101 else True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    write_json(_path(ctx), {"email": "old@x.com", "pid": 101, "process_start": "Stable Start",
                           "started_at": "2026-09-14T08:47:00+00:00"})
    session.mark_session(ctx.data_dir, "codex", "new@x.com")
    assert (ctx.data_dir / "session-codex-101.json").exists() is old_alive
    assert session._read_session(_path(ctx))["email"] == "new@x.com"


def test_session_mtime_signals_start_and_end_without_a_state_write(ctx):
    """The menubar's 3s poll keys off this. A launch on the ALREADY-active seat saves no state, so
    state.json's rev never moves — without the heartbeat mtime the live-session dot would lag by a
    whole usage poll (180s), which is exactly the staleness it exists to remove."""
    assert session.session_mtime_ns(ctx.data_dir, "codex") == 0   # no session
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    started = session.session_mtime_ns(ctx.data_dir, "codex")
    assert started > 0
    session.clear_session(ctx.data_dir, "codex")
    assert session.session_mtime_ns(ctx.data_dir, "codex") == 0   # end is visible too


def test_session_mtime_is_per_tool(ctx):
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    assert session.session_mtime_ns(ctx.data_dir, "claude") == 0


def test_terminals_keep_independent_session_records(ctx, monkeypatch):
    monkeypatch.setattr(session, "_START_CACHE", {})
    monkeypatch.setattr(session, "_alive", lambda pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    monkeypatch.setattr(session.os, "getpid", lambda: 101)
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    monkeypatch.setattr(session.os, "getpid", lambda: 202)
    session.mark_session(ctx.data_dir, "codex", "b@x.com")
    assert len(session.active_sessions(ctx.data_dir, "codex")) == 2
    assert session.active_session(ctx.data_dir, "codex", email="a@x.com")["pid"] == 101
    before = session.session_mtime_ns(ctx.data_dir, "codex")
    session.clear_session(ctx.data_dir, "codex")
    assert session.active_session(ctx.data_dir, "codex")["email"] == "a@x.com"
    assert session.session_mtime_ns(ctx.data_dir, "codex") != before
    monkeypatch.setattr(session.os, "getpid", lambda: 101)
    session.clear_session(ctx.data_dir, "codex")
    assert session.active_sessions(ctx.data_dir, "codex") == []


def test_clearing_older_terminal_does_not_delete_latest_legacy_record(ctx, monkeypatch):
    monkeypatch.setattr(session, "_START_CACHE", {})
    monkeypatch.setattr(session, "_alive", lambda pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    monkeypatch.setattr(session.os, "getpid", lambda: 101)
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    monkeypatch.setattr(session.os, "getpid", lambda: 202)
    session.mark_session(ctx.data_dir, "codex", "b@x.com")
    monkeypatch.setattr(session.os, "getpid", lambda: 101)
    session.clear_session(ctx.data_dir, "codex")
    assert _path(ctx).exists()
    assert session.active_session(ctx.data_dir, "codex")["pid"] == 202


# --- which engine supervises each terminal (app/engine drift) ------------------------------

def test_mark_session_stamps_the_supervising_engine(ctx):
    from acctsw import __version__, engine
    session.mark_session(ctx.data_dir, "claude", "a@x.com")
    record = session.active_session(ctx.data_dir, "claude")
    assert record["engine"] == __version__
    assert record["capabilities"] == list(engine.CAPABILITIES)
    assert session.stale_engines(ctx.data_dir) == []


def test_heartbeat_records_cwd_and_tty_and_passes_them_to_drift(ctx, monkeypatch, tmp_path):
    import json
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session.os, "ttyname", lambda fd: "/dev/ttys042")
    monkeypatch.setattr(session, "__version__", "1.6.1")
    session.mark_session(ctx.data_dir, "claude", "business@x.com")
    raw = json.loads(_path(ctx, "claude").read_text())
    record = session.active_session(ctx.data_dir, "claude")
    stale, = session.stale_engines(ctx.data_dir)
    for data in [raw, record, stale]:
        assert data["cwd"] == str(tmp_path)
        assert data["tty"] == "/dev/ttys042"
        assert data["email"] == "business@x.com"
        assert data["started_at"] == raw["started_at"]


def test_heartbeat_survives_unavailable_cwd_and_tty(ctx, monkeypatch):
    def unavailable(*args):
        raise OSError("no terminal or directory")
    monkeypatch.setattr(session.os, "getcwd", unavailable)
    monkeypatch.setattr(session.os, "ttyname", unavailable)
    session.mark_session(ctx.data_dir, "claude", "a@x.com")
    record = session.active_session(ctx.data_dir, "claude")
    assert record["email"] == "a@x.com"
    assert "cwd" not in record and "tty" not in record


def test_stale_engines_finds_older_and_pre_stamp_supervisors(ctx, monkeypatch):
    from acctsw import __version__
    monkeypatch.setattr(session, "_alive", lambda pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    base = {"email": "a@x.com", "process_start": "Stable Start", "started_at": "2026-10-01"}
    # A 1.1.x supervisor never stamped itself; another stamped an older version.
    write_json(ctx.data_dir / "session-claude-101.json", {**base, "pid": 101})
    write_json(ctx.data_dir / "session-claude-102.json",
               {**base, "pid": 102, "engine": "1.3.0", "capabilities": []})
    write_json(ctx.data_dir / "session-codex-103.json",
               {**base, "pid": 103, "engine": __version__,
                "capabilities": list(session.CAPABILITIES)})
    stale = sorted(session.stale_engines(ctx.data_dir), key=lambda s: s["pid"])
    where = {"email": "a@x.com", "started_at": "2026-10-01"}
    assert stale == [{"tool": "claude", "pid": 101, "engine": None, "self_restart": False, **where},
                     {"tool": "claude", "pid": 102, "engine": "1.3.0", "self_restart": False,
                      **where}]
    # Callers that already read the sessions pass them in instead of re-running ps.
    assert session.stale_engines(ctx.data_dir, {"claude": [], "codex": []}) == []


def test_stale_engines_report_which_sessions_will_restart_themselves(ctx, monkeypatch):
    monkeypatch.setattr(session, "_alive", lambda pid: True)
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    base = {"email": "a@x.com", "process_start": "Stable Start", "started_at": "2026-10-01"}
    write_json(ctx.data_dir / "session-claude-101.json",
               {**base, "pid": 101, "engine": "1.3.0", "self_restart": True})
    write_json(ctx.data_dir / "session-claude-102.json",     # a malformed heartbeat
               {**base, "pid": 102, "engine": "1.3.0", "self_restart": "yes"})
    stale = sorted(session.stale_engines(ctx.data_dir), key=lambda s: s["pid"])
    assert [s["self_restart"] for s in stale] == [True, False]


@pytest.mark.parametrize("tty_fd", [0, 1, 2])
def test_heartbeat_finds_the_terminal_on_any_standard_stream(ctx, monkeypatch, tty_fd):
    def ttyname(fd):
        if fd != tty_fd:
            raise OSError("not a terminal")
        return f"/dev/ttys00{fd}"
    monkeypatch.setattr(session.os, "ttyname", ttyname)
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    assert session.active_session(ctx.data_dir, "codex")["tty"] == f"/dev/ttys00{tty_fd}"


def test_claim_thread_updates_private_heartbeat_and_ledger(ctx, monkeypatch):
    from acctsw import seatledger
    monkeypatch.setattr(session, "_proc_start", lambda pid: "fake-start")
    session.mark_session(ctx.data_dir, "codex", "fake@example.test")
    session.claim_thread(ctx.data_dir, "codex", "discovered-thread")
    path = ctx.data_dir / f"session-codex-{os.getpid()}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["thread"] == "discovered-thread"
    assert [(r["email"], r["thread"]) for r in seatledger.read(ctx.data_dir)] == [
        ("fake@example.test", None), ("fake@example.test", "discovered-thread")]
