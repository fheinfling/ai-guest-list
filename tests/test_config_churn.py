"""Realistic config churn and Claude's mkdir lock. All files/identities are synthetic."""
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

import pytest

from acctsw import claudehome as H, launcher as L
from tests.test_claudehome import seed, put


INTENT = {
    "allowedTools": [], "hasTrustDialogAccepted": False, "mcpServers": {},
    "enabledMcpjsonServers": [], "disabledMcpjsonServers": [], "disabledMcpServers": [],
    "mcpContextUris": [], "hasCompletedProjectOnboarding": False,
    "hasClaudeMdExternalIncludesApproved": False,
    "hasClaudeMdExternalIncludesWarningShown": False,
}
CHURN = {
    "lastCost": 0.01, "lastSessionId": "session", "lastDuration": 100,
    "lastAPIDuration": 80, "lastAPIDurationWithoutRetries": 70,
    "lastTotalInputTokens": 100, "lastTotalOutputTokens": 50,
    "lastTotalCacheCreationInputTokens": 30, "lastTotalCacheReadInputTokens": 20,
    "lastModelUsage": {"opus": {"costUSD": .01}}, "lastSessionMetrics": {"turns": 1},
    "lastFpsAverage": 50, "lastFpsLow": 20, "lastLinesAdded": 10, "lastLinesRemoved": 2,
    "lastToolDuration": 5, "lastGracefulShutdown": True, "lastStartTime": 1234,
    "lastVersionBase": "2.1", "lastTotalWebSearchRequests": 1,
    "exampleFiles": ["main.py"], "exampleFilesGeneratedAt": 4567,
    "hasUnseenTeamArtifacts": True, "reactVulnerabilityCache": {"checked": True},
    "localSettingsSeenGitTracked": True, "futureUnknownProjectField": {"private": 1},
}


def read(path):
    return json.loads(path.read_text())


def profile(email):
    return {"emailAddress": email, "accountUuid": "account:" + email,
            "organizationUuid": "org:" + email, "displayName": email, "billingType": "team"}


def prepare(ctx):
    canonical = ctx._claude_real / ".claude.json"
    put(canonical, {"oauthAccount": profile("stock@test"), "theme": "dark",
                    "tipsHistory": {"tip": 10}, "mcpServers": {},
                    "projects": {p: {**deepcopy(INTENT), **deepcopy(CHURN)}
                                 for p in ("/work", "/other", "/third")},
                    "cachedGrowthBookFeatures": {"private": "stock"},
                    "futureUnknownTopField": {"private": "stock"}})
    paths = {}
    for email in ("a@test", "b@test"):
        seed(ctx, email)
        home = H.ensure_home(ctx, email)
        path = home / ".claude.json"
        config = read(path)
        config["oauthAccount"] = profile(email)
        put(path, config)
        paths[email] = path
    return canonical, paths


def churn(path, owner, turn):
    config = read(path)
    for project in config["projects"].values():
        for key in CHURN:
            project[key] = {"owner": owner, "turn": turn}
    config["tipsHistory"] = {"tip": turn, "owner": owner}
    config["futureUnknownTopField"] = {"owner": owner}
    put(path, config)


