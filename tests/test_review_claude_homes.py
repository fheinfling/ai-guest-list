"""Review regressions, using only temporary homes and the in-memory Keychain."""
import json
import os
from datetime import timedelta
from contextlib import contextmanager

import pytest

from acctsw import bridge, claudehome, codexhome, identity, launcher as L
from acctsw.state import State
from acctsw.switch import switch
from acctsw.util import iso, now
from tests.test_claude_homes_integration import setup
from tests.test_claudehome import seed
from tests.test_usage import claude_ok_body


@pytest.fixture(autouse=True)
def _never_exec_a_real_cli(monkeypatch):
    def unexpected(*a, **kw):
        pytest.fail("unmocked CLI exec")
    monkeypatch.setattr(L.os, "execvpe", unexpected)


@pytest.mark.parametrize("args", [
    ["--version"], ["-v"], ["--help"], ["-h"], ["--model", "opus", "--help"],
    ["agents", "--json"], ["--verbose", "agents", "--json"],
    ["--model", "opus", "agents", "--json"],
    ["--model=opus", "agents", "--json"], ["--tools", "Read", "--help"],
    ["--tools", "Read", "Edit", "--help"],
    *[[option, "--help"] for option in (
        "-d", "--debug", "--cloud", "--from-pr", "--prompt-suggestions", "--remote-control",
        "-r", "--resume", "--teleport", "--tmux", "-w", "--worktree",
    )],
    *[[name] for name in (
        "attach", "auto-mode", "doctor", "gateway", "import", "install", "logs", "mcp",
        "plugin", "plugins", "purge", "respawn", "rm", "stop", "kill", "ultrareview",
        "update", "upgrade", "config", "migrate-installer",
    )],
])
def test_non_session_execs_active_home_without_supervision(ctx, monkeypatch, args):
    setup(ctx)
    state = ctx.load_state()
    state.set_limited_until("claude", "a@test", iso(now() + timedelta(hours=1)))
    state.save()
    before = ctx.state_file.read_bytes()
    stock = ctx.cred["claude"].get_live()
    calls = []
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/must-not-leak")
    monkeypatch.setattr(L.os, "execvpe", lambda exe, argv, env: calls.append((exe, argv, env)))
    def unexpected(*a, **kw):
        pytest.fail("non-session invocation entered supervision")
    for name in ("choose", "active_session", "mark_session", "_verify_capacity"):
        monkeypatch.setattr(L, name, unexpected)
    assert L.run(ctx, "claude", args, spawn=unexpected, get=unexpected) == 127
    assert len(calls) == 1
    exe, argv, env = calls[0]
    assert exe == "claude" and argv == ["claude", *args]
    assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("a@test"))
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
    assert env["USER"] == "tester"
    assert ctx.state_file.read_bytes() == before
    assert ctx.cred["claude"].get_live() == stock


@pytest.mark.parametrize("args", [["--version"], ["agents", "--json"]])
def test_non_session_without_seats_runs_stock(ctx, monkeypatch, args):
    seen = []
    monkeypatch.setattr(L, "exec_stock", lambda c, t, a: seen.append((t, a)) or 23)
    assert L.run(ctx, "claude", args) == 23
    assert seen == [("claude", args)]


def test_non_session_prepares_an_unmigrated_active_home(ctx, monkeypatch):
    seed(ctx)
    home = ctx.claude_home("a@test")
    assert not home.exists()
    calls = []
    def execute(exe, argv, env):
        calls.append(argv)
        assert env["CLAUDE_CONFIG_DIR"] == str(home)
        assert (home / "projects").is_symlink()
        assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
            "emailAddress": "a@test"}
        assert ctx.claude_cred("a@test").get_live() == ctx.snapshot_get("claude", "a@test")
    monkeypatch.setattr(L.os, "execvpe", execute)
    def unexpected(*a, **kw):
        pytest.fail("non-session invocation entered supervision")
    assert L.run(ctx, "claude", ["agents", "--json"], spawn=unexpected,
                 get=unexpected) == 127
    assert calls == [["claude", "agents", "--json"]]


