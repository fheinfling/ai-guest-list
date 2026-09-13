"""Tests for the native shell's pure helpers: dot selection + sync-back-before-login."""
import json
from pathlib import Path

from acctsw import accounts as acct
from acctsw import install as inst
from acctsw.web_dot import dot_for, door_for
from app.menubar import (bootstrap_supervision, usage_poll_interval,
                         USAGE_POLL_OPEN_SECONDS, USAGE_POLL_HIDDEN_SECONDS,
                         request_codex_restart, launch_codex_desktop, usage_poll_scope)
from app import terminal
from tests.conftest import make_codex_blob

FIXTURE = Path(__file__).parent / "fixtures" / "dot_cases.json"
DOOR_FIXTURE = Path(__file__).parent / "fixtures" / "door_cases.json"


def test_usage_poll_cadence_is_fast_only_while_the_popover_is_open():
    """The native timer may be responsive while visible without hammering provider endpoints hidden."""
    assert usage_poll_interval(True) == USAGE_POLL_OPEN_SECONDS == 30.0
    assert usage_poll_interval(False) == USAGE_POLL_HIDDEN_SECONDS == 180.0


def test_open_popover_keeps_a_full_seat_sweep_on_the_hidden_cadence():
    assert usage_poll_scope(True, 100.0, at=129.0) == "active"
    assert usage_poll_scope(True, 100.0, at=280.0) == "all"
    assert usage_poll_scope(False, 270.0, at=271.0) == "all"


class _RunningApp:
    def __init__(self, *, name="Codex", bundle="com.openai.codex", accepts=True):
        self.name, self.bundle, self.accepts = name, bundle, accepts
        self.terminate_calls = 0

    def localizedName(self):
        return self.name

    def bundleIdentifier(self):
        return self.bundle

    def terminate(self):
        self.terminate_calls += 1
        return self.accepts


def test_codex_restart_requests_a_graceful_quit_without_touching_other_apps():
    codex = _RunningApp()
    other = _RunningApp(name="Safari", bundle="com.apple.Safari")

    assert request_codex_restart([other, codex]) == (True, None)
    assert codex.terminate_calls == 1 and other.terminate_calls == 0


def test_codex_restart_does_not_launch_over_a_quit_that_was_refused():
    codex = _RunningApp(accepts=False)

    running, error = request_codex_restart([codex])

    assert running is True and "did not accept" in error
    assert codex.terminate_calls == 1


def test_launch_codex_reports_failures_and_does_not_raise():
    def fail(_argv, **_kwargs):
        raise OSError("open is unavailable")

    assert launch_codex_desktop(run=fail) == "open is unavailable"


def test_launch_codex_surfaces_a_nonzero_open_result():
    class FailedOpen:
        returncode = 1
        stderr = "Application not found"

    assert launch_codex_desktop(run=lambda *_args, **_kwargs: FailedOpen()) == "Application not found"


def test_bootstrap_repairs_missing_rc_block_and_stays_idempotent(ctx):
    """An old sentinel cannot suppress repair of the maintainer's wrappers-only failure state."""
    (ctx.data_dir / ".cli-bootstrapped").write_text("")
    inst.ensure_launchers(wire_rc=False)
    rc = inst.shell_rc_path()
    assert not rc.exists()

    first = bootstrap_supervision(ctx, lambda *_args: None)
    assert first["ok"] is True and first["changed"] is True
    assert inst.supervision_status()["active"] is True
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1

    second = bootstrap_supervision(ctx, lambda *_args: None)
    assert second["ok"] is True and second["changed"] is False
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1


def test_bootstrap_honours_explicit_supervision_opt_out(ctx):
    state = ctx.load_state()
    state.set_setting("supervise_shell", False)
    state.save()
    (ctx.data_dir / ".cli-bootstrapped").write_text("")

    result = bootstrap_supervision(ctx, lambda *_args: None)

    assert result["ok"] is True
    assert inst.supervision_status()["wrappers"] is True   # wrappers still heal every launch
    assert inst.supervision_status()["block"] is False
    assert not inst.shell_rc_path().exists()


def test_bootstrap_failure_is_reported_without_raising(ctx, monkeypatch):
    notices = []

    def fail(**_kwargs):
        raise PermissionError("rc is read-only")

    monkeypatch.setattr(inst, "ensure_launchers", fail)
    result = bootstrap_supervision(ctx, lambda title, text: notices.append((title, text)))

    assert result["ok"] is False
    assert "rc is read-only" in result["error"]
    assert notices and "needs attention" in notices[0][0]
    assert "rc is read-only" in notices[0][1]


def test_dot_for_golden_fixture():
    """The SAME fixture is asserted by the node UI tests → python/JS dot logic can't drift."""
    cases = json.loads(FIXTURE.read_text())
    for c in cases:
        assert dot_for(c["state"]) == c["expected"], c["name"]


def test_door_for_golden_fixture():
    """Door open/shut — same fixture asserted by node UI tests so python/JS can't drift."""
    cases = json.loads(DOOR_FIXTURE.read_text())
    for c in cases:
        assert door_for(c["state"]) == c["expected"], c["name"]


def test_prepare_then_login_syncs_back_active_before_login(ctx, monkeypatch):
    """The invariant: the active seat's (rotated) live creds are snapshotted BEFORE login runs."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    st = ctx.load_state()
    acct.add(ctx, st, "codex", email="a@x.com")
    # rotate live token (as a session would) but DON'T snapshot it yet
    rotated = make_codex_blob("a@x.com").replace('"refresh_token": "r"', '"refresh_token": "ROT"')
    ctx.cred["codex"].set_live(rotated)

    opened = {}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: opened.setdefault("cmd", cmd))
    terminal.prepare_then_login(ctx, "codex", "codex login")

    # sync-back happened before the (mocked) login
    snap = json.loads(ctx.snapshot_get("codex", "a@x.com"))
    assert snap["tokens"]["refresh_token"] == "ROT"
    assert opened["cmd"] == "codex login"


def test_prepare_then_login_resolves_absolute_command_when_none(ctx, monkeypatch):
    """With no explicit command, prepare_then_login resolves the CLI's absolute path and launches it."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.codex_bin = "/opt/homebrew/bin/codex"
    opened = {}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: opened.setdefault("cmd", cmd))
    terminal.prepare_then_login(ctx, "codex")  # command defaults to None → resolve
    assert opened["cmd"] == "/opt/homebrew/bin/codex login"


def test_prepare_then_login_unresolved_cli_falls_back_to_bare(ctx, monkeypatch):
    """An unresolved CLI (rc-only shim not on the GUI PATH) with an inconclusive probe must NOT block
    sign-in: fall back to the bare command, which the login+interactive shell resolves from rc."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.codex_bin = None
    opened = {}
    monkeypatch.setattr(terminal, "open_in_terminal", lambda cmd: opened.setdefault("cmd", cmd))
    terminal.prepare_then_login(ctx, "codex")
    assert opened["cmd"] == "codex login"  # launched with the bare name, not aborted