def test_repeated_two_seat_and_stock_churn_keeps_profiles_and_private_stats(ctx, monkeypatch):
    canonical, paths = prepare(ctx)
    monkeypatch.setattr(H.codexhome, "_park", lambda p: pytest.fail(f"parked {p.name}"))
    # New homes import intent only, including in projects where stock has caches.
    for path in paths.values():
        assert "tipsHistory" not in read(path)
        for project in read(path)["projects"].values():
            assert not CHURN.keys() & project.keys()
    for turn in range(1, 5):
        for email, path in paths.items():
            H.merge_config(ctx, email)  # launch
            churn(path, email, turn)
        churn(canonical, "stock", turn)
        a, b, stock = read(paths["a@test"]), read(paths["b@test"]), read(canonical)
        a["projects"]["/work"]["hasTrustDialogAccepted"] = True
        b["projects"]["/work"]["allowedTools"] = ["Read", "Bash(git status)"]
        stock["projects"]["/work"]["mcpServers"]["stock"] = {"command": "serve"}
        put(paths["a@test"], a); put(paths["b@test"], b); put(canonical, stock)
        for email in ("a@test", "b@test", "a@test"):
            H.merge_config(ctx, email)  # exits and next launch bring intent to both seats
        for owner, path in [("stock", canonical), *paths.items()]:
            config = read(path)
            assert config["oauthAccount"] == profile("stock@test" if owner == "stock" else owner)
            assert config["tipsHistory"] == {"tip": turn, "owner": owner}
            assert config["futureUnknownTopField"] == {"owner": owner}
            for project in config["projects"].values():
                assert {k: project[k] for k in CHURN} == {
                    k: {"owner": owner, "turn": turn} for k in CHURN}
            project = config["projects"]["/work"]
            assert project["hasTrustDialogAccepted"] is True
            assert project["allowedTools"] == ["Read", "Bash(git status)"]
            assert project["mcpServers"] == {"stock": {"command": "serve"}}
        assert not list(ctx.data_dir.rglob("*.orphaned-*"))


def test_real_conflict_canonical_wins_each_key_keeps_identity_and_other_changes(ctx, monkeypatch):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]
    ours, stock = read(path), read(canonical)
    ours["theme"] = "seat"
    ours["projects"]["/work"]["allowedTools"] = ["Read"]
    ours["projects"]["/work"]["lastCost"] = 42
    ours["projects"]["/work"]["hasTrustDialogAccepted"] = True
    ours["mcpServers"]["seat"] = {"command": "seat"}
    stock["theme"] = "canonical"
    stock["projects"]["/work"]["allowedTools"] = ["Write"]
    stock["mcpServers"]["stock"] = {"command": "stock"}
    put(path, ours); put(canonical, stock)
    monkeypatch.setattr(H.codexhome, "_park", lambda p: pytest.fail("parked config"))
    H.merge_config(ctx, "a@test")
    result = read(path)
    assert result["theme"] == "canonical"
    assert result["projects"]["/work"]["allowedTools"] == ["Write"]
    assert result["projects"]["/work"]["lastCost"] == 42
    assert result["projects"]["/work"]["hasTrustDialogAccepted"] is True
    assert result["mcpServers"] == {"seat": {"command": "seat"}, "stock": {"command": "stock"}}
    assert result["oauthAccount"] == profile("a@test")
    assert read(canonical)["oauthAccount"] == profile("stock@test")
    assert not list(path.parent.glob("*.orphaned-*"))


def test_direct_mcp_add_has_no_exit_merge_but_next_merge_publishes(ctx, monkeypatch):
    canonical, paths = prepare(ctx)
    state = ctx.load_state(); state.set_active("claude", "a@test"); state.save()
    path = paths["a@test"]
    def execute(binary, argv, env):
        assert argv[1:] == ["mcp", "add", "local", "serve"]
        assert Path(env["CLAUDE_CONFIG_DIR"]) == path.parent
        config = read(path)
        config["projects"]["/work"]["mcpServers"]["local"] = {"command": "serve"}
        config["projects"]["/work"]["lastCost"] = "seat metric"
        put(path, config)
        raise OSError("fake exec returns to the test")
    monkeypatch.setattr(L.os, "execvpe", execute)
    assert L.run(ctx, "claude", ["mcp", "add", "local", "serve"]) == 127
    assert "local" not in read(canonical)["projects"]["/work"]["mcpServers"]
    stock = read(canonical); stock["projects"]["/work"]["lastCost"] = "stock metric"; put(canonical, stock)
    H.merge_config(ctx, "a@test")
    assert read(canonical)["projects"]["/work"]["mcpServers"]["local"] == {"command": "serve"}
    assert read(path)["oauthAccount"] == profile("a@test")
    assert read(path)["projects"]["/work"]["lastCost"] == "seat metric"
    assert not list(path.parent.glob("*.orphaned-*"))