def test_non_session_rejects_a_pinned_key_before_resolving_it(ctx):
    from acctsw.errors import AcctswError
    with pytest.raises(AcctswError, match="work session"):
        L.run(ctx, "claude", ["agents", "--json"], key="not-a-real-key")


def test_non_session_missing_executable_returns_127(ctx, monkeypatch):
    setup(ctx)
    def missing(*a, **kw):
        raise FileNotFoundError("fake missing CLI")
    monkeypatch.setattr(L.os, "execvpe", missing)
    def unexpected(*a, **kw):
        pytest.fail("non-session invocation entered supervision")
    assert L.run(ctx, "claude", ["--version"], spawn=unexpected, get=unexpected) == 127


@pytest.mark.parametrize("args", [
    [], ["fix the bug"], ["agents need fixing"], ["explain", "mcp"],
    ["-p", "fix the bug"], ["--print", "agents need fixing"],
    ["--model", "opus", "fix the bug"], ["--model", "mcp", "fix the bug"],
    ["--system-prompt", "--help", "fix the bug"],
    ["--model=opus", "fix the bug"],
    ["--system-prompt", "mcp"], ["--add-dir", ".", "mcp"],
    ["--tools", "Read", "mcp", "--model", "opus", "fix the bug"],
    ["--tools", "Read", "Edit", "--model", "opus", "fix the bug"],
    ["--", "mcp"], ["--", "--help"], ["--", "--resume", "literal"],
    ["--", "--continue"], ["--", "--session-id", "literal"], ["--", "--fork-session"],
])
def test_actual_sessions_still_get_an_id(ctx, args):
    setup(ctx)
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        assert argv[1] == "--session-id"
        from uuid import UUID
        UUID(argv[2])
        assert argv[3:] == args
        return 0
    assert L.run(ctx, "claude", args, spawn=spawn, get=lambda *a: (0, "")) == 0
    assert len(calls) == 1


def test_fork_flag_without_resume_preserves_fresh_session_and_can_hop(ctx):
    setup(ctx)
    args = ["--session-id", "fresh", "--fork-session"]
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            assert argv == ["claude", *args]
            assert output(b"5-hour limit reached\n")
            return 1
        assert argv == ["claude", "--resume", "fresh"]
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", args, spawn=spawn, get=get) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("resume", [
    ["--resume", "parent"], ["-r", "parent"], ["--resume=parent"],
    ["--continue"], ["-c"], ["--resume"],
])
@pytest.mark.parametrize("transcripts", [["fork"], ["fork", "other"], ["parent"]])
def test_fork_resume_never_hops_to_parent(ctx, resume, transcripts):
    setup(ctx)
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    calls, messages = [], []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            assert argv == ["claude", *resume, "--fork-session"]
            for name in transcripts:
                (root / f"{name}.jsonl").write_text(json.dumps(
                    {"sessionId": name, "cwd": os.getcwd()}) + "\n")
            assert not output(b"5-hour limit reached\n")
            return 0
        pytest.fail("forked resume must not hop")
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", [*resume, "--fork-session"], spawn=spawn, get=get,
                 notify=messages.append) == 0
    assert len(calls) == 1
    assert any("session" in m and "staying" in m for m in messages)


def test_live_fork_does_not_offer_a_paid_handoff(ctx, monkeypatch):
    from acctsw import handoff
    from tests.test_launcher_keyseats import setup as setup_key, usage_get
    _, reset = setup_key(ctx, tool="claude")
    def unexpected(*a, **kw):
        pytest.fail("fork must not offer a paid resume")
    monkeypatch.setattr(handoff, "request", unexpected)
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    def spawn(argv, output, on_tick=None, *, env=None):
        (root / "parent.jsonl").write_text(json.dumps(
            {"sessionId": "parent", "cwd": os.getcwd()}) + "\n")
        assert not output(b"5-hour limit reached\n")
        return 0
    assert L.run(ctx, "claude", ["--resume", "parent", "--fork-session"], spawn=spawn,
                 get=usage_get(reset, tool="claude"), price_get=unexpected) == 0
    assert not handoff.pending(ctx)


