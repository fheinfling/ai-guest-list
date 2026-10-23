"""Launcher regressions: no shared credential handoff, even with several supervisors."""
import json
import os
from pathlib import Path
from datetime import timedelta
from acctsw.util import iso, now

import pytest

from acctsw import accounts, identity, launcher as L, usage
from acctsw.switch import switch
from tests.conftest import make_claude_blob
from tests.test_usage import claude_ok_body


def setup(ctx):
    for email, token in [("b@test", "b"), ("a@test", "a")]:
        ctx.cred["claude"].set_live(make_claude_blob().replace('"x"', json.dumps(token)))
        accounts.add(ctx, ctx.load_state(), "claude", email=email)


def test_launch_uses_private_config_and_never_writes_stock_item(ctx, monkeypatch):
    setup(ctx)
    stock = ctx.cred["claude"].get_live()
    writes = []
    monkeypatch.setattr(ctx.cred["claude"], "set_live", lambda blob: writes.append(blob))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/supervisor/custom")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/must-not-leak")
    def spawn(argv, output, on_tick=None, *, env=None):
        assert env is not None
        home = Path(env["CLAUDE_CONFIG_DIR"])
        assert home != ctx._claude_real
        assert home.is_dir()
        assert (home / "projects").resolve() == (ctx._claude_real / "projects").resolve()
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
        assert env["ACCTSW_CLAUDE_SHARED_DIR"] == str(ctx._claude_real)
        assert env["ACCTSW_CLAUDE_SHARED_EXPLICIT"] == str(int(ctx.claude_config_explicit))
        assert env["ACCTSW_CLAUDE_STOCK_CONFIG"] == (ctx._claude_config_value or "")
        assert env["USER"] == "tester"
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn) == 0
    assert not writes
    assert ctx.cred["claude"].get_live() == stock
    assert os.environ["CLAUDE_CONFIG_DIR"] == "/supervisor/custom"


def test_two_concurrent_supervisors_and_hop_preserve_each_others_items_and_resume(ctx, monkeypatch):
    setup(ctx)
    from acctsw import claudehome
    original_a = ctx.claude_cred("a@test").get_live()
    original_b = ctx.claude_cred("b@test").get_live()
    stock = ctx.cred["claude"].get_live()
    launches = []
    messages = []
    monkeypatch.setattr(identity, "claude_live_identity", lambda c, *, email=None:
                        identity.ClaudeLiveIdentity((c.claude_cred(email) if email else c.cred["claude"]).get_live(), email))
    transcript = ctx._claude_real / "projects" / "project" / "ours.jsonl"
    def spawn(argv, output, on_tick=None, *, env=None):
        launches.append((argv, env["CLAUDE_CONFIG_DIR"]))
        if len(launches) == 1:
            # A remains alive while a second supervisor cold-starts B.
            with ctx.locked():
                state = ctx.load_state()
                switch(ctx, state, "claude", "b@test", sync=False, mirror=False)
            def sibling(argv, output, on_tick=None, *, env=None):
                assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("b@test"))
                assert ctx.claude_cred("a@test").get_live() == original_a
                return 0
            assert L.run(ctx, "claude", [], spawn=sibling) == 0
            assert ctx.claude_cred("a@test").get_live() == original_a
            ctx.claude_cred("a@test").set_live(original_a.replace('"a"', '"a-rotated"'))
            assert ctx.claude_cred("b@test").get_live() == original_b
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({"sessionId": "ours", "cwd": os.getcwd()}) + "\n")
            assert output(b"5-hour limit reached\n"), (messages, ctx.load_state().accounts("claude"))
            return 1
        assert argv[-2:] == ["--resume", "ours"]
        assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("b@test"))
        return 0
    def get(url, headers, timeout):
        return 200, claude_ok_body(five=100 if headers["Authorization"] == "Bearer a-rotated" else 10,
                                   five_reset=iso(now() + timedelta(hours=1)))
    assert L.run(ctx, "claude", ["--model", "opus", "--session-id", "ours"], spawn=spawn, get=get, notify=messages.append) == 0
    assert len(launches) == 2
    assert launches[0][1] == str(ctx.claude_home("a@test"))
    assert launches[1][1] == str(ctx.claude_home("b@test"))
    assert ctx.snapshot_get("claude", "a@test") == original_a.replace('"a"', '"a-rotated"')
    assert ctx.claude_cred("b@test").get_live() == original_b
    assert ctx.cred["claude"].get_live() == stock