def test_lock_holder_defers_merge_and_keeps_pending_changes_for_next_round(ctx):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]; baseline = path.parent / ".acctsw-shared.json"
    config = read(path); config["mcpServers"]["pending"] = {"command": "seat"}; put(path, config)
    old_local, old_base = path.read_bytes(), baseline.read_bytes()
    lock = Path(str(canonical) + ".lock")
    holding, finish = threading.Event(), threading.Event()
    errors = []
    def stock_writer():
        try:
            lock.mkdir()
            stock = read(canonical); stock["model"] = "stock model"
            holding.set()
            assert finish.wait(2)
            put(canonical, stock)
        except Exception as exc:
            errors.append(exc)
        finally:
            lock.rmdir()
    writer = threading.Thread(target=stock_writer)
    writer.start()
    assert holding.wait(2)
    started = time.monotonic()
    try:
        with ctx.locked():
            H.merge_config(ctx, "a@test")
        assert time.monotonic() - started < 2
        assert path.read_bytes() == old_local
        assert baseline.read_bytes() == old_base
        assert "pending" not in read(canonical)["mcpServers"]
        assert lock.is_dir()
    finally:
        finish.set()
        writer.join(2)
    assert not errors
    assert not writer.is_alive()
    H.merge_config(ctx, "a@test")
    assert read(canonical)["mcpServers"]["pending"] == {"command": "seat"}
    assert read(canonical)["model"] == "stock model"
    assert read(path)["model"] == "stock model"
    assert not lock.exists()


@pytest.mark.parametrize("age, reclaimed", [(9, False), (10, False), (11, True)])
def test_lock_stale_rule_is_ten_seconds_and_writes_hold_lock(ctx, monkeypatch, age, reclaimed):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]
    config = read(path); config["theme"] = "seat"; put(path, config)
    lock = Path(str(canonical) + ".lock"); lock.mkdir()
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    stamp = 1000 - age; os.utime(lock, (stamp, stamp))
    original_inode = lock.stat().st_ino
    write = H.atomic_write_text
    writes = []
    def record(p, text, **kw):
        if p == canonical:
            assert lock.is_dir()
            assert lock.stat().st_mtime > stamp
            writes.append(p)
        return write(p, text, **kw)
    monkeypatch.setattr(H, "atomic_write_text", record)
    H.merge_config(ctx, "a@test")
    assert bool(writes) is reclaimed
    if reclaimed:
        assert read(canonical)["theme"] == "seat"
        assert not lock.exists()
    else:
        assert read(canonical)["theme"] == "dark"
        assert lock.stat().st_ino == original_inode
        assert lock.stat().st_mtime == stamp
        lock.rmdir()


def test_canonical_is_reread_after_mkdir_not_from_prelock_snapshot(ctx, monkeypatch):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]; config = read(path); config["theme"] = "seat"; put(path, config)
    lock = Path(str(canonical) + ".lock")
    mkdir = Path.mkdir
    acquired = []
    def racing_mkdir(p, *args, **kw):
        if p == lock:
            stock = read(canonical); stock["model"] = "written just before acquisition"; put(canonical, stock)
            acquired.append(p)
        return mkdir(p, *args, **kw)
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    H.merge_config(ctx, "a@test")
    assert acquired
    assert read(canonical)["model"] == "written just before acquisition"
    assert read(canonical)["theme"] == "seat"
    assert read(path)["model"] == "written just before acquisition"


