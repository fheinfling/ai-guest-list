"""Isolated browser logins and local-only wrapper logout; no real CLI, HTTP or Keychain."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from acctsw import accounts, cli, identity, launcher
from acctsw.claudehome import service_name
from acctsw.credlocations import CodexCredLocation
from tests.conftest import make_codex_blob, make_claude_blob

A, B = "alpha@example.test", "beta@example.test"


def blob(tool, email):
    if tool == "codex":
        return make_codex_blob(email, "shared-workspace", user_id=email.split("@")[0])
    data = json.loads(make_claude_blob())
    data["claudeAiOauth"].update(accessToken="fake-access-" + email.split("@")[0],
                                 refreshToken="fake-refresh-" + email.split("@")[0])
    return json.dumps(data)


def seed(ctx, tool, email):
    value = blob(tool, email)
    accounts.add(ctx, ctx.load_state(), tool, email=email, blob=value)
    ctx.set_live(tool, value)
    return value


@pytest.fixture
def fakes(ctx, monkeypatch):
    calls = SimpleNamespace(logins=[], revokes=[], homes=[], services=[], fail=None, owner=A, login_blob=None)
    ctx.codex_bin, ctx.claude_bin = "/fake/bin/codex", "/fake/bin/claude"
    monkeypatch.setattr(identity, "claude_profile_email",
                        lambda value, *a, **k: calls.owner if value == calls.login_blob else
                        next((e for e in (A, B) if value == blob("claude", e)), None))

    def login(argv, *, env):
        assert not ctx._store_lock_held()
        tool = "codex" if argv[0] in (ctx.codex_bin, "codex") else "claude"
        assert argv[:2] == [ctx.codex_bin or "codex", "login"] if tool == "codex" else argv[:3] == [ctx.claude_bin or "claude", "auth", "login"]
        home = Path(env["CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"])
        assert home.parent == ctx.data_dir and home.stat().st_mode & 0o777 == 0o700
        assert not list(home.iterdir())
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
        calls.logins.append((argv, env))
        calls.homes.append(home)
        value = calls.login_blob or (blob(tool, calls.owner) if calls.owner else "{}")
        if tool == "codex":
            CodexCredLocation(home / "auth.json").set_live(value)
        else:
            service = service_name(str(home))
            calls.services.append(service)
            ctx.keychain.set_raw(service, ctx.cred[tool].account, value)
        if calls.fail:
            raise calls.fail("fake-private-diagnostic")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", login)
    monkeypatch.setattr(launcher.os, "execvpe", lambda *a: calls.revokes.append(a))
    monkeypatch.setattr(cli.Context, "default", lambda: ctx)
    return calls


def assert_clean(ctx, calls):
    assert calls.homes and all(not p.exists() for p in calls.homes)
    assert all(ctx.keychain.get_raw(s, ctx.cred["claude"].account) is None for s in calls.services)
    assert calls.revokes == []


@pytest.mark.parametrize("tool,logout,login", [
    ("codex", ["logout"], ["login"]),
    ("claude", ["auth", "logout"], ["auth", "login"]),
])
def test_incident_logout_then_isolated_login(ctx, fakes, capsys, tool, logout, login):
    before = seed(ctx, tool, B)
    assert launcher.run(ctx, tool, logout) == 0
    assert fakes.revokes == []
    assert ctx.cred[tool].get_live() is None
    assert ctx.snapshot_get(tool, B) == before
    if tool == "claude":
        assert ctx.claude_cred(B).get_live() == before
    assert launcher.run(ctx, tool, login) == 0
    assert ctx.snapshot_get(tool, A) == blob(tool, A)
    assert ctx.snapshot_get(tool, B) == before
    assert ctx.cred[tool].get_live() == blob(tool, A)
    assert ctx.load_state().active(tool) == A
    assert_clean(ctx, fakes)
    output = capsys.readouterr()
    assert B in output.out + output.err and "keeps its login" in output.out + output.err
    assert f"✓ seat saved — {tool}:{A} (now on the floor)" in output.out
    assert before not in output.out + output.err
    assert "fake-access" not in output.out + output.err
    assert "fake-refresh" not in output.out + output.err


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("owner", [A, None])
def test_signin_mismatch_or_unknown_writes_nothing(ctx, fakes, tool, owner):
    before = seed(ctx, tool, B)
    state = ctx.state_file.read_bytes()
    fakes.owner = owner
    assert cli.main(["signin", tool, "--seat", B]) == 1
    assert ctx.snapshot_get(tool, B) == before
    assert ctx.snapshot_get(tool, A) is None
    assert ctx.cred[tool].get_live() == before
    assert ctx.state_file.read_bytes() == state
    assert_clean(ctx, fakes)


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("failure,code", [(KeyboardInterrupt, 130), (OSError, 1)])
def test_signin_always_cleans_on_failure(ctx, fakes, capsys, tool, failure, code):
    fakes.fail = failure
    assert cli.main(["signin", tool]) == code
    assert ctx.load_state().accounts(tool) == {}
    assert_clean(ctx, fakes)
    assert "fake-private-diagnostic" not in str(capsys.readouterr())


@pytest.mark.parametrize("tool,args", [("codex", ["logout"]), ("claude", ["auth", "logout"])])
@pytest.mark.parametrize("revoke", [False, True])
def test_logout_unregistered_or_explicit_revoke_runs_stock(ctx, fakes, tool, args, revoke, capsys):
    if revoke:
        seed(ctx, tool, B)
    else:
        ctx.set_live(tool, blob(tool, B))
    launcher.exec_stock(ctx, tool, args + (["--revoke"] if revoke else []))
    assert len(fakes.revokes) == 1
    assert fakes.revokes[0][1][1:] == args
    assert ctx.cred[tool].get_live() == blob(tool, B)
    if revoke:
        assert "WARNING" in str(capsys.readouterr())


def test_safe_logout_claude_survives_convergence_and_automatic_switch(ctx, fakes):
    from acctsw import claudesync
    from acctsw.context import Context
    from acctsw.switch import switch
    before = seed(ctx, "claude", B)
    assert launcher.exec_stock(ctx, "claude", ["auth", "logout"]) == 0
    ctx.__dict__.pop("_claude_tracker", None)
    claudesync.converge(ctx)
    with ctx.locked():
        switch(ctx, ctx.load_state(), "claude", B, sync=False)
    assert ctx.cred["claude"].get_live() is None
    # A fresh Context/process must honor the persisted decision too.
    fresh = Context.for_test(ctx.data_dir.parent)
    fresh.keychain = ctx.keychain
    fresh.cred = ctx.cred
    claudesync.converge(fresh)
    assert fresh.cred["claude"].get_live() is None
    with ctx.locked():
        switch(ctx, ctx.load_state(), "claude", B, sync=False, manual=True)
    assert ctx.cred["claude"].get_live() == before
    assert not ctx.load_state().data["tools"]["claude"].get("stock_logged_out")


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_signin_repairs_existing_seat_and_selects_canonical_email(ctx, fakes, tool):
    before = seed(ctx, tool, B)
    seed(ctx, tool, A)
    state = ctx.load_state()
    state.get_seat(tool, A).update(name="Work", auth_error=True,
                                  usage={"error": "unauthorized", "error_streak": 7,
                                         "last_attempted_at": "old", "fetched_at": "old", "stale": True})
    state.data["tools"][tool]["stock_logged_out"] = True
    state.save()
    fakes.owner = A.upper()
    if tool == "claude":
        # The fake profile is case-preserving like the real endpoint.
        fakes.owner = A
    assert cli.main(["signin", tool, "--seat", A.upper()]) == 0
    state = ctx.load_state()
    assert set(state.accounts(tool)) == {A, B}
    seat = state.get_seat(tool, A)
    assert seat["name"] == "Work" and "auth_error" not in seat
    assert seat["usage"] == {"error": None, "error_streak": 0, "last_attempted_at": None,
                              "fetched_at": None, "stale": False}
    assert state.active(tool) == A
    assert not state.data["tools"][tool].get("stock_logged_out")
    assert ctx.cred[tool].get_live() == ctx.snapshot_get(tool, A)
    assert ctx.snapshot_get(tool, B) == before
    assert_clean(ctx, fakes)


@pytest.mark.parametrize("evidence", ["home", "snapshot", "refresh", "profile", "unknown"])
def test_claude_logout_uses_cheapest_reliable_owner_proof(ctx, fakes, monkeypatch, evidence):
    seed(ctx, "claude", B)
    stored = blob("claude", B)
    live = json.loads(stored)
    live["claudeAiOauth"]["accessToken"] = "fake-rotated-access"
    live["claudeAiOauth"]["refreshToken"] = "fake-new-family"
    if evidence in ("home", "snapshot"):
        value = stored
        # Distinguish the paths so the proof cannot accidentally come from the other copy.
        if evidence == "home":
            ctx.snapshot_set("claude", B, "{}")
        else:
            ctx.claude_cred(B).set_live("{}")
    else:
        if evidence == "refresh":
            live["claudeAiOauth"]["refreshToken"] = json.loads(stored)["claudeAiOauth"]["refreshToken"]
        value = json.dumps(live)
    ctx.set_live("claude", value)
    profiles = []
    def profile(value, *a, **k):
        assert not ctx._store_lock_held()
        profiles.append(value)
        return B.upper() if evidence == "profile" else None
    monkeypatch.setattr(identity, "claude_profile_email", profile)
    launcher.exec_stock(ctx, "claude", ["auth", "logout"])
    assert len(profiles) == (1 if evidence in ("profile", "unknown") else 0)
    assert bool(fakes.revokes) == (evidence == "unknown")
    assert ctx.cred["claude"].get_live() == (value if evidence == "unknown" else None)


@pytest.mark.parametrize("change", ["stock", "registration"])
def test_logout_rechecks_identity_after_profile(ctx, fakes, monkeypatch, change):
    from acctsw.errors import AcctswError
    seed(ctx, "claude", B)
    ctx.set_live("claude", "{}")
    def profile(*a):
        if change == "stock":
            ctx.set_live("claude", blob("claude", A))
        else:
            state = ctx.load_state()
            state.remove_seat("claude", B)
            state.save()
        return B
    monkeypatch.setattr(identity, "claude_profile_email", profile)
    if change == "stock":
        with pytest.raises(AcctswError, match="changed during logout"):
            launcher.exec_stock(ctx, "claude", ["auth", "logout"])
        assert ctx.cred["claude"].get_live() == blob("claude", A)
        assert fakes.revokes == []
    else:
        launcher.exec_stock(ctx, "claude", ["auth", "logout"])
        assert len(fakes.revokes) == 1


@pytest.mark.parametrize("tool,args", [("codex", ["login"]), ("claude", ["auth", "login"])])
@pytest.mark.parametrize("app_open", [True, False])
def test_wrapper_signin_routes_with_app_open_or_closed(ctx, fakes, monkeypatch, tool, args, app_open):
    from acctsw import appalive
    monkeypatch.setattr(appalive, "app_running", lambda *a: app_open)
    assert cli.main(["run", tool, *args]) == 0
    assert len(fakes.logins) == 1
    assert ctx.snapshot_get(tool, A) == blob(tool, A)
    assert_clean(ctx, fakes)


@pytest.mark.parametrize("args", [["setup-token"], ["auth", "status"], ["auth", "refresh"]])
def test_other_claude_passthroughs_are_unchanged(ctx, fakes, args):
    launcher.run(ctx, "claude", args)
    assert fakes.logins == []
    assert len(fakes.revokes) == 1
    assert fakes.revokes[0][1] == [ctx.claude_bin, *args]


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("result", ["nonzero", "missing", "filing-error"])
def test_signin_nonzero_missing_and_filing_failure_cleanup(ctx, fakes, monkeypatch, capsys, tool, result):
    import subprocess
    real_fake = subprocess.run
    def run(argv, **kwargs):
        answer = real_fake(argv, **kwargs)
        if result == "nonzero":
            answer.returncode = 17
        if result == "missing":
            if tool == "codex":
                (fakes.homes[-1] / "auth.json").unlink()
            else:
                ctx.keychain.delete(fakes.services[-1], ctx.cred["claude"].account)
        return answer
    monkeypatch.setattr(subprocess, "run", run)
    if result == "filing-error":
        monkeypatch.setattr(accounts, "add", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("fake-private-diagnostic")))
    assert cli.main(["signin", tool]) == (17 if result == "nonzero" else 1)
    assert ctx.load_state().accounts(tool) == {}
    assert_clean(ctx, fakes)
    assert "fake-private-diagnostic" not in str(capsys.readouterr())


def test_signin_claude_reads_raw_item_and_profile_outside_lock(ctx, fakes, monkeypatch):
    from acctsw import authflows
    captured = []
    def profile(value, *a):
        assert not ctx._store_lock_held()
        captured.append(value)
        return A
    monkeypatch.setattr(identity, "claude_profile_email", profile)
    get = ctx.keychain.get
    def snapshot_only(service, account):
        assert service == ctx.keychain_service
        return get(service, account)
    monkeypatch.setattr(ctx.keychain, "get", snapshot_only)
    assert authflows.signin(ctx, "claude", args=["--email", A]) == 0
    assert captured == [blob("claude", A)]
    assert fakes.logins[0][0] == [ctx.claude_bin, "auth", "login", "--email", A]
    assert_clean(ctx, fakes)


def test_native_signin_passes_requested_seat_to_terminal(ctx, monkeypatch):
    from app import terminal
    from tests.test_claude_sync_timer import callback
    launched = []
    monkeypatch.setattr(terminal, "prepare_then_login", lambda *a, **k: launched.append((a, k)))
    native = callback("loginBg_", {"objc": SimpleNamespace(selector=lambda v, **k: v)})
    receiver = SimpleNamespace(ctx=ctx, loginNudge_=object(),
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda *a: None)
    native(receiver, {"tool": "claude", "email": B})
    assert launched == [((ctx, "claude"), {"seat": B})]


def test_patch_version():
    from acctsw import __version__
    assert __version__ == "1.3.1"


@pytest.mark.parametrize("stored", ["{}", "[]", "null", "broken", '{"claudeAiOauth":{"refreshToken":3}}'])
def test_unknown_claude_credentials_do_not_prove_sharing(ctx, fakes, stored):
    seed(ctx, "claude", B)
    ctx.set_live("claude", stored)
    launcher.exec_stock(ctx, "claude", ["auth", "logout"])
    assert len(fakes.revokes) == 1


def test_keychain_delete_failure_does_not_claim_logout_succeeded(ctx, fakes, monkeypatch, capsys):
    from acctsw.errors import AcctswError
    seed(ctx, "claude", B)
    monkeypatch.setattr(ctx.keychain, "delete", lambda *a: False)
    with pytest.raises(AcctswError, match="could not remove"):
        launcher.exec_stock(ctx, "claude", ["auth", "logout"])
    assert ctx.cred["claude"].get_live() is not None
    assert "keeps its login" not in str(capsys.readouterr())
    assert fakes.revokes == []


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_signin_env_is_child_only_and_strips_auth_overrides(ctx, fakes, monkeypatch, tool):
    import os
    overrides = ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
    for key in overrides:
        monkeypatch.setenv(key, "fake-inherited-secret")
    monkeypatch.setenv("PYTHONHOME", "/fake/frozen-python")
    monkeypatch.setenv("CODEX_HOME", "/fake/parent-codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/fake/parent-claude")
    assert cli.main(["signin", tool]) == 0
    env = fakes.logins[0][1]
    assert not set(overrides).intersection(env)
    assert "PYTHONHOME" not in env
    assert env.get("USER") == ctx.cred["claude"].account if tool == "claude" else True
    assert os.environ["CODEX_HOME"] == "/fake/parent-codex"
    assert os.environ["CLAUDE_CONFIG_DIR"] == "/fake/parent-claude"
    assert os.environ["OPENAI_API_KEY"] == "fake-inherited-secret"
    assert_clean(ctx, fakes)


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_fresh_signin_replaces_old_family_without_syncing_it_back(ctx, fakes, tool):
    seed(ctx, tool, A)
    fakes.login_blob = blob(tool, A).replace('"refresh_token": "r"', '"refresh_token": "fake-new-family"') if tool == "codex" else blob(tool, A).replace('fake-refresh-alpha', 'fake-new-family')
    assert cli.main(["signin", tool, "--seat", A]) == 0
    assert ctx.snapshot_get(tool, A) == fakes.login_blob
    assert ctx.cred[tool].get_live() == fakes.login_blob
    if tool == "claude":
        assert ctx.claude_cred(A).get_live() == fakes.login_blob
    assert_clean(ctx, fakes)


def test_signin_preserves_outgoing_stock_rotation(ctx, fakes):
    seed(ctx, "codex", B)
    rotated = blob("codex", B).replace('"refresh_token": "r"', '"refresh_token": "fake-rotated"')
    ctx.set_live("codex", rotated)
    assert cli.main(["signin", "codex"]) == 0
    assert ctx.snapshot_get("codex", B) == rotated
    assert ctx.snapshot_get("codex", A) == blob("codex", A)


def test_claude_logout_suppresses_convergence_until_manual_switch(ctx, fakes, monkeypatch):
    from acctsw import claudesync
    seed(ctx, "claude", B)
    before = blob("claude", B)
    newer = json.loads(before)
    newer["claudeAiOauth"].update(accessToken="fake-newer-access", expiresAt=500)
    newer = json.dumps(newer)
    monkeypatch.setattr(identity, "claude_profile_email", lambda *a, **k: B)
    claudesync.converge(ctx)
    assert launcher.exec_stock(ctx, "claude", ["auth", "logout"]) == 0
    claudesync.converge(ctx)
    # A stale external writer must not re-enable automatic stock mirroring.
    ctx.set_live("claude", before)
    ctx.claude_cred(B).set_live(newer)
    claudesync.converge(ctx)
    assert ctx.cred["claude"].get_live() == before
    assert ctx.claude_cred(B).get_live() == newer
    assert ctx.snapshot_get("claude", B) == newer


@pytest.mark.parametrize("tool,args", [
    ("codex", ["login", "status"]), ("codex", ["login", "status", "--future-option"]),
    ("codex", ["login", "--help"]),
    ("codex", ["login", "-h"]), ("codex", ["logout", "--help"]),
    ("codex", ["logout", "-h"]), ("claude", ["auth", "logout", "--help"]),
    ("claude", ["auth", "login", "--help"]),
])
def test_auth_status_and_help_remain_stock(ctx, fakes, tool, args):
    seed(ctx, tool, B)
    launcher.exec_stock(ctx, tool, args)
    assert fakes.logins == []
    assert len(fakes.revokes) == 1
    assert fakes.revokes[0][1][1:] == args
    assert ctx.cred[tool].get_live() == blob(tool, B)


@pytest.mark.parametrize("tool,args", [("codex", ["logout"]), ("claude", ["auth", "logout"])])
def test_missing_stock_login_uses_stock_logout_without_profile(ctx, fakes, monkeypatch, tool, args):
    monkeypatch.setattr(identity, "claude_profile_email", lambda *a, **k: pytest.fail("no credentials to identify"))
    launcher.exec_stock(ctx, tool, args)
    assert len(fakes.revokes) == 1
    assert not ctx.load_state().data["tools"][tool].get("stock_logged_out")


def test_signin_direct_call_creates_private_data_dir(ctx, fakes):
    from acctsw import authflows
    ctx.backup_dir.rmdir()
    ctx.data_dir.rmdir()
    assert authflows.signin(ctx, "codex") == 0
    assert ctx.data_dir.stat().st_mode & 0o777 == 0o700
    assert_clean(ctx, fakes)


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_signin_falls_back_to_official_cli_on_path(ctx, fakes, tool):
    ctx.codex_bin = ctx.claude_bin = None
    assert cli.main(["signin", tool]) == 0
    assert fakes.logins[0][0][0] == tool
    assert_clean(ctx, fakes)


def test_codex_signin_does_not_probe_claude(ctx, fakes, monkeypatch):
    ctx.set_live("claude", blob("claude", B))
    monkeypatch.setattr(identity, "claude_profile_email", lambda *a, **k: pytest.fail("unrelated tool"))
    assert cli.main(["signin", "codex"]) == 0


def test_claude_signin_preserves_rotation_with_profile_outside_lock(ctx, fakes, monkeypatch):
    seed(ctx, "claude", B)
    rotated = blob("claude", B).replace("fake-refresh-beta", "fake-rotated-beta")
    ctx.set_live("claude", rotated)
    ctx.claude_cred(B).set_live(rotated)
    seen = []
    def profile(value, *a, **k):
        assert not ctx._store_lock_held()
        seen.append(value)
        return B if value == rotated else A
    monkeypatch.setattr(identity, "claude_profile_email", profile)
    assert cli.main(["signin", "claude"]) == 0
    assert seen == [blob("claude", A), rotated]
    assert ctx.snapshot_get("claude", B) == rotated
    assert ctx.snapshot_get("claude", A) == blob("claude", A)


def test_concurrent_local_codex_removal_is_idempotent(ctx, fakes, monkeypatch):
    seed(ctx, "codex", B)
    stock = ctx.cred["codex"].auth_path
    unlink = Path.unlink
    def competing_removal(path, *a, **k):
        if path == stock and path.exists():
            unlink(path)
        return unlink(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", competing_removal)
    assert launcher.exec_stock(ctx, "codex", ["logout"]) == 0
    assert ctx.snapshot_get("codex", B) == blob("codex", B)
    assert fakes.revokes == []


@pytest.mark.parametrize("failure", ["throw", "refuse"])
def test_temp_keychain_cleanup_failure_still_removes_home(ctx, fakes, monkeypatch, capsys, failure):
    delete = ctx.keychain.delete
    def failed_delete(service, account):
        if service in fakes.services:
            if failure == "throw":
                raise RuntimeError("fake-private-diagnostic")
            return False
        return delete(service, account)
    monkeypatch.setattr(ctx.keychain, "delete", failed_delete)
    assert cli.main(["signin", "claude"]) == 1
    assert all(not p.exists() for p in fakes.homes)
    assert "fake-private-diagnostic" not in str(capsys.readouterr())
    for service in fakes.services:
        delete(service, ctx.cred["claude"].account)


def test_bridge_and_terminal_preserve_requested_seat(ctx, monkeypatch):
    import shlex
    from acctsw import bridge
    from app import terminal
    assert shlex.split(bridge.login_command("claude", seat=B)) == ["acctsw", "signin", "claude", "--seat", B]
    opened = []
    monkeypatch.setattr(terminal, "checked_engine_command", lambda *a: ["/fake/app engine"])
    monkeypatch.setattr(terminal, "open_in_terminal", opened.append)
    terminal.prepare_then_login(ctx, "claude", seat=B)
    assert shlex.split(opened[0]) == ["/fake/app engine", "signin", "claude", "--seat", B]


def test_codex_child_exit_does_not_restore_deliberate_stock_logout(ctx, fakes):
    before = seed(ctx, "codex", B)
    def spawn(argv, on_output, on_tick=None):
        assert launcher.exec_stock(ctx, "codex", ["logout"]) == 0
        return 0
    assert launcher.run(ctx, "codex", [], spawn=spawn, get=lambda *a: (503, "{}")) == 0
    assert ctx.cred["codex"].get_live() is None
    assert ctx.snapshot_get("codex", B) == before
    assert fakes.revokes == []


def test_signin_engine_failure_mentions_signin_and_hides_diagnostics(ctx, monkeypatch):
    from app import terminal
    def probe(*a, **k):
        return SimpleNamespace(returncode=1, stdout="fake-private-diagnostic")
    monkeypatch.setattr(terminal.subprocess, "run", probe)
    with pytest.raises(RuntimeError) as exc:
        terminal.resolve_login_command(ctx, "claude")
    assert str(exc.value).startswith("this app's engine couldn't start sign-in. ")
    assert "fake-private-diagnostic" not in str(exc.value)


def test_normal_codex_child_exit_still_mirrors_rotated_home(ctx, fakes):
    seed(ctx, "codex", B)
    rotated = blob("codex", B).replace('"refresh_token": "r"', '"refresh_token": "fake-rotated"')
    def spawn(argv, on_output, on_tick=None):
        ctx.snapshot_set("codex", B, rotated)
        return 0
    assert launcher.run(ctx, "codex", [], spawn=spawn, get=lambda *a: (503, "{}")) == 0
    assert ctx.cred["codex"].get_live() == rotated
