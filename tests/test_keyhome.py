"""Preparation keeps config private and Codex socket paths short.

These tests exercise the stored-seat/Keychain boundary with fake validation, never a live harness
or network. These checks cannot establish post-session containment: test_keyhome_live.py
exercises the real harness. Parsing TOML catches escaping and table-scope mistakes.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import tomllib

import pytest

from acctsw import codexhome, keyhome, keyseats, paths as P, providers
from acctsw.context import Context
from acctsw.keychain import KeychainError

SECRET = 'sk-private-"\\-never-on-disk-9876'


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Key-home preparation attempted network or process launch")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def add(ctx, provider="langdock", *, harness="codex", model="model-id", base_url=None):
    p = providers.get_provider(provider, base_url=base_url)
    if harness == "claude" and provider == "langdock":
        p = replace(p, wire_api="messages", responses_support="unsupported")
    elif provider == "openai_compatible":
        p = replace(p, responses_support="verified")
    return keyseats.add(ctx, p, SECRET, label="Work", model=model,
                        get=lambda *args: (200, "{}"))


def config(runtime):
    return tomllib.loads((runtime.home / "config.toml").read_text(encoding="utf-8"))


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_codex_provider_and_all_egress_suppression(ctx):
    seat = add(ctx)
    runtime = keyhome.prepare(ctx, seat["id"])
    assert config(runtime) == {
        "model_provider": "langdock_eu", "model": "model-id", "web_search": "disabled",
        "check_for_update_on_startup": False,
        "shell_environment_policy": {"inherit": "all", "ignore_default_excludes": False,
                                     "exclude": ["LANGDOCK_API_KEY"]},
        "model_providers": {"langdock_eu": {
            "name": "Langdock EU", "base_url": "https://api.langdock.com/openai/eu/v1",
            "wire_api": "responses", "env_key": "LANGDOCK_API_KEY",
            "requires_openai_auth": False, "supports_websockets": False,
            "request_max_retries": 0, "stream_max_retries": 0}},
        "analytics": {"enabled": False}, "feedback": {"enabled": False},
        "otel": {"exporter": "none", "trace_exporter": "none", "metrics_exporter": "none",
                 "log_user_prompt": False},
        "features": {"apps": False, "plugins": False, "shell_snapshot": False}}
    assert runtime.home == ctx.codex_home(f"key:codex:{seat['id']}")
    assert runtime.home.stat().st_mode & 0o777 == 0o700
    assert (runtime.home / "config.toml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider,harness", [("langdock", "codex"), ("langdock", "claude"),
                                             ("anthropic", "claude")])
def test_preparation_does_not_write_or_log_secret(ctx, provider, harness, caplog):
    seat = add(ctx, provider, harness=harness)
    runtime = keyhome.prepare(ctx, seat["id"])
    env = keyhome.build_env(ctx, runtime, env={})
    assert env[runtime.env_key] == SECRET
    assert list(env.values()).count(SECRET) == 1
    for text in (repr(runtime), repr(env), str(env), repr([env, runtime]), caplog.text):
        assert SECRET not in text
    assert not any(SECRET.encode() in data for data in tree(runtime.home).values())
    assert not any(SECRET.encode() in data for data in tree(ctx.data_dir).values())
    assert not any(p.is_symlink() for p in runtime.home.rglob("*"))


@pytest.mark.parametrize("provider", ["openai", "langdock", "openrouter", "openai_compatible"])
def test_codex_credential_excluded_from_shell_and_snapshots(ctx, provider):
    runtime = keyhome.prepare(ctx, add(ctx, provider, base_url=(
        "https://gateway.example/v1" if provider == "openai_compatible" else None))["id"])
    data = config(runtime)
    policy = data["shell_environment_policy"]
    assert runtime.env_key in policy["exclude"]
    assert policy["ignore_default_excludes"] is False
    assert runtime.env_key not in policy.get("set", {})
    assert data["features"]["shell_snapshot"] is False
    # The harness still receives the key for provider authentication.
    assert keyhome.build_env(ctx, runtime, env={})[runtime.env_key] == SECRET


@pytest.mark.parametrize("name", ["OpenAI", "openai", " OpenAI "])
def test_refuse_custom_provider_named_openai(ctx, monkeypatch, name):
    seat = add(ctx, "openrouter")
    monkeypatch.setitem(providers.PROVIDERS, "openrouter",
                        replace(providers.PROVIDERS["openrouter"], display_name=name))
    with pytest.raises(ValueError, match="must not be named"):
        keyhome.prepare(ctx, seat["id"])
    assert not ctx._homes_root.exists()


def test_direct_openai_also_uses_a_custom_identity(ctx):
    runtime = keyhome.prepare(ctx, add(ctx, "openai")["id"])
    data = config(runtime)
    assert data["model_provider"] == "openai_api"
    assert data["model_providers"]["openai_api"]["name"] == "OpenAI API"
    assert data["model_providers"]["openai_api"]["env_key"] == "OPENAI_API_KEY"


@pytest.mark.parametrize("value", ['quoted"\\path', '"\ninjected = true\n[evil]\nx = "',
                                  "control\x00\x01\t\r\n\x7f and Unicode 🦉"])
def test_model_values_cannot_inject_toml(ctx, value):
    runtime = keyhome.prepare(ctx, add(ctx, model=value)["id"])
    data = config(runtime)
    assert data["model"] == value
    assert set(data) == {"model", "model_provider", "web_search", "check_for_update_on_startup",
                         "model_providers", "analytics", "feedback", "otel", "features",
                         "shell_environment_policy"}


@pytest.mark.parametrize("base", ['https://gateway.example/v1/"\\injected = true',
                                  'https://gateway.example/v1/"\ninjected = true\n[evil]\nx = "'])
def test_custom_base_url_quotes_backslashes_cannot_inject_toml(ctx, base):
    runtime = keyhome.prepare(ctx, add(ctx, "openai_compatible", base_url=base)["id"])
    data = config(runtime)
    block = data["model_providers"][runtime.model_provider]
    assert block["base_url"] == base
    assert set(block) == {"name", "base_url", "wire_api", "env_key", "requires_openai_auth",
                          "request_max_retries", "stream_max_retries"}
    assert "evil" not in data and "injected" not in data


def test_provider_name_and_table_key_are_escaped_too(ctx, monkeypatch):
    seat = add(ctx, "openrouter")
    value = 'custom"\\\n[evil]\nx = "🦉'
    monkeypatch.setitem(providers.PROVIDERS, "openrouter",
                        replace(providers.PROVIDERS["openrouter"], id=value, display_name=value))
    data = config(keyhome.prepare(ctx, seat["id"]))
    assert data["model_provider"] == value
    assert list(data["model_providers"]) == [value]
    assert data["model_providers"][value]["name"] == value
    assert "evil" not in data


@pytest.mark.parametrize("provider,harness", [("langdock", "codex"), ("langdock", "claude")])
def test_preparation_converges_and_never_touches_user_config(ctx, monkeypatch, provider, harness):
    for home in (ctx._codex_real, P.CODEX_HOME, P.CLAUDE_CONFIG_DIR):
        home.mkdir(parents=True)
        (home / "config.toml").write_text('model = "personal"\n')
        (home / "settings.json").write_text('{"permissions":{"deny":["WebFetch"]}}\n')
        (home / "auth.json").write_text('{"personal":"credential"}')
    homes = (ctx._codex_real, P.CODEX_HOME, P.CLAUDE_CONFIG_DIR)
    before = [tree(home) for home in homes]
    monkeypatch.setenv("CODEX_HOME", str(ctx._codex_real))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(P.CLAUDE_CONFIG_DIR))
    seat = add(ctx, provider, harness=harness)
    first = keyhome.prepare(ctx, seat["id"])
    files = tree(first.home)
    second = keyhome.prepare(ctx, seat["id"])
    assert first == second
    assert tree(second.home) == files
    keyhome.build_env(ctx, second)
    assert [tree(home) for home in homes] == before
    assert os.environ["CODEX_HOME"] == str(ctx._codex_real)
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(P.CLAUDE_CONFIG_DIR)


@pytest.mark.parametrize("provider,base", [("langdock", "https://api.langdock.com/anthropic/eu"),
                                         ("anthropic", "https://api.anthropic.com")])
def test_claude_env_and_private_webfetch_settings(ctx, provider, base):
    runtime = keyhome.prepare(ctx, add(ctx, provider, harness="claude")["id"])
    inherited = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "wrong-key",
                 "ANTHROPIC_AUTH_TOKEN": "wrong-token", "ANTHROPIC_BASE_URL": "wrong-host",
                 "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "0",
                 "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_OAUTH_TOKEN": "subscription"}
    before = inherited.copy()
    env = keyhome.build_env(ctx, runtime, env=inherited)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env == {"PATH": "/usr/bin", "DO_NOT_TRACK": "1", "ANTHROPIC_BASE_URL": base,
                   "ANTHROPIC_AUTH_TOKEN": SECRET, "ANTHROPIC_MODEL": "model-id",
                   "CLAUDE_CONFIG_DIR": str(runtime.home),
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
                   "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                   "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1"}
    assert inherited == before
    assert json.loads((runtime.home / "settings.json").read_text()) == {"skipWebFetchPreflight": True}


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_both_harness_envs_scrub_frozen_python_and_leave_parent_unchanged(ctx, monkeypatch, harness):
    runtime = keyhome.prepare(ctx, add(ctx, harness=harness)["id"])
    names = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")
    for name in names:
        monkeypatch.setenv(name, "/frozen/python")
    env = keyhome.build_env(ctx, runtime)
    for name in names:
        assert name not in env
        assert os.environ[name] == "/frozen/python"
    assert env["DO_NOT_TRACK"] == "1"
    if harness == "codex":
        assert env["CODEX_HOME"] == str(runtime.home)


def test_key_home_fits_socket_budget_with_full_uuid(ctx):
    # Check the production-depth synthetic root without writing under /Users.
    seat = add(ctx)
    root = Path("/Users/" + "u" * 23) / ".account-switcher" / P.CODEX_HOMES.name
    home = codexhome.home_dir(f"key:codex:{seat['id']}", root)
    assert len(str(home).encode()) <= codexhome.MAX_HOME_LEN
    assert codexhome.daemon_socket_fits(home)
    # An actually prepared home, too: pytest's own temp path is too deep on macOS.
    with tempfile.TemporaryDirectory(prefix="kh-", dir="/tmp") as temp:
        short = Context.for_test(Path(temp))
        runtime = keyhome.prepare(short, add(short)["id"])
        assert codexhome.daemon_socket_fits(runtime.home)


def test_different_seats_and_harnesses_have_separate_homes(ctx):
    ids = [add(ctx)["id"], add(ctx)["id"], add(ctx, harness="claude")["id"]]
    assert len({keyhome.prepare(ctx, id).home for id in ids}) == 3


def test_environment_reads_rotated_key_without_writing_anything(ctx):
    runtime = keyhome.prepare(ctx, add(ctx)["id"])
    before = tree(ctx.data_dir)
    fresh = "rotated-private-key-12345"
    ctx.keychain.set(ctx.keychain_service, ctx.snapshot_key("key", runtime.seat_id), fresh)
    env = keyhome.build_env(ctx, runtime, env={})
    assert env[runtime.env_key] == fresh
    assert fresh not in repr(env)
    assert tree(ctx.data_dir) == before


def test_keychain_failures_do_not_echo_secret(ctx, monkeypatch):
    seat = add(ctx)
    def fail(*args, **kwargs):
        raise KeychainError(SECRET)
    monkeypatch.setattr(keyseats, "get_secret", fail)
    with pytest.raises(KeychainError) as error:
        keyhome.prepare(ctx, seat["id"])
    assert SECRET not in str(error.value)
    assert not ctx._homes_root.exists()


def test_missing_seat_and_secret_fail_before_config_writes(ctx):
    with pytest.raises(ValueError, match="Unknown key seat"):
        keyhome.prepare(ctx, "missing")
    seat = add(ctx)
    ctx.keychain.delete(ctx.keychain_service, ctx.snapshot_key("key", seat["id"]))
    with pytest.raises(KeychainError, match="missing"):
        keyhome.prepare(ctx, seat["id"])
    assert not ctx._homes_root.exists()


def test_contaminated_metadata_never_persists_a_secret(ctx):
    seat = add(ctx)
    state = ctx.load_state()
    state.data["keys"][seat["id"]]["model"] = SECRET
    state.save()
    with pytest.raises(ValueError, match="must not appear"):
        keyhome.prepare(ctx, seat["id"])
    assert not ctx._homes_root.exists()


@pytest.mark.parametrize("link_home", [True, False])
def test_redirected_home_or_config_cannot_modify_user_files(ctx, link_home):
    seat = add(ctx)
    home = ctx.codex_home(f"key:codex:{seat['id']}")
    ctx._codex_real.mkdir()
    target = ctx._codex_real / "config.toml"
    target.write_text("personal settings")
    home.parent.mkdir(parents=True)
    if link_home:
        home.symlink_to(ctx._codex_real)
    else:
        home.mkdir()
        (home / "config.toml").symlink_to(target)
    with pytest.raises(ValueError, match="private directory|shared links"):
        keyhome.prepare(ctx, seat["id"])
    assert target.read_text() == "personal settings"