def test_canonical_lock_released_on_write_exception_without_baseline_advance(ctx, monkeypatch):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]; baseline = path.parent / ".acctsw-shared.json"
    config = read(path); config["theme"] = "pending"; put(path, config)
    old_base = baseline.read_bytes()
    lock = Path(str(canonical) + ".lock")
    write = H.atomic_write_text
    def fail(p, text, **kw):
        if p == canonical:
            assert lock.is_dir(), "canonical write did not acquire Claude's lock"
            raise OSError("synthetic disk failure")
        return write(p, text, **kw)
    monkeypatch.setattr(H, "atomic_write_text", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        H.merge_config(ctx, "a@test")
    assert not lock.exists()
    assert baseline.read_bytes() == old_base
    assert read(path)["theme"] == "pending"
    monkeypatch.setattr(H, "atomic_write_text", write)
    H.merge_config(ctx, "a@test")
    assert read(canonical)["theme"] == "pending"


@pytest.mark.parametrize("field, value", list(INTENT.items()))
def test_each_project_intent_field_propagates_both_ways(ctx, field, value):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]
    assert read(path)["projects"]["/other"][field] == value
    changed = True if value is False else (["choice"] if isinstance(value, list) else {"server": {"command": "run"}})
    config = read(path); config["projects"]["/other"][field] = changed; put(path, config)
    H.merge_config(ctx, "a@test")
    assert read(canonical)["projects"]["/other"][field] == changed
    stock = read(canonical); del stock["projects"]["/other"][field]; put(canonical, stock)
    H.merge_config(ctx, "a@test")
    assert field not in read(path)["projects"]["/other"]
    assert read(canonical)["projects"]["/other"]["lastCost"] == CHURN["lastCost"]


def test_tip_counts_and_unknown_project_fields_are_never_shared(ctx):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]
    assert "tipsHistory" not in read(path)
    assert "futureUnknownProjectField" not in read(path)["projects"]["/work"]
    ours = read(path)
    ours["tipsHistory"] = {"seat": 99}
    ours["projects"]["/seat-only"] = {"future": "seat"}
    ours["projects"]["/work"]["futureUnknownProjectField"] = {"seat": 1}
    put(path, ours)
    H.merge_config(ctx, "a@test")
    assert read(canonical)["tipsHistory"] == {"tip": 10}
    assert "/seat-only" not in read(canonical)["projects"]
    assert read(path)["projects"]["/seat-only"] == {"future": "seat"}
    assert read(path)["projects"]["/work"]["futureUnknownProjectField"] == {"seat": 1}
    assert read(canonical)["projects"]["/work"]["futureUnknownProjectField"] == {"private": 1}


def test_heartbeat_refreshes_mtime_every_five_seconds_and_stops(ctx, monkeypatch):
    canonical, _ = prepare(ctx)
    lock = Path(str(canonical) + ".lock")
    real_event, real_utime = threading.Event, os.utime
    refreshed = real_event()
    waits = []
    made = []
    class FastStop:
        def __init__(self):
            self.event = real_event()
        def wait(self, timeout):
            waits.append(timeout)
            return self.event.wait(.005)
        def set(self):
            self.event.set()
    def event():
        e = FastStop() if not made else real_event()
        made.append(e)
        return e
    def utime(p, *args, **kw):
        result = real_utime(p, *args, **kw)
        if p == lock:
            refreshed.set()
        return result
    monkeypatch.setattr(H.threading, "Event", event)
    monkeypatch.setattr(H.os, "utime", utime)
    before_threads = set(threading.enumerate())
    with H._config_lock(canonical) as owned:
        assert owned()
        workers = set(threading.enumerate()) - before_threads
        assert workers and all(t.daemon for t in workers)  # heartbeat cannot hold up process exit
        assert refreshed.wait(1)
        assert waits and set(waits) == {5}
    assert not lock.exists()
    assert made[0].event.is_set()


@pytest.mark.parametrize("successor", [False, True])
def test_compromised_lock_defers_and_never_releases_successors_lock(ctx, monkeypatch, successor):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]; config = read(path); config["theme"] = "pending"; put(path, config)
    baseline = path.parent / ".acctsw-shared.json"; old_base = baseline.read_bytes()
    lock = Path(str(canonical) + ".lock"); previous = lock.with_name("old-lock")
    read_config = H._read
    def stolen(p):
        result = read_config(p)
        if p == canonical:
            lock.rename(previous)
            if successor:
                lock.mkdir()
        return result
    monkeypatch.setattr(H, "_read", stolen)
    H.merge_config(ctx, "a@test")
    assert read(canonical)["theme"] == "dark"
    assert read(path)["theme"] == "pending"
    assert baseline.read_bytes() == old_base
    assert lock.is_dir() is successor  # any replacement belongs to the successor, not ours