def test_transcripts_use_shared_projects_even_with_inherited_private_env(ctx, monkeypatch):
    shared = ctx._claude_real / "projects" / "project" / "id.jsonl"
    shared.parent.mkdir(parents=True)
    shared.write_text('{}\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ctx.data_dir / "other-private-home"))
    assert shared in L._claude_transcripts(ctx)


def test_manual_stock_switch_never_changes_running_seat_item(ctx):
    setup(ctx)
    from acctsw import claudehome
    before = ctx.claude_cred("a@test").get_live()
    rotated = before.replace('"a"', '"new-a"')
    ctx.claude_cred("a@test").set_live(rotated)
    state = ctx.load_state()
    switch(ctx, state, "claude", "b@test", sync=False, manual=True)
    assert ctx.claude_cred("a@test").get_live() == rotated
    assert ctx.cred["claude"].get_live() == ctx.claude_cred("b@test").get_live()
    answer = identity.ClaudeLiveIdentity(ctx.cred["claude"].get_live(), "b@test")
    assert accounts.reconcile_claude(ctx, state, live_identity=answer) is None
    assert ctx.claude_cred("a@test").get_live() == rotated


def test_foreign_login_inside_child_is_retained_but_never_misattributed(ctx, monkeypatch):
    setup(ctx)
    before = ctx.snapshot_get("claude", "a@test")
    messages = []
    def spawn(argv, output, on_tick=None, *, env=None):
        home = Path(env["CLAUDE_CONFIG_DIR"])
        config = json.loads((home / ".claude.json").read_text())
        config["oauthAccount"] = {"emailAddress": "y@test"}
        (home / ".claude.json").write_text(json.dumps(config))
        ctx.claude_cred("a@test").set_live("fresh foreign login")
        return 0
    monkeypatch.setattr(identity, "claude_live_identity", lambda c, *, email=None:
                        identity.ClaudeLiveIdentity((c.claude_cred(email) if email else c.cred["claude"]).get_live(),
                            "y@test" if email and c.claude_cred(email).get_live() == "fresh foreign login" else email))
    assert L.run(ctx, "claude", [], spawn=spawn, notify=messages.append) == 0
    assert ctx.snapshot_get("claude", "a@test") == before
    assert ctx.claude_cred("a@test").get_live() == "fresh foreign login"
    assert ctx.snapshot_get("claude", "y@test") is None
    assert sum("could not be identified as a@test" in m for m in messages) == 1


def test_cold_start_and_hop_never_install_b_into_stock_item(ctx, monkeypatch):
    """Behavioral regression using only the old public API, runnable against pristine main."""
    setup(ctx)
    stock = ctx.cred["claude"].get_live()
    writes = []
    monkeypatch.setattr(ctx.cred["claude"], "set_live", lambda blob: writes.append(blob))
    monkeypatch.setattr(identity, "claude_live_identity", lambda c, **kw:
                        identity.ClaudeLiveIdentity(stock, "a@test"))
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            assert output(b"5-hour limit reached\n")
            return 1
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", [], spawn=spawn, get=get) == 0
    assert len(calls) == 2
    assert not writes
    assert ctx.cred["claude"].get_live() == stock


def test_launcher_migrates_active_snapshot_before_first_spawn(ctx):
    from tests.test_claudehome import seed
    seed(ctx)
    def spawn(argv, output, on_tick=None, *, env=None):
        home = Path(env["CLAUDE_CONFIG_DIR"])
        assert home.is_dir()
        assert (home / "projects").is_symlink()
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn, get=lambda *a: (0, "")) == 0


@pytest.mark.parametrize("args", [
    ["--continue"], ["-c"], ["--resume", "our-id"], ["-r", "our-id"],
    ["--session-id=our-id"], ["--resume=our-id"], ["--resume", "--model", "opus"],
])
def test_resume_forms_hop_to_the_exact_changed_transcript(ctx, args):
    setup(ctx)
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    (root / "old.jsonl").write_text(json.dumps({"sessionId": "old", "cwd": os.getcwd()}) + "\n")
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            (root / "ours.jsonl").write_text(json.dumps({"sessionId": "our-id", "cwd": os.getcwd()}) + "\n")
            (root / "other-cwd.jsonl").write_text(json.dumps({"sessionId": "other", "cwd": "/other"}) + "\n")
            assert output(b"5-hour limit reached\n")
            return 1
        assert argv == ["claude", "--resume", "our-id"]
        assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("b@test"))
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", args, spawn=spawn, get=get) == 0
    assert len(calls) == 2


