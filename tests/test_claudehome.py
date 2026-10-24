"""Private Claude OAuth items, config sharing and migration. Never use a real Keychain."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from acctsw import accounts, identity, usage
from acctsw.credlocations import ClaudeCredLocation
from acctsw.errors import CredentialIdentityMismatch, MissingSnapshot
from acctsw.keychain import SecurityKeychain
from acctsw.switch import switch
from tests.conftest import make_claude_blob
from tests.test_usage import claude_ok_body


@pytest.fixture
def homes():
    from acctsw import claudehome
    return claudehome


def seed(ctx, email="a@test", token="a"):
    state = ctx.load_state()
    state.upsert_seat("claude", email)
    state.set_active("claude", email)
    ctx.snapshot_set("claude", email, make_claude_blob().replace('"x"', json.dumps(token)))
    state.save()
    return state


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_claude_official_items_use_raw_json(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (
        seen.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    backend = SecurityKeychain()
    loc = ClaudeCredLocation(backend, "Claude Code-credentials-deadbeef", "tester")
    blob = make_claude_blob()
    loc.set_live(blob)
    assert seen[-1][-1] == blob
    backend.set("snapshots", "a", blob)
    assert seen[-1][-1] != blob  # our snapshots retain their established encoding


@pytest.mark.parametrize("path, expected", [
    (None, ""), ("", ""), ("/tmp/claude-seat", "-01b1bae2"),
    ("/tmp/claude-seat/", "-561ce9c7"), ("/tmp/café", "-0873cca0"),
    ("/tmp/cafe\u0301", "-0873cca0"), ("relative/path", "-fdc1a528"),
    ("/tmp/alias/../home", "-42d152d3"),
])
def test_service_name_preserves_claudes_exact_path_rule(homes, path, expected):
    assert homes.service_name(path) == "Claude Code-credentials" + expected


def test_migration_seeds_raw_home_without_overwriting_stock(ctx, homes):
    seed(ctx)
    ctx.cred["claude"].set_live("stock")
    home = homes.ensure_home(ctx, "a@test")
    assert home == ctx.claude_home("a@test")
    assert ctx.claude_cred("a@test").get_live() == ctx.snapshot_get("claude", "a@test")
    assert ctx.cred["claude"].get_live() == "stock"
    assert home.stat().st_mode & 0o777 == 0o700
    for name in (".claude.json", ".acctsw-shared.json", ".acctsw-seeded", ".acctsw-unverified"):
        assert (home / name).stat().st_mode & 0o777 == 0o600
    assert len(home.name) == 8
    assert homes.home_dir("A@TEST", home.parent) == home
    assert homes.load(ctx, "a@test") == ctx.snapshot_get("claude", "a@test")
    assert not (home / ".claude.json").is_symlink()


def test_existing_home_tokens_win_over_old_snapshot(ctx, homes):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    ctx.claude_cred("a@test").set_live("rotated")
    homes.ensure_home(ctx, "a@test")
    assert homes.load(ctx, "a@test") == "rotated"


def test_missing_snapshot_is_a_clean_error(ctx, homes):
    with pytest.raises(MissingSnapshot, match="re-add"):
        homes.ensure_home(ctx, "missing@test")
    assert ctx.claude_cred("missing@test").get_live() is None
    with pytest.raises(MissingSnapshot):
        seed(ctx, "missing@test")
        ctx.keychain.delete(ctx.keychain_service, ctx.snapshot_key("claude", "missing@test"))
        switch(ctx, ctx.load_state(), "claude", "missing@test", sync=False, mirror=False)


def test_shared_entries_and_private_identity_and_runtime(ctx, homes):
    seed(ctx)
    real = ctx._claude_real
    put(real / ".claude.json", {"oauthAccount": {"emailAddress": "other@test", "accountUuid": "Y"},
                                "projects": {"/work": {"hasTrustDialogAccepted": True}},
                                "mcpServers": {"local": {"command": "serve"}},
                                "primaryApiKey": "private"})
    for name in ["settings.json", "CLAUDE.md", "history.jsonl", "remote-settings.json", ".credentials.json",
                 ".claude.json.backup.123", ".config.json.lock", "policy-limits.json"]:
        (real / name).write_text("{}")
    (real / "daemon").mkdir()
    (real / "skills").mkdir()
    home = homes.ensure_home(ctx, "a@test")
    for name in ["settings.json", "CLAUDE.md", "history.jsonl", "projects", "skills", "agents", "plugins", "hooks"]:
        assert (home / name).is_symlink(), name
        assert (home / name).resolve() == (real / name).resolve()
    for name in ["remote-settings.json", ".credentials.json", ".claude.json.backup.123", ".config.json.lock",
                 "policy-limits.json", "daemon"]:
        assert not (home / name).exists(), name
    config = json.loads((home / ".claude.json").read_text())
    assert config["oauthAccount"] == {"emailAddress": "a@test"}
    assert config["mcpServers"] == {"local": {"command": "serve"}}
    assert config["projects"]["/work"]["hasTrustDialogAccepted"]
    assert "primaryApiKey" not in config


def test_capture_matching_profile_and_never_share_it(ctx, homes):
    seed(ctx)
    profile = {"emailAddress": "a@test", "organizationUuid": "org", "accountUuid": "a"}
    put(ctx._claude_real / ".claude.json", {"oauthAccount": profile, "mcpServers": {}})
    a = homes.ensure_home(ctx, "a@test")
    assert json.loads((a / ".claude.json").read_text())["oauthAccount"] == profile
    seed(ctx, "b@test", "b")
    b = homes.ensure_home(ctx, "b@test")
    assert json.loads((b / ".claude.json").read_text())["oauthAccount"] == {"emailAddress": "b@test"}
    assert "accountUuid" not in json.loads((b / ".claude.json").read_text())["oauthAccount"]


def test_atomic_rename_shared_file_is_parked_and_new_file_promoted(ctx, homes):
    seed(ctx)
    real = ctx._claude_real
    put(real / "settings.json", {"stock": True})
    home = homes.ensure_home(ctx, "a@test")
    put(home / "replacement", {"seat": True})
    os.replace(home / "replacement", home / "settings.json")
    put(home / "new-shared.json", {"new": True})
    put(home / ".claude.json.backup.123", {"oauthAccount": {"emailAddress": "a@test"}})
    homes.ensure_home(ctx, "a@test", promote=True)
    assert (home / "settings.json").is_symlink()
    assert json.loads((real / "settings.json").read_text()) == {"stock": True}
    parked = list(home.glob("settings.json.orphaned-*"))
    assert len(parked) == 1 and json.loads(parked[0].read_text()) == {"seat": True}
    assert (home / "new-shared.json").is_symlink()
    assert json.loads((real / "new-shared.json").read_text()) == {"new": True}
    assert not (real / ".claude.json.backup.123").exists()
    homes.ensure_home(ctx, "a@test", promote=True)
    assert parked == list(home.glob("settings.json.orphaned-*"))


def test_config_three_way_merge_preserves_other_seat_identity_and_concurrent_edits(ctx, homes):
    seed(ctx)
    real = ctx._claude_real / ".claude.json"
    put(real, {"oauthAccount": {"emailAddress": "stock@test"}, "projects": {"/p": {"hasTrustDialogAccepted": False}},
               "mcpServers": {"old": {"command": "old"}}, "theme": "dark"})
    a = homes.ensure_home(ctx, "a@test")
    seed(ctx, "b@test", "b")
    b = homes.ensure_home(ctx, "b@test")
    ours = json.loads((a / ".claude.json").read_text())
    ours["projects"]["/p"]["hasTrustDialogAccepted"] = True
    ours["mcpServers"]["new"] = {"command": "new"}
    ours["oauthAccount"] = {"emailAddress": "a@test", "accountUuid": "a"}
    put(a / ".claude.json", ours)
    stock = json.loads(real.read_text())
    stock["theme"] = "light"
    put(real, stock)
    homes.merge_config(ctx, "a@test")
    homes.merge_config(ctx, "b@test")
    shared = json.loads(real.read_text())
    bconfig = json.loads((b / ".claude.json").read_text())
    assert shared["projects"]["/p"]["hasTrustDialogAccepted"] is True
    assert shared["mcpServers"]["new"] == {"command": "new"}
    assert shared["theme"] == "light"
    assert shared["oauthAccount"] == {"emailAddress": "stock@test"}
    assert bconfig["oauthAccount"] == {"emailAddress": "b@test"}
    assert bconfig["projects"] == shared["projects"]
    assert bconfig["mcpServers"] == shared["mcpServers"]


def test_config_conflict_keeps_canonical_without_parking_private_copy(ctx, homes):
    seed(ctx)
    real = ctx._claude_real / ".claude.json"
    put(real, {"theme": "old", "mcpServers": {"removed": {}}})
    home = homes.ensure_home(ctx, "a@test")
    config = json.loads((home / ".claude.json").read_text())
    config["theme"] = "seat"
    del config["mcpServers"]["removed"]
    put(home / ".claude.json", config)
    put(real, {"theme": "stock", "mcpServers": {"removed": {}}})
    homes.merge_config(ctx, "a@test")
    assert json.loads(real.read_text()) == {"theme": "stock", "mcpServers": {}}
    assert not list(home.glob(".claude.json.orphaned-*"))
    assert json.loads((home / ".claude.json").read_text()) == {**config, "theme": "stock"}


def test_legacy_config_and_default_global_config_location(ctx, homes):
    seed(ctx)
    ctx.claude_config_explicit = False
    put(ctx._claude_real.parent / ".claude.json", {"mcpServers": {"default": {}}})
    home = homes.ensure_home(ctx, "a@test")
    assert "default" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    put(ctx._claude_real / ".config.json", {"mcpServers": {"legacy": {}}})
    homes.ensure_home(ctx, "a@test")
    assert "legacy" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert not (home / ".config.json").exists()


@pytest.mark.parametrize("actual", [None, "y@test"])
def test_foreign_or_unknown_home_login_is_never_captured_as_x(ctx, homes, actual):
    state = seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    previous = ctx.snapshot_get("claude", "a@test")
    put(home / ".claude.json", {"oauthAccount": {"emailAddress": "y@test"}})
    ctx.claude_cred("a@test").set_live("Y's credentials")
    answer = identity.ClaudeLiveIdentity("Y's credentials", actual)
    assert not homes.sync_back(ctx, state, "a@test", live_identity=answer)
    assert ctx.snapshot_get("claude", "a@test") == previous
    if actual:
        assert homes.load(ctx, "a@test") is None
        with pytest.raises(CredentialIdentityMismatch):
            homes.ensure_home(ctx, "a@test")
    else:
        # Unknown token ownership cannot be inferred from the config's stale label.
        assert homes.load(ctx, "a@test") == "Y's credentials"
    assert ctx.claude_cred("a@test").get_live() == "Y's credentials"


def test_identity_answer_is_rechecked_and_removed_seat_cannot_be_resurrected(ctx, homes):
    state = seed(ctx)
    homes.ensure_home(ctx, "a@test")
    answer = identity.ClaudeLiveIdentity("old", "a@test")
    ctx.claude_cred("a@test").set_live("new")
    assert not homes.sync_back(ctx, state, "a@test", live_identity=answer)
    state.remove_seat("claude", "a@test")
    answer = identity.ClaudeLiveIdentity("new", "a@test")
    assert not homes.sync_back(ctx, state, "a@test", live_identity=answer)
    assert ctx.snapshot_get("claude", "a@test") != "new"


def test_claude_profile_is_scoped_to_home_and_bound_to_exact_bytes(ctx, homes):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    ctx.cred["claude"].set_live(make_claude_blob().replace('"x"', '"stock"'))
    seen = []
    def get(url, headers, timeout):
        seen.append((url, headers, timeout))
        return 200, '{"account":{"email":"a@test"}}'
    result = identity.claude_live_identity(ctx, email="a@test", get=get)
    assert result == identity.ClaudeLiveIdentity(ctx.claude_cred("a@test").get_live(), "a@test")
    assert seen[0][0] == "https://api.anthropic.com/api/oauth/profile"
    assert seen[0][1]["Authorization"] == "Bearer a"
    assert seen[0][2] == 10.0


def test_usage_reads_current_home_tokens_for_active_and_parked_seats(ctx, homes):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    state = seed(ctx, "b@test", "b")
    homes.ensure_home(ctx, "b@test")
    ctx.claude_cred("a@test").set_live(make_claude_blob().replace('"x"', '"a-rotated"'))
    ctx.claude_cred("b@test").set_live(make_claude_blob().replace('"x"', '"b-rotated"'))
    ctx.cred["claude"].set_live("unrelated stock")
    seen = []
    def get(url, headers, timeout):
        seen.append(headers["Authorization"])
        return 200, claude_ok_body()
    result = usage.refresh_live(ctx, "claude", force=True, get=get, user_agent="test",
                                post=lambda *a: pytest.fail("acctsw must never refresh Claude"))
    assert result == {"claude": {"a@test": "ok", "b@test": "ok"}}
    assert set(seen) == {"Bearer a-rotated", "Bearer b-rotated"}
    assert usage._seat_blob(ctx, state, "claude", "a@test") == ctx.claude_cred("a@test").get_live()


def test_stock_readd_repairs_only_matching_home_and_old_stock_never_rolls_tokens_back(ctx, homes):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    seed(ctx, "b@test", "b")
    homes.ensure_home(ctx, "b@test")
    ctx.cred["claude"].set_live("fresh login A")
    state = ctx.load_state()
    state.get_seat("claude", "a@test")["auth_error"] = "revoked"
    answer = identity.ClaudeLiveIdentity("fresh login A", "a@test")
    assert accounts.add(ctx, state, "claude", email=answer.email, blob=answer.blob)["email"] == "a@test"
    assert ctx.claude_cred("a@test").get_live() == "fresh login A"
    b = ctx.claude_cred("b@test").get_live()
    ctx.claude_cred("a@test").set_live("child refreshed A")
    assert accounts.reconcile_claude(ctx, state, live_identity=answer) is None
    assert ctx.claude_cred("a@test").get_live() == "child refreshed A"
    assert ctx.claude_cred("b@test").get_live() == b
    assert "auth_error" not in state.get_seat("claude", "a@test")


def test_removing_home_deletes_item_and_links_but_preserves_real_shared_files(ctx, homes):
    state = seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(ctx._claude_real / "projects" / "p" / "session.jsonl", {"data": "keep"})
    assert accounts.remove(ctx, state, "claude", "a@test")
    assert not home.exists()
    assert ctx.claude_cred("a@test").get_live() is None
    assert ctx.snapshot_get("claude", "a@test") is None
    assert (ctx._claude_real / "projects" / "p" / "session.jsonl").exists()


def test_logout_does_not_reinstall_old_snapshot_but_readd_repairs_home(ctx, homes):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    item = ctx.claude_cred("a@test")
    ctx.keychain.delete(item.service, item.account)
    with pytest.raises(MissingSnapshot, match="removed"):
        homes.ensure_home(ctx, "a@test")
    ctx.cred["claude"].set_live("new login")
    accounts.add(ctx, ctx.load_state(), "claude", email="a@test")
    assert item.get_live() == "new login"


def test_healthy_home_survives_missing_backup_snapshot(ctx, homes):
    state = seed(ctx)
    homes.ensure_home(ctx, "a@test")
    ctx.keychain.delete(ctx.keychain_service, ctx.snapshot_key("claude", "a@test"))
    switch(ctx, state, "claude", "a@test", sync=False, mirror=False)
    assert state.active("claude") == "a@test"
    assert homes.load(ctx, "a@test") is not None


def test_home_root_cannot_redirect_writes_into_user_directory(ctx, homes, tmp_path):
    seed(ctx)
    home = ctx.claude_home("a@test")
    home.parent.mkdir(parents=True)
    real = tmp_path / "elsewhere"
    real.mkdir()
    home.symlink_to(real, target_is_directory=True)
    with pytest.raises(CredentialIdentityMismatch, match="private"):
        homes.ensure_home(ctx, "a@test")
    assert not list(real.iterdir())


def test_new_nested_config_edits_merge_without_losing_other_projects(homes):
    result, conflict = homes._merge({}, {"projects": {"a": {"hasTrustDialogAccepted": True}}},
                                    {"projects": {"b": {"hasTrustDialogAccepted": True}}})
    assert result == {"projects": {"a": {"hasTrustDialogAccepted": True}, "b": {"hasTrustDialogAccepted": True}}}
    assert conflict is False


@pytest.mark.parametrize("name", [
    ".claude.json", ".config.json", ".claude.json.backup", ".credentials.json",
    ".acctsw-shared.json", ".acctsw-seeded", "backups", "statsig", "remote-settings.json",
    "policy-limits.json", "managed-settings.json", "hfi-auth.json", ".session_ingress_token",
    "daemon", "daemon.json", "jobs", "teams", "usage-data",
])
def test_private_entry_symlinks_are_unlinked_and_never_promoted(ctx, homes, name):
    seed(ctx)
    real, home = ctx._claude_real, ctx.claude_home("a@test")
    real.mkdir(parents=True)
    put(real / name, {})
    home.mkdir(parents=True)
    (home / name).symlink_to(real / name)
    homes.ensure_home(ctx, "a@test", promote=True)
    assert not (home / name).is_symlink()
    if name not in (".claude.json", ".acctsw-shared.json", ".acctsw-seeded"):
        assert not (home / name).exists()
    assert json.loads((real / name).read_text()) == {}


@pytest.mark.parametrize("field", [
    "mcpServers", "hasCompletedOnboarding", "lastOnboardingVersion", "theme",
    "preferredNotifChannel", "editorMode", "verbose", "model", "autoUpdates",
    "shiftEnterKeyBindingInstalled", "hasCompletedProjectOnboarding",
])
def test_shared_config_fields_follow_canonical_updates_and_publish_seat_changes(ctx, homes, field):
    seed(ctx)
    real = ctx._claude_real / ".claude.json"
    put(real, {field: {"initial": True}})
    home = homes.ensure_home(ctx, "a@test")
    config = json.loads((home / ".claude.json").read_text())
    assert config[field] == {"initial": True}
    config[field]["seat"] = True
    put(home / ".claude.json", config)
    homes.merge_config(ctx, "a@test")
    assert json.loads(real.read_text())[field] == {"initial": True, "seat": True}
    put(real, {field: {"stock": True}})
    homes.merge_config(ctx, "a@test")
    assert json.loads((home / ".claude.json").read_text())[field] == {"stock": True}


def test_wrong_and_dangling_shared_links_are_repaired_only_on_promoting_heal(ctx, homes, tmp_path):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    (home / "skills").unlink()
    (home / "skills").symlink_to(tmp_path / "gone")
    homes.ensure_home(ctx, "a@test")
    assert (home / "skills").resolve() == (ctx._claude_real / "skills").resolve()
    (home / "dangling").symlink_to(tmp_path / "gone")
    homes.ensure_home(ctx, "a@test", promote=True)
    assert not (home / "dangling").is_symlink()


@pytest.mark.parametrize("raw", ['[]', 'null', 'true', '"string"', 'broken{'])
def test_invalid_config_is_preserved_and_cannot_be_rewritten(ctx, homes, raw):
    seed(ctx)
    path = ctx._claude_real / ".claude.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        homes.ensure_home(ctx, "a@test")
    assert path.read_text() == raw


def test_unchanged_sync_has_no_state_write_but_changed_sync_clears_auth_error(ctx, homes):
    state = seed(ctx)
    homes.ensure_home(ctx, "a@test")
    before = state.data["rev"]
    answer = identity.ClaudeLiveIdentity(ctx.claude_cred("a@test").get_live(), "A@TEST")
    assert homes.sync_back(ctx, state, "a@test", live_identity=answer)
    assert state.data["rev"] == before
    state.get_seat("claude", "a@test")["auth_error"] = "revoked"
    ctx.claude_cred("a@test").set_live("new")
    answer = identity.ClaudeLiveIdentity("new", "A@TEST")
    assert homes.sync_back(ctx, state, "a@test", live_identity=answer)
    assert ctx.snapshot_get("claude", "a@test") == "new"
    assert "auth_error" not in state.get_seat("claude", "a@test")
    assert ctx.load_state().data["rev"] > before


def test_manual_mirror_moves_profile_without_mcp_loss(ctx, homes):
    state = seed(ctx)
    real = ctx._claude_real / ".claude.json"
    put(real, {"oauthAccount": {"emailAddress": "stock@test"}, "mcpServers": {"keep": {}}})
    home = homes.ensure_home(ctx, "a@test")
    config = json.loads((home / ".claude.json").read_text())
    config["oauthAccount"] = {"emailAddress": "a@test", "accountUuid": "uuid"}
    put(home / ".claude.json", config)
    switch(ctx, state, "claude", "a@test", sync=False)
    config = json.loads(real.read_text())
    assert config["oauthAccount"] == {"emailAddress": "a@test", "accountUuid": "uuid"}
    assert config["mcpServers"] == {"keep": {}}


def test_home_permissions_are_repaired_and_private_files_have_private_modes(ctx, homes):
    state = seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    os.chmod(home, 0o755)  # deliberately too open (temp test dir): ensure_home must repair it to 0700
    homes.ensure_home(ctx, "a@test")
    assert home.stat().st_mode & 0o777 == 0o700
    for name in (".claude.json", ".acctsw-shared.json", ".acctsw-seeded"):
        assert (home / name).stat().st_mode & 0o777 == 0o600
    switch(ctx, state, "claude", "a@test", sync=False)
    assert (ctx._claude_real / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_readd_initializes_home_and_replaces_its_own_profile(ctx, homes):
    seed(ctx)
    real = ctx._claude_real / ".claude.json"
    put(real, {"oauthAccount": {"emailAddress": "a@test", "accountUuid": "new-uuid"}})
    homes.save(ctx, "a@test", "fresh")
    home = ctx.claude_home("a@test")
    assert (home / "projects").is_symlink()
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == "new-uuid"
    assert ctx.claude_cred("a@test").get_live() == "fresh"
    put(real, {"oauthAccount": {"emailAddress": "a@test", "accountUuid": "newer"}})
    homes.merge_config(ctx, "a@test")
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == "new-uuid"
    homes.save(ctx, "a@test", "newer token")
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == "newer"


def test_all_well_known_shared_directories_exist_before_first_child(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    for name in ("projects", "skills", "agents", "plugins", "commands", "hooks", "tasks", "todos",
                 "plans", "file-history", "session-env", "shell-snapshots", "debug", "ide"):
        assert (home / name).is_symlink() and (home / name).is_dir(), name


def test_private_prefixes_exclude_new_backup_and_cache_variants(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    for prefix in (".claude.json", ".config.json", ".credentials", ".acctsw-", "remote-settings", "policy-limits"):
        name = prefix + '.new-variant'
        put(home / name, {"seat": True})
        put(ctx._claude_real / name, {"stock": True})
    homes.ensure_home(ctx, "a@test", promote=True)
    for prefix in (".claude.json", ".config.json", ".credentials", ".acctsw-", "remote-settings", "policy-limits"):
        name = prefix + '.new-variant'
        assert not (home / name).is_symlink()
        assert json.loads((home / name).read_text()) == {"seat": True}
        assert json.loads((ctx._claude_real / name).read_text()) == {"stock": True}


def test_existing_shared_real_file_waits_for_safe_promotion(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(home / "new-file", {"seat": True})
    put(ctx._claude_real / "new-file", {"stock": True})
    homes.ensure_home(ctx, "a@test")
    assert not (home / "new-file").is_symlink()
    assert json.loads((home / "new-file").read_text()) == {"seat": True}


def test_premigration_reads_snapshot_and_config_path_defaults_to_explicit(ctx, homes):
    seed(ctx)
    assert homes.load(ctx, "a@test") == ctx.snapshot_get("claude", "a@test")
    assert homes.load(ctx, "missing@test") is None
    assert homes.config_path(ctx._claude_real) == ctx._claude_real / ".claude.json"


def test_missing_identity_is_not_repaired_from_stock_by_an_ordinary_merge(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(home / ".claude.json", {"theme": "dark"})
    homes.merge_config(ctx, "a@test")
    assert "oauthAccount" not in json.loads((home / ".claude.json").read_text())
    assert homes.load(ctx, "a@test") == ctx.claude_cred("a@test").get_live()


def test_default_ensure_does_not_promote_real_files(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(home / "new-shared", {"seat": True})
    homes.ensure_home(ctx, "a@test")
    assert not (ctx._claude_real / "new-shared").exists()
    assert not (home / "new-shared").is_symlink()


def test_custom_real_home_is_the_exact_sharing_source(ctx, homes, tmp_path):
    seed(ctx)
    custom = tmp_path / "custom-real"
    ctx.claude_real = custom
    assert ctx._claude_real == custom
    home = homes.ensure_home(ctx, "a@test")
    assert home.parent == ctx.data_dir / "clh"
    assert (home / "projects").resolve() == (custom / "projects").resolve()
    ctx.claude_real = None
    from acctsw import paths
    assert ctx._claude_real == paths.CLAUDE_CONFIG_DIR


@pytest.mark.parametrize("explicit,marker,secure", [
    (False, None, None), (True, None, None), (True, "0", None),
    (False, "1", None), (True, None, "/secure-override"),
])
def test_default_context_uses_the_official_stock_item_and_injected_backend(ctx, homes, monkeypatch,
                                                                          explicit, marker, secure):
    from acctsw import context, paths
    monkeypatch.setattr(context, "SecurityKeychain", lambda: ctx.keychain)
    monkeypatch.setattr(context, "which_tool", lambda _: None)
    monkeypatch.setattr(paths, "CLAUDE_CONFIG_DIR", ctx.claude_real)
    for name in ("CLAUDE_CONFIG_DIR", "ACCTSW_CLAUDE_SHARED_EXPLICIT", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    if explicit:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ctx.claude_real))
    if marker is not None:
        monkeypatch.setenv("ACCTSW_CLAUDE_SHARED_EXPLICIT", marker)
    if secure is not None:
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", secure)
    made = context.Context.default()
    is_explicit = marker == "1" if marker is not None else explicit
    assert made.keychain is ctx.keychain
    assert made.claude_real == ctx.claude_real
    assert made.claude_config_explicit is is_explicit
    expected = secure if secure is not None else str(ctx.claude_real) if is_explicit else None
    assert made.cred["claude"].service == homes.service_name(expected)


def test_switch_migrates_a_snapshot_even_without_stock_mirroring(ctx, homes):
    state = seed(ctx)
    switch(ctx, state, "claude", "a@test", sync=False, mirror=False)
    assert ctx.claude_home("a@test").is_dir()
    assert ctx.claude_cred("a@test").get_live() == ctx.snapshot_get("claude", "a@test")


def test_reconcile_skips_unchanged_stock_before_any_identity_command(ctx, homes, monkeypatch):
    ctx.cred["claude"].set_live("captured login")
    state = ctx.load_state()
    accounts.add(ctx, state, "claude", email="a@test")
    ctx.claude_cred("a@test").set_live("child rotation")
    monkeypatch.setattr(identity, "claude_live_identity", lambda *a, **kw:
                        pytest.fail("unchanged stock item must not be identified again"))
    assert accounts.reconcile_claude(ctx, state) is None
    assert ctx.claude_cred("a@test").get_live() == "child rotation"
    ctx.keychain.delete(ctx.cred["claude"].service, ctx.cred["claude"].account)
    assert accounts.reconcile_claude(ctx, state) is None


def test_empty_identity_blob_cannot_capture_even_if_it_names_a_known_seat(ctx, homes):
    state = seed(ctx)
    before = ctx.snapshot_get("claude", "a@test")
    answer = identity.ClaudeLiveIdentity(None, "a@test")
    assert accounts.reconcile_claude(ctx, state, live_identity=answer) is None
    assert ctx.snapshot_get("claude", "a@test") == before


def test_raw_keychain_command_addresses_the_exact_official_item(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    SecurityKeychain("/fake/security").set_raw("Claude Code-credentials-aabbccdd", "tester", '{"token":"raw"}')
    assert calls == [(["/fake/security", "add-generic-password", "-U", "-s",
                       "Claude Code-credentials-aabbccdd", "-a", "tester", "-w", '{"token":"raw"}'],
                      {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"})]


def test_baseline_symlink_cannot_inject_someone_elses_merge_history(ctx, homes):
    seed(ctx)
    real, home = ctx._claude_real, ctx.claude_home("a@test")
    put(real / ".claude.json", {"mcpServers": {"keep": {}}})
    put(real / ".acctsw-shared.json", {"mcpServers": {"keep": {}}})
    home.mkdir(parents=True)
    (home / ".acctsw-shared.json").symlink_to(real / ".acctsw-shared.json")
    homes.ensure_home(ctx, "a@test")
    assert json.loads((home / ".claude.json").read_text())["mcpServers"] == {"keep": {}}
    assert json.loads((real / ".claude.json").read_text())["mcpServers"] == {"keep": {}}


def test_matching_migration_profile_is_kept_when_config_is_parked(ctx, homes):
    # A conflict parks the path, but its local identity must survive without being invented anew.
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(home / ".claude.json", {"oauthAccount": {"emailAddress": "a@test", "accountUuid": "keep"},
                                "theme": "seat"})
    put(home / ".acctsw-shared.json", {"theme": "old"})
    put(ctx._claude_real / ".claude.json", {"theme": "stock"})
    homes.merge_config(ctx, "a@test")
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
        "emailAddress": "a@test", "accountUuid": "keep"}


def test_canonical_claude_path_rejects_managed_homes_but_honors_custom_dirs(tmp_path, monkeypatch):
    from acctsw import paths
    monkeypatch.setattr(paths, "HOME", tmp_path)
    monkeypatch.setattr(paths, "CLAUDE_HOMES", tmp_path / "store" / "clh")
    monkeypatch.setattr(paths, "CODEX_HOMES", tmp_path / "store" / "ch")
    monkeypatch.delenv("ACCTSW_CLAUDE_SHARED_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert paths._canonical_claude_home() == tmp_path / ".claude"
    custom = tmp_path / "custom"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert paths._canonical_claude_home() == custom
    for root in (paths.CLAUDE_HOMES, paths.CODEX_HOMES):
        private = root / "seat"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(private))
        assert paths._canonical_claude_home() == tmp_path / ".claude"
        monkeypatch.setenv("ACCTSW_CLAUDE_SHARED_DIR", str(custom))
        assert paths._canonical_claude_home() == custom
        monkeypatch.delenv("ACCTSW_CLAUDE_SHARED_DIR")
        private.mkdir(parents=True)
        alias = tmp_path / (root.name + "-alias")
        alias.symlink_to(private, target_is_directory=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alias))
        assert paths._canonical_claude_home() == tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/config/")
    assert paths._canonical_claude_home() == Path("relative/config")


def test_stock_config_hash_and_status_preserve_trailing_slash(ctx, homes, monkeypatch):
    from acctsw import context, paths
    raw = str(ctx.claude_real) + "/"
    monkeypatch.setattr(context, "SecurityKeychain", lambda: ctx.keychain)
    monkeypatch.setattr(context, "which_tool", lambda _: "/fake/claude")
    monkeypatch.setattr(paths, "CLAUDE_CONFIG_DIR", ctx.claude_real)
    monkeypatch.delenv("ACCTSW_CLAUDE_SHARED_EXPLICIT", raising=False)
    monkeypatch.delenv("ACCTSW_CLAUDE_STOCK_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", raw)
    made = context.Context.default()
    assert made.cred["claude"].service == homes.service_name(raw)
    assert made._claude_config_value == raw
    blob = make_claude_blob()
    made.cred["claude"].set_live(blob)
    result = identity.claude_live_identity(made, get=lambda *a:
                                          (200, '{"account":{"email":"a@test"}}'))
    assert result == identity.ClaudeLiveIdentity(blob, "a@test")


@pytest.mark.parametrize("explicit", [False, True])
def test_stock_identity_environment_is_distinct_from_home_identity(ctx, homes, monkeypatch, explicit):
    seed(ctx)
    homes.ensure_home(ctx, "a@test")
    ctx.claude_bin = "/fake/claude"
    ctx.claude_config_explicit = explicit
    ctx.cred["claude"].set_live(make_claude_blob().replace('"x"', '"stock"'))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/inherited/private/home")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/stock-storage")
    seen = []
    def get(url, headers, timeout):
        seen.append(headers["Authorization"])
        return 200, '{"account":{"email":"a@test"}}'
    monkeypatch.setattr(usage, "_default_get", get)
    monkeypatch.setattr(usage, "claude_user_agent", lambda *_: "fake")
    assert identity.live_email(ctx, "claude") == "a@test"
    identity.claude_live_identity(ctx, email="a@test")
    assert seen == ["Bearer stock", "Bearer a"]


@pytest.mark.parametrize("answer", [[], None, {"account":{"email":12}},
                                    {"account":{"email":[]}}, {"account":{"email":""}}])
def test_profile_requires_an_object_and_a_nonempty_string_email(answer):
    assert identity.claude_profile_email(make_claude_blob(),
        get=lambda *a: (200, json.dumps(answer))) is None


def test_direct_profile_discards_config_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/original")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/original-secure")
    def get(url, headers, timeout):
        assert url == "https://api.anthropic.com/api/oauth/profile"
        assert headers["Authorization"] == "Bearer x" and timeout == 10.0
        return 200, '{"account":{"email":"a@test"}}'
    assert identity.claude_profile_email(make_claude_blob(), get=get) == "a@test"


@pytest.mark.parametrize("same", [True, False])
def test_bridge_does_not_probe_unchanged_stock_after_a_home_refresh(ctx, monkeypatch, same):
    from acctsw import bridge
    ctx.cred["claude"].set_live("stock login")
    state = ctx.load_state()
    accounts.add(ctx, state, "claude", email="a@test")
    if not same:
        ctx.snapshot_set("claude", "a@test", "home rotation")
    from acctsw import claudesync
    claudesync.converge(ctx)
    monkeypatch.setattr(identity, "claude_live_identity", lambda *a, **kw:
                        pytest.fail("unchanged stock must not stall observer polling"))
    monkeypatch.setattr(usage, "refresh_live", lambda *a, **kw: {"claude": {"a@test": "cached"}})
    result = bridge.handle(ctx, {"action": "usage", "tool": "claude"})
    assert result["ok"], result


def test_claude_promotion_does_not_inherit_codex_specific_exclusions(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    put(home / "auth.json", {"plugin": "shared data"})
    put(home / "packages" / "metadata", {"plugin": "shared data"})
    homes.ensure_home(ctx, "a@test", promote=True)
    assert (home / "auth.json").is_symlink()
    assert (home / "packages").is_symlink()
    assert (ctx._claude_real / "packages" / "metadata").is_file()


def test_default_stock_path_has_no_explicit_config_value(ctx):
    ctx.claude_config_explicit = False
    assert ctx._claude_config_value is None


def test_nested_context_retains_the_original_stock_path_string(ctx, homes, monkeypatch):
    from acctsw import context, paths
    monkeypatch.setattr(context, "SecurityKeychain", lambda: ctx.keychain)
    monkeypatch.setattr(context, "which_tool", lambda _: None)
    monkeypatch.setattr(paths, "CLAUDE_CONFIG_DIR", ctx.claude_real)
    original = str(ctx.claude_real) + "/"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ctx.claude_home("a@test")))
    monkeypatch.setenv("ACCTSW_CLAUDE_SHARED_EXPLICIT", "1")
    monkeypatch.setenv("ACCTSW_CLAUDE_STOCK_CONFIG", original)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    nested = context.Context.default()
    assert nested._claude_config_value == original
    assert nested.cred["claude"].service == homes.service_name(original)


def test_exported_mcp_and_project_config_uses_private_file_permissions(ctx, homes):
    seed(ctx)
    home = homes.ensure_home(ctx, "a@test")
    config = json.loads((home / ".claude.json").read_text())
    config["mcpServers"] = {"new": {"command": "run"}}
    put(home / ".claude.json", config)
    homes.merge_config(ctx, "a@test")
    assert (ctx._claude_real / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_premigration_usage_uses_its_named_snapshot_instead_of_unrelated_stock(ctx):
    state = seed(ctx)
    ctx.cred["claude"].set_live("other account's tokens")
    expected = ctx.snapshot_get("claude", "a@test")
    assert usage._seat_blob(ctx, state, "claude", "a@test") == expected
    assert usage._detached_blob(ctx, state, "claude", "a@test", active=True) == expected
