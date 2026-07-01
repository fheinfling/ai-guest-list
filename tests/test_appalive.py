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


def test_mark_alive_concurrent_writers_dont_race(ctx):
    """Overlapping mark_alive() calls in one process must not collide on the temp file (each uses a
    per-thread temp name); the heartbeat stays valid and no thread raises FileNotFoundError."""
    import threading
    errors = []
    def worker():
        try:
            for _ in range(40):
                appalive.mark_alive(ctx.data_dir)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert appalive.app_running(ctx.data_dir) is True


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

    def fake_execvpe(file, argv, env):
        captured["file"] = file
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        # return (don't replace the process) so the test continues

    monkeypatch.setenv("PYTHONPATH", "/frozen/app/lib/python311.zip")
    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(ctx, "codex_bin", "/usr/bin/codex")
    rc = launcher.exec_stock(ctx, "codex", ["exec", "hi"])
    assert captured["file"] == "/usr/bin/codex"
    assert captured["argv"] == ["/usr/bin/codex", "exec", "hi"]
    # the frozen-app interpreter vars must be stripped from the child's env (the whole point of the fix)
    assert "PYTHONPATH" not in captured["env"]
    assert rc == 127  # only reached because the fake execvpe returned


def test_exec_stock_returns_127_when_binary_missing(ctx, monkeypatch):
    def boom(file, argv, env):
        raise OSError("no such file")

    monkeypatch.setattr(launcher.os, "execvpe", boom)
    assert launcher.exec_stock(ctx, "codex", []) == 127


# --- cli `run` gate ---------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    c = Context.for_test(tmp_path)
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: c))
    return c


def test_run_gate_execs_stock_when_app_closed(isolated, monkeypatch):
    from acctsw import headroom
    calls = {"stock": None, "supervised": False}
    monkeypatch.setattr(headroom, "needs_reconcile", lambda ctx: False)  # Headroom never used → hermetic
    monkeypatch.setattr(launcher, "exec_stock",
                        lambda ctx, tool, args: calls.__setitem__("stock", (tool, args)) or 0)
    monkeypatch.setattr(launcher, "run",
                        lambda *a, **k: calls.__setitem__("supervised", True) or 0)
    # no heartbeat written → app is "closed"
    cli.main(["run", "codex", "--", "exec", "hi"])
    assert calls["stock"] == ("codex", ["exec", "hi"])
    assert calls["supervised"] is False


def test_run_gate_runs_stock_when_app_open_but_no_seats(isolated, monkeypatch):
    """Fresh install: app open (aliases wired) but no seats yet. launch() raises NoSeats — we must
    fall back to stock instead of erroring, so plain codex/claude still work."""
    calls = {"stock": None}
    monkeypatch.setattr(launcher, "exec_stock",
                        lambda ctx, tool, args: calls.__setitem__("stock", (tool, args)) or 0)
    appalive.mark_alive(isolated.data_dir)        # app open
    # isolated Context has no seats → real launcher.run raises NoSeats
    cli.main(["run", "codex", "hi"])
    assert calls["stock"] == ("codex", ["hi"])


def test_run_gate_self_heals_headroom_before_exec_when_app_closed(isolated, monkeypatch):
    """App closed + Headroom was used → strip dangling routing / reap an orphan proxy BEFORE running
    stock, so codex/claude don't exec into a dead-or-foreign proxy (ConnectionRefused)."""
    from acctsw import headroom
    order = []
    monkeypatch.setattr(headroom, "needs_reconcile", lambda ctx: True)
    monkeypatch.setattr(headroom, "heal",
                        lambda data_dir, **k: order.append(("heal", k)) or (True, "x"))
    monkeypatch.setattr(launcher, "exec_stock",
                        lambda *a, **k: order.append(("stock", None)) or 0)
    cli.main(["run", "codex", "hi"])
    assert order == [("heal", {"blocking": False, "app_running": False}), ("stock", None)]


def test_run_gate_skips_heal_when_headroom_never_used(isolated, monkeypatch):
    """Hot path: no Headroom state AND no live proxy → skip the heal entirely and exec stock."""
    from acctsw import headroom
    called = {"heal": False}
    monkeypatch.setattr(headroom, "needs_reconcile", lambda ctx: False)
    monkeypatch.setattr(headroom, "proxy_maybe_running", lambda data_dir: False)
    monkeypatch.setattr(headroom, "heal", lambda *a, **k: called.__setitem__("heal", True) or (False, ""))
    monkeypatch.setattr(launcher, "exec_stock", lambda *a, **k: 0)
    cli.main(["run", "codex", "hi"])
    assert called["heal"] is False


def test_run_gate_reaps_orphan_proxy_via_pidfile_when_reconcile_false(isolated, monkeypatch):
    """Graceful-OFF deletes the backup → needs_reconcile False, but a live proxy pidfile must still
    trigger the reap (otherwise the orphan leaks — the exact bug this gate exists to fix)."""
    from acctsw import headroom
    called = {"heal": False}
    monkeypatch.setattr(headroom, "needs_reconcile", lambda ctx: False)
    monkeypatch.setattr(headroom, "proxy_maybe_running", lambda data_dir: True)
    monkeypatch.setattr(headroom, "heal",
                        lambda data_dir, **k: called.__setitem__("heal", True) or (True, "x"))
    monkeypatch.setattr(launcher, "exec_stock", lambda *a, **k: 0)
    cli.main(["run", "codex", "hi"])
    assert called["heal"] is True


def test_run_gate_blocking_retry_when_busy_and_still_injected(isolated, monkeypatch):
    """If a non-blocking heal is busy (e.g. quit teardown holds the lock) AND routing is still
    injected, retry blocking — never exec stock into a half-torn-down proxy (ConnectionRefused)."""
    from acctsw import headroom
    calls = []
    monkeypatch.setattr(headroom, "needs_reconcile", lambda ctx: True)
    monkeypatch.setattr(headroom, "routing_injected", lambda: True)
    monkeypatch.setattr(headroom, "heal",
                        lambda data_dir, **k: calls.append(k.get("blocking")) or (False, "busy"))
    monkeypatch.setattr(launcher, "exec_stock", lambda *a, **k: 0)
    cli.main(["run", "codex", "hi"])
    assert calls == [False, True]   # fast attempt, then a blocking retry


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
