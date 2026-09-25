"""The supervised-session heartbeat rejects stale and recycled PIDs."""
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


def test_mark_session_updates_active_seat(ctx):
    session.mark_session(ctx.data_dir, "codex", "a@x.com")
    session.mark_session(ctx.data_dir, "codex", "b@x.com")
    assert session.active_session(ctx.data_dir, "codex")["email"] == "b@x.com"


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
