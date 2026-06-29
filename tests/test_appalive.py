"""The app is the master switch: cx/cl supervise only while the menubar app is alive.

Covers the PID heartbeat (appalive) and the cli `run` gate that execs the stock tool when the
app is closed.
"""
import os
import subprocess

import pytest

from acctsw import appalive, cli, launcher
from acctsw.context import Context


# --- heartbeat --------------------------------------------------------------------------------

def test_app_running_false_when_no_heartbeat(ctx):
    assert appalive.app_running(ctx.data_dir) is False


def test_mark_alive_then_running(ctx):
    appalive.mark_alive(ctx.data_dir)
    assert appalive.app_running(ctx.data_dir) is True


def test_mark_dead_clears(ctx):
    appalive.mark_alive(ctx.data_dir)
    appalive.mark_dead(ctx.data_dir)
    assert appalive.app_running(ctx.data_dir) is False


def test_mark_dead_is_idempotent_when_missing(ctx):
    appalive.mark_dead(ctx.data_dir)  # no file yet → no error
    assert appalive.app_running(ctx.data_dir) is False


def test_stale_dead_pid_reads_as_closed(ctx):
    """A crash leaves a heartbeat with a dead PID → must read as closed (degrade to stock)."""
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead.wait()
    (ctx.data_dir / "app.pid").write_text(str(dead.pid))
    assert appalive.app_running(ctx.data_dir) is False


def test_garbage_heartbeat_reads_as_closed(ctx):
    (ctx.data_dir / "app.pid").write_text("not-a-pid")
    assert appalive.app_running(ctx.data_dir) is False


def test_recycled_pid_reads_as_closed(ctx):
    """A live PID whose recorded start-time doesn't match (the OS recycled the PID for an unrelated
    process after a crash) must read as closed — not as the app still running."""
    (ctx.data_dir / "app.pid").write_text(f"{os.getpid()}\nNot The Real Start Time")
    assert appalive.app_running(ctx.data_dir) is False


def test_legacy_pidfile_without_start_falls_back_to_liveness(ctx):
    """A heartbeat with only a PID (no recorded start-time) still works via liveness."""
    (ctx.data_dir / "app.pid").write_text(str(os.getpid()))
    assert appalive.app_running(ctx.data_dir) is True


# --- exec_stock -------------------------------------------------------------------------------

def test_exec_stock_execs_the_stock_binary(ctx, monkeypatch):
    captured = {}

    def fake_execvp(file, argv):
        captured["file"] = file
        captured["argv"] = list(argv)
        # return (don't replace the process) so the test continues

    monkeypatch.setattr(launcher.os, "execvp", fake_execvp)
    monkeypatch.setattr(ctx, "codex_bin", "/usr/bin/codex")
    rc = launcher.exec_stock(ctx, "codex", ["exec", "hi"])
    assert captured["file"] == "/usr/bin/codex"
    assert captured["argv"] == ["/usr/bin/codex", "exec", "hi"]
    assert rc == 127  # only reached because the fake execvp returned


def test_exec_stock_returns_127_when_binary_missing(ctx, monkeypatch):
    def boom(file, argv):
        raise OSError("no such file")

    monkeypatch.setattr(launcher.os, "execvp", boom)
    assert launcher.exec_stock(ctx, "codex", []) == 127


# --- cli `run` gate ---------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    c = Context.for_test(tmp_path)
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: c))
    return c


def test_run_gate_execs_stock_when_app_closed(isolated, monkeypatch):
    calls = {"stock": None, "supervised": False}
    monkeypatch.setattr(launcher, "exec_stock",
                        lambda ctx, tool, args: calls.__setitem__("stock", (tool, args)) or 0)
    monkeypatch.setattr(launcher, "run",
                        lambda *a, **k: calls.__setitem__("supervised", True) or 0)
    # no heartbeat written → app is "closed"
    cli.main(["run", "codex", "--", "exec", "hi"])
    assert calls["stock"] == ("codex", ["exec", "hi"])
    assert calls["supervised"] is False


def test_run_gate_supervises_when_app_open(isolated, monkeypatch):
    calls = {"stock": False, "supervised": False}
    monkeypatch.setattr(launcher, "exec_stock",
                        lambda *a, **k: calls.__setitem__("stock", True) or 0)
    monkeypatch.setattr(launcher, "run",
                        lambda *a, **k: calls.__setitem__("supervised", True) or 0)
    appalive.mark_alive(isolated.data_dir)   # app is open
    cli.main(["run", "codex", "hi"])
    assert calls["supervised"] is True
    assert calls["stock"] is False
