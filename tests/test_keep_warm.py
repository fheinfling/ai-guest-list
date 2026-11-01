"""Idle Claude renewal through an injected CLI only; never execute the real Claude binary."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from acctsw import bridge, claudehome, session
from acctsw.util import iso

AT = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def blob(minutes=10, **overrides):
    oauth = {"accessToken": "access", "refreshToken": "refresh",
             "expiresAt": (AT + timedelta(minutes=minutes)).timestamp() * 1000}
    oauth.update(overrides)
    return json.dumps({"claudeAiOauth": oauth})


def seed(ctx, email="parked@test", *, minutes=10, **fields):
    state = ctx.load_state()
    state.upsert_seat("claude", email).update(fields)
    state.save()
    home = ctx.claude_home(email)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".acctsw-seeded").touch()
    ctx.claude_cred(email).set_live(blob(minutes))
    return home


def warm_record(ctx, email="parked@test"):
    return ctx.load_state().get_seat("claude", email).get("warm")


@pytest.fixture(autouse=True)
def forbid_default_cli(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("keep-warm test tried to run the real Claude binary")
    # Replace this module's dependency, not subprocess.run shared by the whole test suite.
    monkeypatch.setattr(claudehome, "subprocess",
                        SimpleNamespace(run=forbidden, DEVNULL=subprocess.DEVNULL))


def test_picks_only_parked_idle_expiring_seat_and_records_success(ctx, monkeypatch):
    active = seed(ctx, "active@test")
    billed = seed(ctx, "billed@test")
    homed = seed(ctx, "homed@test")
    home = seed(ctx)
    state = ctx.load_state()
    state.set_active("claude", "active@test")
    state.save()
    session.mark_session(ctx.data_dir, "claude", "billed@test", home="homed@test")
    calls = []
    ctx.claude_bin = "/fake/bin/claude"
    monkeypatch.setenv("WARM_TEST_INHERITED", "present")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other/home")

    def renew(argv, **kwargs):
        calls.append((argv, kwargs))
        assert not ctx._store_lock_held()
        assert argv == ["/fake/bin/claude", "mcp", "list"]
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(home)
        assert kwargs["env"]["WARM_TEST_INHERITED"] == "present"
        assert kwargs["env"] is not os.environ
        assert Path(kwargs["cwd"]).is_dir()
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        assert Path(kwargs["cwd"]) not in (active, billed, homed, home, Path.cwd())
        assert kwargs["timeout"] == 60
        assert all(kwargs[k] == subprocess.DEVNULL for k in ("stdin", "stdout", "stderr"))
        # A concurrent state write during the slow child must survive the attempt record.
        with ctx.locked():
            current = ctx.load_state()
            current.get_seat("claude", "parked@test")["name"] = "renamed while warming"
            current.save()
        ctx.claude_cred("parked@test").set_live(blob(480))
        return SimpleNamespace(returncode=0)

    assert claudehome.keep_warm(ctx, AT, run=renew) == "parked@test"
    assert len(calls) == 1
    assert not Path(calls[0][1]["cwd"]).exists()
    assert os.environ["CLAUDE_CONFIG_DIR"] == "/other/home"
    assert warm_record(ctx) == {"at": iso(AT), "ok": True, "fails": 0}
    assert ctx.load_state().get_seat("claude", "parked@test")["name"] == "renamed while warming"
    for email in ("active@test", "billed@test", "homed@test"):
        assert warm_record(ctx, email) is None


@pytest.mark.parametrize("reason", [
    "active", "billed", "homed", "quarantined", "signed_out", "missing_home", "unseeded",
    "non_expiring", "recent", "setting_off", "no_refresh", "no_access", "no_item",
    "unknown_expiry", "invalid_expiry", "boolean_expiry",
])
def test_skips_ineligible_seat(ctx, reason):
    home = seed(ctx)
    state = ctx.load_state()
    seat = state.get_seat("claude", "parked@test")
    if reason == "active":
        state.set_active("claude", "parked@test")
    elif reason in ("billed", "homed"):
        session.mark_session(ctx.data_dir, "claude",
                             "PARKED@test" if reason == "billed" else "other@test",
                             home="PARKED@test" if reason == "homed" else None)
    elif reason == "quarantined":
        (home / ".acctsw-quarantined").touch()
    elif reason == "signed_out":
        seat["signin"] = {"signed_out": True}
    elif reason in ("missing_home", "unseeded"):
        (home / ".acctsw-seeded").unlink()
        if reason == "missing_home":
            home.rmdir()
    elif reason == "non_expiring":
        ctx.claude_cred("parked@test").set_live(blob(30.01))
    elif reason == "recent":
        seat["warm"] = {"at": iso(AT - timedelta(hours=3)), "ok": True, "fails": 0}
    elif reason == "setting_off":
        state.set_setting("keep_warm", False)
    elif reason == "no_item":
        item = ctx.claude_cred("parked@test")
        ctx.keychain.delete(item.service, item.account)
    else:
        key, value = {"no_refresh": ("refreshToken", ""), "no_access": ("accessToken", ""),
                      "unknown_expiry": ("expiresAt", None),
                      "invalid_expiry": ("expiresAt", float("nan")),
                      "boolean_expiry": ("expiresAt", True)}[reason]
        ctx.claude_cred("parked@test").set_live(blob(**{key: value}))
    state.save()
    calls = []
    before = warm_record(ctx)
    assert claudehome.keep_warm(ctx, AT, run=lambda *a, **kw: calls.append(a)) is None
    assert calls == []
    assert warm_record(ctx) == before


@pytest.mark.parametrize("minutes", [-480, 0, 30])
def test_expired_and_thirty_minute_tokens_are_due(ctx, minutes):
    seed(ctx, minutes=minutes)
    calls = []
    assert claudehome.keep_warm(ctx, AT, run=lambda *a, **kw: calls.append(a)) == "parked@test"
    assert len(calls) == 1


def test_failure_backoff_and_success_reset(ctx):
    seed(ctx)
    calls = []
    def unchanged(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    assert claudehome.keep_warm(ctx, AT, run=unchanged) == "parked@test"
    assert warm_record(ctx) == {"at": iso(AT), "ok": False, "fails": 1}
    assert claudehome.keep_warm(ctx, AT + timedelta(hours=3), run=unchanged) is None
    second = AT + timedelta(hours=3, seconds=1)
    assert claudehome.keep_warm(ctx, second, run=unchanged) == "parked@test"
    assert warm_record(ctx) == {"at": iso(second), "ok": False, "fails": 2}
    assert claudehome.keep_warm(ctx, second + timedelta(hours=4), run=unchanged) is None
    assert claudehome.keep_warm(ctx, second + timedelta(hours=24), run=unchanged) is None
    assert len(calls) == 2

    def renew(*_args, **_kwargs):
        ctx.claude_cred("parked@test").set_live(blob(3000))
        return SimpleNamespace(returncode=1)  # expiry movement, not exit status, proves renewal
    third = second + timedelta(hours=24, seconds=1)
    assert claudehome.keep_warm(ctx, third, run=renew) == "parked@test"
    assert warm_record(ctx) == {"at": iso(third), "ok": True, "fails": 0}


@pytest.mark.parametrize("error", [OSError("fake failure"), subprocess.TimeoutExpired("fake", 60)])
def test_runner_exceptions_never_escape_and_count_as_failure(ctx, error, caplog):
    seed(ctx)
    def fail(*_args, **_kwargs):
        raise error
    assert claudehome.keep_warm(ctx, AT, run=fail) == "parked@test"
    assert warm_record(ctx) == {"at": iso(AT), "ok": False, "fails": 1}
    assert "keep-warm runner failed" in caplog.text


def test_earlier_expiry_is_not_success(ctx):
    seed(ctx)
    def earlier(*_args, **_kwargs):
        ctx.claude_cred("parked@test").set_live(blob(-1))
    assert claudehome.keep_warm(ctx, AT, run=earlier) == "parked@test"
    assert warm_record(ctx)["ok"] is False


def test_only_one_seat_per_call(ctx):
    seed(ctx, "first@test")
    seed(ctx, "second@test")
    calls = []
    def renew(_argv, **kwargs):
        calls.append(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        email = next(e for e in ("first@test", "second@test")
                     if str(ctx.claude_home(e)) == calls[-1])
        ctx.claude_cred(email).set_live(blob(480))
    first = claudehome.keep_warm(ctx, AT, run=renew)
    assert len(calls) == 1
    assert sum(warm_record(ctx, e) is not None for e in ("first@test", "second@test")) == 1
    second = claudehome.keep_warm(ctx, AT, run=renew)
    assert first != second and {first, second} == {"first@test", "second@test"}
    assert len(calls) == 2


def test_default_runner_and_clock_are_injectable(ctx, monkeypatch):
    seed(ctx)
    calls = []
    monkeypatch.setattr(claudehome, "now", lambda: AT)
    monkeypatch.setattr(claudehome.subprocess, "run", lambda *a, **kw: calls.append(a))
    assert claudehome.keep_warm(ctx) == "parked@test"
    assert calls == [(["claude", "mcp", "list"],)]
    assert warm_record(ctx)["at"] == iso(AT)


@pytest.mark.parametrize("failure", ["state", "sessions", "credential", "temp", "save"])
def test_maintenance_errors_never_escape(ctx, monkeypatch, failure, caplog):
    seed(ctx)
    def fail(*_args, **_kwargs):
        raise OSError("secret credential bytes must not be logged")
    if failure == "state":
        monkeypatch.setattr(ctx, "load_state", fail)
    elif failure == "sessions":
        monkeypatch.setattr(session, "active_sessions", fail)
    elif failure == "credential":
        monkeypatch.setattr(ctx, "claude_cred", fail)
    elif failure == "temp":
        monkeypatch.setattr(claudehome.tempfile, "TemporaryDirectory", fail)
    else:
        from acctsw.state import State
        monkeypatch.setattr(State, "save", fail)
    claudehome.keep_warm(ctx, AT, run=lambda *_a, **_kw: None)
    assert "keep-warm" in caplog.text
    assert "secret credential bytes" not in caplog.text


def test_setting_defaults_on_and_bridge_toggle_persists(ctx):
    assert ctx.load_state().settings()["keep_warm"] is True
    for value in (False, True):
        result = bridge.handle(ctx, {"action": "toggle", "key": "keep_warm", "value": value})
        assert result["ok"] is True
        assert result["state"]["settings"]["keep_warm"] is value
        assert ctx.load_state().settings()["keep_warm"] is value


@pytest.mark.keep_warm_poll
def test_bridge_poll_does_not_wait_and_prevents_overlap(ctx, monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(bridge, "_KEEP_WARM_LOCK", lock)
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    workers, warmed, refreshed = [], [], []
    def fake_warm(context):
        assert refreshed, "refresh_live must finish before warming"
        warmed.append(context)
        started.set()
        assert release.wait(5)
    def thread_factory(**kwargs):
        assert kwargs["daemon"] is True
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(bridge, "threading", SimpleNamespace(Thread=thread_factory))
    monkeypatch.setattr(claudehome, "keep_warm", fake_warm)
    monkeypatch.setattr(bridge.usage_mod, "refresh_live", lambda *a, **kw: refreshed.append(1) or {})
    monkeypatch.setattr(bridge, "_auto_switch_after_usage", lambda *_a: None)
    monkeypatch.setattr(bridge, "snapshot_state", lambda *_a: {})
    results = []
    def poll():
        results.append(bridge.handle(ctx, {"action": "usage"}))
        returned.set()
    caller = threading.Thread(target=poll, daemon=True)
    try:
        caller.start()
        assert started.wait(2)
        assert returned.wait(2), "poll waited for the blocked maintenance worker"
        assert results[0]["ok"] is True
        assert lock.locked()
        assert bridge.handle(ctx, {"action": "usage"})["ok"] is True
        assert len(workers) == 1 and warmed == [ctx]
    finally:
        release.set()
        caller.join(2)
        for worker in workers:
            worker.join(2)
    assert not lock.locked()
    assert bridge.handle(ctx, {"action": "usage"})["ok"] is True
    workers[-1].join(2)
    assert len(workers) == 2 and warmed == [ctx, ctx]
    assert not lock.locked()


@pytest.mark.keep_warm_poll
@pytest.mark.parametrize("failure", ["start", "worker"])
def test_bridge_releases_lock_on_errors(ctx, monkeypatch, failure, caplog):
    lock = threading.Lock()
    monkeypatch.setattr(bridge, "_KEEP_WARM_LOCK", lock)
    workers = []
    def fail(*_args, **_kwargs):
        raise RuntimeError("fake worker error")
    def thread_factory(**kwargs):
        if failure == "start":
            return SimpleNamespace(start=fail)
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(bridge, "threading", SimpleNamespace(Thread=thread_factory))
    monkeypatch.setattr(claudehome, "keep_warm", fail)
    bridge._start_keep_warm(ctx)
    for worker in workers:
        worker.join(2)
    assert not lock.locked()
    assert "keep-warm" in caplog.text


@pytest.mark.keep_warm_poll
def test_codex_only_poll_does_not_start_claude_warmer(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "_start_keep_warm", lambda _ctx: calls.append(1))
    monkeypatch.setattr(bridge.usage_mod, "refresh_live", lambda *_a, **_kw: {})
    assert bridge.handle(ctx, {"action": "usage", "tool": "codex"})["ok"] is True
    assert calls == []