def test_ambiguous_continue_never_stops_child_and_restores_other_tabs_selection(ctx):
    setup(ctx)
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    probes = []
    messages = []
    def spawn(argv, output, on_tick=None, *, env=None):
        for name in ("one", "two"):
            (root / f"{name}.jsonl").write_text(json.dumps({"sessionId": name, "cwd": os.getcwd()}) + "\n")
        state = ctx.load_state()
        state.set_active("claude", "b@test")
        state.save()
        assert not output(b"5-hour limit reached\n")
        assert ctx.load_state().active("claude") == "b@test"
        count = len(probes)
        assert not output(b"ordinary output\n")
        assert len(probes) == count
        return 0
    def get(*a):
        probes.append(True)
        return 200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1)))
    assert L.run(ctx, "claude", ["--continue"], spawn=spawn, get=get, notify=messages.append) == 0
    assert any("couldn't identify this session" in m for m in messages)


def test_postexit_hop_resolves_continue_session_and_saves_settings(ctx):
    setup(ctx)
    calls = []
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            (root / "ours.jsonl").write_text(json.dumps({"sessionId": "our-id", "cwd": os.getcwd()}) + "\n")
            config = Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
            value = json.loads(config.read_text())
            value["mcpServers"] = {"added": {"command": "run"}}
            config.write_text(json.dumps(value))
            return 3
        assert argv == ["claude", "--resume", "our-id"]
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", ["--continue"], spawn=spawn, get=get) == 0
    assert len(calls) == 2
    assert json.loads((ctx._claude_real / ".claude.json").read_text())["mcpServers"] == {"added": {"command": "run"}}


def test_exception_after_child_rotation_still_syncs_its_own_home(ctx, monkeypatch):
    setup(ctx)
    monkeypatch.setattr(identity, "claude_live_identity", lambda c, *, email=None:
                        identity.ClaudeLiveIdentity((c.claude_cred(email) if email else c.cred["claude"]).get_live(), email))
    def spawn(argv, output, on_tick=None, *, env=None):
        ctx.claude_cred("a@test").set_live("last rotation")
        state = ctx.load_state()
        state.set_active("claude", "b@test")
        state.save()
        raise RuntimeError("fake PTY failure")
    with pytest.raises(RuntimeError, match="fake PTY"):
        L.run(ctx, "claude", [], spawn=spawn, get=lambda *a: (0, ""))
    assert ctx.snapshot_get("claude", "a@test") == "last rotation"
    assert ctx.snapshot_get("claude", "b@test") != "last rotation"


@pytest.mark.parametrize("busy", [False, True])
def test_launcher_only_promotes_when_no_claude_session_is_alive(ctx, monkeypatch, busy):
    setup(ctx)
    home = ctx.claude_home("a@test")
    real = ctx._claude_real
    (real / "settings.json").write_text('{"stock": true}')
    (home / "settings.json").write_text('{"seat": true}')
    monkeypatch.setattr(L, "active_session", lambda data, tool, **kw:
                        {"email": "b@test"} if busy and tool == "claude" else None)
    def spawn(argv, output, on_tick=None, *, env=None):
        assert (home / "settings.json").is_symlink() is (not busy)
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn, get=lambda *a: (0, "")) == 0


def test_idle_app_handoff_uses_home_tokens_even_when_stock_and_snapshot_are_stale(ctx, monkeypatch):
    from acctsw import bridge
    from tests.test_auto_handoff import seed, probe
    summary = seed(ctx, "claude", spare_count=1)
    active, landing = "seat0@test.example", "seat1@test.example"
    rotated = ctx.claude_cred(active).get_live().replace('"x0"', '"rotated"')
    ctx.claude_cred(active).set_live(rotated)
    state = ctx.load_state()
    usage.store_fetch(state, "claude", active, usage.Usage(
        ok=True, fetched_at=iso(now()), windows={"5h": usage.Window(
            used_pct=100, resets_at=iso(now() + timedelta(hours=1)))}), blob=rotated)
    state.save()
    ctx.cred["claude"].set_live("unrelated stock login")
    monkeypatch.setattr(identity, "claude_live_identity", lambda c, *, email=None:
                        identity.ClaudeLiveIdentity((c.claude_cred(email) if email else c.cred["claude"]).get_live(), email))
    probe(monkeypatch)
    stock_get = ctx.cred["claude"].get_live
    monkeypatch.setattr(ctx.cred["claude"], "get_live", lambda:
                        pytest.fail("home-based idle handoff must not read stock credentials"))
    result = bridge._auto_switch_after_usage(ctx, summary)
    assert result["to"] == landing
    assert ctx.claude_cred(active).get_live() == rotated
    assert stock_get() == ctx.claude_cred(landing).get_live()