def test_stock_identity_mirror_also_uses_global_lock(ctx, monkeypatch):
    from acctsw.errors import AcctswError
    canonical, paths = prepare(ctx)
    lock = Path(str(canonical) + ".lock"); lock.mkdir()
    original = canonical.read_bytes()
    with pytest.raises(AcctswError, match="busy"):
        H.mirror_identity(ctx, "a@test")
    assert canonical.read_bytes() == original
    lock.rmdir()
    write = H.atomic_write_text
    def record(p, text, **kw):
        assert lock.is_dir()
        return write(p, text, **kw)
    monkeypatch.setattr(H, "atomic_write_text", record)
    H.mirror_identity(ctx, "a@test")
    assert read(canonical)["oauthAccount"] == profile("a@test")
    assert read(canonical)["projects"]["/work"]["lastCost"] == CHURN["lastCost"]
    assert not lock.exists()


def test_pending_intent_can_create_missing_canonical_parent(ctx):
    ctx.claude_real = ctx._claude_real / "new" / "nested"
    home = ctx.claude_home("a@test")
    put(home / ".claude.json", {"theme": "light", "oauthAccount": profile("a@test")})
    H.merge_config(ctx, "a@test")
    assert read(ctx._claude_real / ".claude.json") == {"theme": "light"}
    assert read(home / ".claude.json")["oauthAccount"] == profile("a@test")


def test_release_waits_for_inflight_heartbeat_before_removing_lock(ctx, monkeypatch):
    canonical, _ = prepare(ctx)
    lock = Path(str(canonical) + ".lock")
    real_event, real_thread, real_utime = threading.Event, threading.Thread, os.utime
    entered, finish, stopped, checked = [real_event() for _ in range(4)]
    made = []
    observations = []
    class FastStop:
        def wait(self, timeout):
            assert timeout == 5
            return stopped.wait(.005)
        def set(self):
            stopped.set()
    def event():
        e = FastStop() if not made else real_event()
        made.append(e)
        return e
    def utime(p, *args, **kw):
        if p == lock:
            entered.set()
            assert finish.wait(2)
        return real_utime(p, *args, **kw)
    def check_then_finish():
        assert stopped.wait(2)
        checked.wait(.03)
        observations.append(lock.exists())
        finish.set()
    checker = real_thread(target=check_then_finish)
    monkeypatch.setattr(H.threading, "Event", event)
    monkeypatch.setattr(H.os, "utime", utime)
    checker.start()
    try:
        with H._config_lock(canonical):
            assert entered.wait(2)
    finally:
        finish.set()
        checker.join(2)
    assert observations == [True]
    assert not lock.exists()


def test_unknown_top_level_names_resembling_project_config_stay_private(ctx):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]
    stock, ours = read(canonical), read(path)
    # A future project storage field must not become shared by resembling a known namespace.
    for name in ("projects_mutated", "projectsV2", "projectsCache"):
        stock[name] = {"stock": 1}
        ours[name] = {"seat": 2}
    put(canonical, stock); put(path, ours)
    H.merge_config(ctx, "a@test")
    for name in ("projects_mutated", "projectsV2", "projectsCache"):
        assert read(canonical)[name] == {"stock": 1}
        assert read(path)[name] == {"seat": 2}


