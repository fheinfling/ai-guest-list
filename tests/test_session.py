"""The supervised-session heartbeat rejects stale and recycled PIDs."""
import os
import stat
import subprocess

from acctsw import session
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


def test_garbage_session_reads_as_none(ctx):
    _path(ctx).write_text("{not json")
    assert session.active_session(ctx.data_dir, "codex") is None


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