def test_transcript_discovery_ignores_private_supervisor_environment(tmp_path, monkeypatch):
    """Uses the pre-existing API so main fails on the actual transcript lookup behavior."""
    from acctsw import paths
    shared = tmp_path / "shared"
    transcript = shared / "projects" / "p" / "id.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{}\n')
    monkeypatch.setattr(paths, "CLAUDE_CONFIG_DIR", shared)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "private"))
    assert transcript in L._claude_transcripts()


def test_removing_a_running_seat_does_not_resurrect_its_home_on_exit(ctx):
    setup(ctx)
    home = ctx.claude_home("a@test")
    def spawn(argv, output, on_tick=None, *, env=None):
        accounts.remove(ctx, ctx.load_state(), "claude", "a@test")
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn, get=lambda *a: (0, "")) == 0
    assert not home.exists()
    assert ctx.claude_cred("a@test").get_live() is None
    assert ctx.load_state().get_seat("claude", "a@test") is None


@pytest.mark.parametrize("args", [["--session-id", "ours"], ["--resume", "ours"], ["-r", "ours"],
                                   ["--resume=ours"], ["--session-id=ours"]])
def test_a_known_session_id_cannot_be_replaced_by_another_tabs_transcript(ctx, args):
    setup(ctx)
    calls = []
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            (root / "other.jsonl").write_text(json.dumps({"sessionId": "other", "cwd": os.getcwd()}) + "\n")
            assert output(b"5-hour limit reached\n")
            return 1
        assert argv == ["claude", "--resume", "ours"]
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", args, spawn=spawn, get=get) == 0


def test_seat_selected_during_command_build_is_prepared_before_spawn(ctx, monkeypatch):
    from tests.test_claudehome import seed
    seed(ctx)
    build = L.build_cmd
    def build_and_select(ctx, tool, args):
        seed(ctx, "new@test", "new")  # a registered snapshot, with no home yet
        return build(ctx, tool, args)
    monkeypatch.setattr(L, "build_cmd", build_and_select)
    def spawn(argv, output, on_tick=None, *, env=None):
        assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("new@test"))
        assert (Path(env["CLAUDE_CONFIG_DIR"]) / "projects").is_symlink()
        assert ctx.claude_cred("new@test").get_live() == ctx.snapshot_get("claude", "new@test")
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn, get=lambda *a: (0, "")) == 0


@pytest.mark.parametrize("cold", [False, True])
def test_subscription_handoffs_only_write_stock_with_equal_owner_proof(ctx, monkeypatch, cold):
    setup(ctx)
    if cold:
        state = ctx.load_state()
        state.set_limited_until("claude", "a@test", iso(now() + timedelta(hours=1)))
        state.save()
    monkeypatch.setattr(ctx.cred["claude"], "set_live", lambda *a:
                        pytest.fail("supervised handoff wrote stock credentials"))
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if not cold and len(calls) == 1:
            assert output(b"5-hour limit reached\n")
            return 1
        assert env["CLAUDE_CONFIG_DIR"] == str(ctx.claude_home("b@test"))
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", [], spawn=spawn, get=get) == 0


def test_already_selected_launch_does_not_record_a_spurious_switch(ctx):
    setup(ctx)
    before = ctx.load_state().get_seat("claude", "a@test")["last_on_floor"]
    assert L.run(ctx, "claude", [], spawn=lambda *a, **kw: 0, get=lambda *a: (0, "")) == 0
    assert ctx.load_state().get_seat("claude", "a@test")["last_on_floor"] == before


def test_all_resting_without_an_active_pointer_does_not_prepare_a_none_seat(ctx, monkeypatch):
    setup(ctx)
    state = ctx.load_state()
    for email in ("a@test", "b@test"):
        state.set_limited_until("claude", email, iso(now() + timedelta(hours=1)))
    state.set_active("claude", None)
    state.save()
    monkeypatch.setattr(L, "_wait_for_unlock", lambda *a, **kw: None)
    assert L.run(ctx, "claude", [], spawn=lambda *a, **kw: pytest.fail("no unlocked seat"),
                 get=lambda *a: (0, "")) == L.EXIT_GAVE_UP


