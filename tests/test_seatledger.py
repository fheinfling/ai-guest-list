import json
import os
import stat
from datetime import datetime

from acctsw import seatledger, session


def test_append_permissions_and_tolerant_read(tmp_path):
    seatledger.record(tmp_path, "claude", "fake@example.test", pid=42, thread="fake-thread")
    path = tmp_path / "seat-ledger.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with path.open("ab") as stream:
        stream.write(b'garbage\n[]\n{"v":1}\n\xff\n{"v":1')
    rows = seatledger.read(tmp_path)
    assert len(rows) == 1
    assert rows[0] == dict(v=1, at=rows[0]["at"], tool="claude", email="fake@example.test",
                          pid=42, thread="fake-thread", event="bind")
    assert datetime.fromisoformat(rows[0]["at"]).utcoffset() is not None
    assert seatledger.read(tmp_path / "missing") == []


def test_append_uses_one_write_and_oserror_is_swallowed(tmp_path, monkeypatch, caplog):
    calls = []
    original = os.write
    def write(fd, data):
        calls.append(data)
        return original(fd, data)
    monkeypatch.setattr(os, "write", write)
    seatledger.record(tmp_path, "codex", "fake", pid=2)
    assert len(calls) == 1 and calls[0].endswith(b"\n")
    monkeypatch.setattr(os, "open", lambda *args: (_ for _ in ()).throw(OSError("secret path")))
    seatledger.record(tmp_path, "codex", "fake", pid=2)
    assert "Could not append seat ledger" in caplog.text
    assert "secret path" not in caplog.text


def test_session_binds_hops_attaches_and_ends(ctx, monkeypatch):
    monkeypatch.setattr(session, "_proc_start", lambda pid: "fake-start")
    session.mark_session(ctx.data_dir, "codex", "first", thread=None)
    session.claim_thread(ctx.data_dir, "codex", "thread")
    session.mark_session(ctx.data_dir, "codex", "second", thread="thread", home="first")
    session.clear_session(ctx.data_dir, "codex")
    rows = seatledger.read(ctx.data_dir)
    assert [(r["email"], r["thread"], r["event"]) for r in rows] == [
        ("first", None, "bind"), ("first", "thread", "bind"),
        ("second", "thread", "bind"), ("second", "thread", "end")]
    assert all(r["pid"] == os.getpid() for r in rows)


def test_existing_ledger_permissions_are_tightened(tmp_path):
    path = tmp_path / "seat-ledger.jsonl"
    path.write_text("")
    path.chmod(0o644)
    seatledger.record(tmp_path, "claude", "fake", pid=1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_record_creates_missing_parent_and_read_waits_for_complete_line(tmp_path):
    directory = tmp_path / "missing" / "nested"
    seatledger.record(directory, "codex", "fake@example.test", pid=42, thread="thread")
    path = directory / "seat-ledger.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = seatledger.read(directory)[0]
    # Even a valid JSON value isn't published until its terminating newline arrives.
    with path.open("ab") as stream:
        stream.write(json.dumps({**first, "email": "next@example.test"}).encode())
    assert seatledger.read(directory) == [first]
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert [r["email"] for r in seatledger.read(directory)] == ["fake@example.test", "next@example.test"]


def test_record_releases_its_file_descriptor(tmp_path, monkeypatch):
    import pytest
    opened = []
    original = os.open

    def tracked_open(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(seatledger.os, "open", tracked_open)
    try:
        seatledger.record(tmp_path, "codex", "fake@example.test", pid=42)
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