def test_manual_switch_probes_the_active_private_home_before_lock(ctx, monkeypatch):
    setup(ctx)
    ctx.claude_cred("a@test").set_live("fresh private bytes")
    ctx.cred["claude"].set_live("unrelated stock login")
    calls = []
    held = False
    locked = ctx.locked
    @contextmanager
    def track_lock():
        nonlocal held
        with locked():
            held = True
            try:
                yield
            finally:
                held = False
    monkeypatch.setattr(ctx, "locked", track_lock)
    def probe(c, *, email=None):
        assert not held
        calls.append(email)
        assert email in (None, "a@test", "b@test")
        return identity.ClaudeLiveIdentity((c.claude_cred(email) if email else c.cred["claude"]).get_live(), email)
    monkeypatch.setattr(identity, "claude_live_identity", probe)
    result = bridge.handle(ctx, {"action": "switch", "tool": "claude", "email": "b@test"})
    assert result["ok"], result
    assert set(calls) == {None, "a@test", "b@test"}
    assert ctx.claude_cred("a@test").get_live() == "fresh private bytes"
    assert ctx.load_state().active("claude") == "b@test"


def test_hop_persists_active_floor_and_timestamp_in_one_save(ctx, monkeypatch):
    setup(ctx)
    saves, calls = [], []
    save = State.save
    tracking = False
    def record(state):
        if tracking:
            saves.append(json.loads(json.dumps(state.data)))
        return save(state)
    monkeypatch.setattr(State, "save", record)
    def spawn(argv, output, on_tick=None, *, env=None):
        nonlocal tracking
        calls.append(argv)
        if len(calls) == 1:
            assert output(b"5-hour limit reached\n")
            tracking = True
            return 1
        assert len(saves) == 1
        persisted = ctx.load_state()
        assert persisted.active("claude") == "b@test"
        assert persisted.get_seat("claude", "b@test")["last_on_floor"]
        assert persisted.data["last_switch_at"]
        tracking = False
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", [], spawn=spawn, get=get) == 0
    assert len(calls) == 2


def test_plain_switch_still_saves_by_default(ctx, monkeypatch):
    setup(ctx)
    revision = ctx.load_state().data["rev"]
    switch(ctx, ctx.load_state(), "claude", "b@test", sync=False, mirror=False)
    state = ctx.load_state()
    assert state.active("claude") == "b@test"
    assert state.data["rev"] == revision + 1


@pytest.mark.parametrize("name", [".credentials.late", ".claude.json.late", "daemon"])
def test_private_entry_created_at_heal_time_is_never_promoted(ctx, monkeypatch, name):
    seed(ctx)
    home = claudehome.ensure_home(ctx, "a@test")
    heal = codexhome._heal
    def create_then_heal(h, real, **kw):
        (h / name).write_text("private bytes")
        heal(h, real, **kw)
    monkeypatch.setattr(codexhome, "_heal", create_then_heal)
    claudehome.ensure_home(ctx, "a@test", promote=True)
    assert not (ctx._claude_real / name).exists()
    assert not (home / name).is_symlink()
    assert (home / name).read_text() == "private bytes"


def test_unchanged_merge_does_not_replace_baseline(ctx, monkeypatch):
    seed(ctx)
    home = claudehome.ensure_home(ctx, "a@test")
    writes = []
    write = claudehome.atomic_write_text
    def record(path, text, **kw):
        writes.append(path)
        return write(path, text, **kw)
    monkeypatch.setattr(claudehome, "atomic_write_text", record)
    claudehome.merge_config(ctx, "a@test")
    assert writes == []
    canonical = ctx._claude_real / ".claude.json"
    canonical.write_text(json.dumps({"theme": "dark"}))
    claudehome.merge_config(ctx, "a@test")
    baseline = home / ".acctsw-shared.json"
    assert baseline in writes
    assert json.loads(baseline.read_text()) == {"theme": "dark"}