def test_ambiguous_continue_with_plain_failure_does_not_hop_to_last_global_session(ctx):
    setup(ctx)
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        assert len(calls) == 1
        root = ctx._claude_real / "projects" / "p"
        root.mkdir(parents=True)
        for name in ("one", "two"):
            (root / f"{name}.jsonl").write_text(json.dumps({"sessionId": name, "cwd": os.getcwd()}) + "\n")
        return 3
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", ["--continue"], spawn=spawn, get=get) == 3


def test_claude_home_change_preserves_codex_exit_mirroring(ctx):
    from tests.conftest import make_codex_blob
    original = make_codex_blob("codex@test")
    ctx.cred["codex"].set_live(original)
    accounts.add(ctx, ctx.load_state(), "codex", email="codex@test")
    rotated = original.replace('"refresh_token": "r"', '"refresh_token": "rotated"')
    def spawn(argv, output, on_tick=None):
        ctx.snapshot_set("codex", "codex@test", rotated)
        return 0
    assert L.run(ctx, "codex", [], spawn=spawn, get=lambda *a: (0, "")) == 0
    assert ctx.cred["codex"].get_live() == rotated


@pytest.mark.parametrize("args,expected", [(["-r"], "picked"), (["--resume=ours=title"], "ours=title")])
def test_resume_picker_and_equal_signs_preserve_the_selected_session(ctx, args, expected):
    setup(ctx)
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            assert "--session-id" not in argv
            root = ctx._claude_real / "projects" / "p"
            root.mkdir(parents=True)
            (root / "picked.jsonl").write_text(json.dumps({"sessionId": "picked", "cwd": os.getcwd()}) + "\n")
            assert output(b"5-hour limit reached\n")
            return 1
        assert argv == ["claude", "--resume", expected]
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", args, spawn=spawn, get=get) == 0


def test_claude_cold_paid_resume_never_scans_codex_transcripts(ctx, monkeypatch):
    from tests.test_launcher_keyseats import setup as key_setup, usage_get, price_get, THREAD, MODEL, SECRET
    from acctsw import handoff
    _, reset = key_setup(ctx, tool="claude", resting=True, confirm=False)
    monkeypatch.setattr(handoff.session, "_proc_start", lambda *_: "test process")
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    (root / f"{THREAD}.jsonl").write_text(json.dumps({"sessionId": THREAD, "cwd": os.getcwd()}) + "\n")
    monkeypatch.setattr(L.rollout, "scan_rollouts", lambda *a, **kw:
                        pytest.fail("Claude paid resume consulted Codex transcripts"))
    def spawn(argv, output, on_tick=None, *, env=None):
        assert argv[1:] == ["--resume", THREAD, "--model", MODEL]
        assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
        return 0
    assert L.run(ctx, "claude", ["--resume", THREAD], spawn=spawn,
                 get=usage_get(reset, tool="claude"), price_get=price_get) == 0


def test_continue_session_is_pinned_before_later_tabs_append_after_exit(ctx, monkeypatch):
    from acctsw import claudehome
    setup(ctx)
    root = ctx._claude_real / "projects" / "p"
    root.mkdir(parents=True)
    calls = []
    race = {"exited": False, "pending": False}
    original_merge, original_load = claudehome.merge_config, ctx.load_state
    def merge(*a, **kw):
        result = original_merge(*a, **kw)
        if race["exited"]:
            race["pending"] = True
            race["exited"] = False
        return result
    def load():
        if race["pending"]:
            (root / "later-tab.jsonl").write_text(json.dumps({"sessionId": "later", "cwd": os.getcwd()}) + "\n")
            race["pending"] = False
        return original_load()
    monkeypatch.setattr(claudehome, "merge_config", merge)
    monkeypatch.setattr(ctx, "load_state", load)
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if len(calls) == 1:
            (root / "ours.jsonl").write_text(json.dumps({"sessionId": "ours", "cwd": os.getcwd()}) + "\n")
            race["exited"] = True
            return 3
        assert argv == ["claude", "--resume", "ours"]
        return 0
    get = lambda *a: (200, claude_ok_body(five=100, five_reset=iso(now() + timedelta(hours=1))))
    assert L.run(ctx, "claude", ["--continue"], spawn=spawn, get=get) == 0
    assert len(calls) == 2