@pytest.mark.parametrize("race", ["disappears", "successor"])
def test_stale_lock_takeover_race_defers_without_owning_or_releasing_other_lock(ctx, monkeypatch, race):
    canonical, paths = prepare(ctx)
    path = paths["a@test"]; baseline = path.parent / ".acctsw-shared.json"
    config = read(path); config["theme"] = "pending"; put(path, config)
    old_local, old_base = path.read_bytes(), baseline.read_bytes()
    lock = Path(str(canonical) + ".lock"); lock.mkdir()
    stamp = time.time() - 20; os.utime(lock, (stamp, stamp))
    stat, mkdir = Path.stat, Path.mkdir
    creates = []
    def racing_stat(p, *args, **kw):
        if p == lock and race == "disappears":
            lock.rmdir()
            raise FileNotFoundError("stock released before our stat")
        return stat(p, *args, **kw)
    def racing_mkdir(p, *args, **kw):
        if p == lock:
            creates.append(p)
            if race == "successor" and len(creates) == 2:
                mkdir(p)  # stock wins the race after the stale lock was removed
        return mkdir(p, *args, **kw)
    monkeypatch.setattr(Path, "stat", racing_stat)
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    H.merge_config(ctx, "a@test")
    monkeypatch.setattr(Path, "stat", stat)
    assert path.read_bytes() == old_local
    assert baseline.read_bytes() == old_base
    assert read(canonical)["theme"] == "dark"
    assert lock.exists() is (race == "successor")


@pytest.mark.parametrize("fault", ["vanished", "replaced", "utime"])
def test_heartbeat_remembers_compromise_and_stops_touching_the_lock(ctx, monkeypatch, fault):
    canonical = ctx._claude_real / ".claude.json"
    put(canonical, {})
    lock = Path(str(canonical) + ".lock"); previous = lock.with_name("old-lock")
    real_event, real_utime = threading.Event, os.utime
    made, waits, touches = [], [], []
    class SingleTick:
        def wait(self, timeout):
            waits.append(timeout)
            return len(waits) > 1
        def set(self):
            pass
    def event():
        result = SingleTick() if not made else real_event()
        made.append(result)
        return result
    class DrivenHeartbeat:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            if fault != "utime":
                lock.rename(previous)
                if fault == "replaced":
                    lock.mkdir()
            self.target()
            # Restore the original inode: a previously observed loss still invalidates ownership.
            if fault != "utime":
                if lock.exists():
                    lock.rmdir()
                previous.rename(lock)
        def join(self):
            pass
    def utime(p, *args, **kw):
        touches.append(p)
        if fault == "utime":
            raise OSError("synthetic heartbeat I/O error")
        return real_utime(p, *args, **kw)
    monkeypatch.setattr(H.threading, "Event", event)
    monkeypatch.setattr(H.threading, "Thread", DrivenHeartbeat)
    monkeypatch.setattr(H.os, "utime", utime)
    with H._config_lock(canonical) as owned:
        assert not owned()
        assert waits == [5]  # abort at the first failed heartbeat; never keep refreshing/retrying
        assert touches == ([lock] if fault == "utime" else [])
    assert not lock.exists()


@pytest.mark.parametrize("projects", [None, [], "unexpected namespace"])
def test_non_object_project_namespace_is_never_imported_as_shared_intent(ctx, projects):
    seed(ctx)
    canonical = ctx._claude_real / ".claude.json"
    put(canonical, {"theme": "dark", "projects": projects})
    home = H.ensure_home(ctx, "a@test")
    assert "projects" not in read(home / ".claude.json")
    assert read(canonical)["projects"] == projects


def test_lock_signals_heartbeat_to_stop_before_join(ctx, monkeypatch):
    canonical = ctx._claude_real / ".claude.json"
    put(canonical, {})
    real_event = threading.Event
    events = []
    def event():
        result = real_event(); events.append(result)
        return result
    class WaitingHeartbeat:
        def __init__(self, *, target, daemon):
            pass
        def start(self):
            pass
        def join(self):
            assert events[0].is_set(), "joining a heartbeat before stopping it would hang"
    monkeypatch.setattr(H.threading, "Event", event)
    monkeypatch.setattr(H.threading, "Thread", WaitingHeartbeat)
    with H._config_lock(canonical) as owned:
        assert owned()
    assert not Path(str(canonical) + ".lock").exists()
