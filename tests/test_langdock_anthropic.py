"""Langdock's two routes share credentials, but never catalogs or harness homes.

Catalog fixtures are assumptions, not recordings from the Anthropic route.
"""
import io
import json
import socket
import subprocess

import pytest

from acctsw import bridge, cli, codexhome, keyhome, keyprove, keyseats, launcher, pricing, providers

SECRET = "sk-langdock-test-only-never-on-disk-1842"
PROVIDER = "langdock_anthropic"
CATALOG = json.dumps({"data": [{"id": "claude-test", "display_name": "Claude test"}]})


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Langdock route tests must not use a real process or network")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def add(ctx, provider=PROVIDER, region="eu"):
    return keyseats.add(ctx, providers.get_provider(provider, region=region), SECRET,
                        label=provider, model="claude-test" if provider == PROVIDER else "gpt-test",
                        get=lambda *_: (200, CATALOG))


def test_routes_pair_to_their_harness_and_reuse_anthropic_headers():
    claude = providers.get_provider(PROVIDER)
    codex = providers.get_provider("langdock")
    assert keyseats.harness_for(claude) == "claude"
    assert keyseats.harness_for(codex) == "codex"
    assert claude.headers(SECRET) == providers.get_provider("anthropic").headers(SECRET) == {
        "x-api-key": SECRET, "anthropic-version": "2023-06-01"}
    assert "Authorization" not in claude.headers(SECRET)
    assert codex.headers(SECRET) == {"Authorization": "Bearer " + SECRET}


@pytest.mark.parametrize("region", ["eu", "us", "global"])
def test_regions_reach_validation_catalog_and_claude_runtime(ctx, region):
    provider = providers.get_provider(PROVIDER, region=region)
    base = f"https://api.langdock.com/anthropic/{region}"
    assert provider.base_url == base
    assert provider.models_endpoint == provider.validation_endpoint == base + "/v1/models"
    calls = []

    def get(url, headers, timeout):
        calls.append(url)
        assert headers == {"x-api-key": SECRET, "anthropic-version": "2023-06-01"}
        return 200, CATALOG

    seat = keyseats.add(ctx, provider, SECRET, label="Claude work", model="claude-test", get=get)
    assert seat["harness"] == "claude" and seat["region"] == region
    assert seat["last_validation"]["operation_permitted"]
    catalog = pricing.fetch_catalog(provider, SECRET, get=get, cache_path=ctx.data_dir / "prices.json")
    assert calls == [base + "/v1/models"] * 2
    model, = catalog.models
    assert model.id == "claude-test" and model.wire_api == "messages"
    assert model.display_name == "Claude test"
    assert model.rate("input") is None and model.context_window is None

    runtime = keyhome.prepare(ctx, seat["id"])
    inherited = {"ANTHROPIC_API_KEY": "wrong", "ANTHROPIC_AUTH_TOKEN": "wrong",
                 "ANTHROPIC_BASE_URL": "wrong"}
    env = keyhome.build_env(ctx, runtime, env=inherited)
    assert runtime.base_url == env["ANTHROPIC_BASE_URL"] == base
    assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert "ANTHROPIC_API_KEY" not in env
    assert inherited["ANTHROPIC_API_KEY"] == "wrong"
    for name in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY",
                 "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER"):
        assert env[name] == "1"
    assert json.loads((runtime.home / "settings.json").read_text()) == {"skipWebFetchPreflight": True}
    assert SECRET not in repr(runtime) + repr(env)
    for path in ctx.data_dir.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_invalid_region_is_rejected():
    with pytest.raises(ValueError, match="eu, us or global"):
        providers.get_provider(PROVIDER, region="moon")


@pytest.mark.parametrize("body", ["{}", "[]", "null", '{"models":[]}', '{"data":{}}',
                                  '{"data":[null,42,{}, {"id":42}]}', '{"data":[]}'])
def test_unverified_catalog_shape_surfaces_no_models(ctx, monkeypatch, body):
    assert pricing.parse_catalog(providers.get_provider(PROVIDER), body) == []
    monkeypatch.setattr(pricing, "_default_get", lambda *_: (200, body))
    result = bridge.key_action(ctx, {"action": "models_list", "provider": PROVIDER, "secret": SECRET})
    assert result["models"] == [] and result["ok"] is False
    assert "no models" in result["error"]


@pytest.mark.parametrize("provider", [PROVIDER, "anthropic"])
def test_proof_refuses_claude_before_keychain_home_or_process(ctx, monkeypatch, capsys, provider):
    seat = keyseats.add(ctx, providers.get_provider(provider), SECRET, label="Claude",
                        model="claude-test", get=lambda *_: (200, CATALOG))
    before = ctx.load_state().data

    def forbidden(*args, **kwargs):
        pytest.fail("Refused proof must not read credentials, prepare a home or run a harness")

    monkeypatch.setattr(ctx.keychain, "get", forbidden)
    monkeypatch.setattr(keyhome, "prepare", forbidden)
    monkeypatch.setattr(keyprove, "_run", forbidden)
    proof = keyprove.prove(ctx, seat["id"])
    assert proof["outcome"] == "refused" and proof["error"] == "unsupported_harness"
    assert "codex app-server" in proof["reason"] and "Messages" in proof["reason"]
    assert "checked_at" not in proof
    result = bridge.key_action(ctx, {"action": "key_prove", "id": seat["id"]})
    assert result["ok"] is False and result["proof"] == proof
    assert "codex app-server" in result["error"] and "Messages" in result["error"]
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
    assert cli.main(["keys", "prove", seat["id"]]) == cli.EXIT_ERR
    assert "codex app-server" in capsys.readouterr().err
    assert cli.main(["keys", "prove", seat["id"], "--json"]) == cli.EXIT_ERR
    assert json.loads(capsys.readouterr().out) == result
    assert ctx.load_state().data == before
    assert not ctx._homes_root.exists()


def test_same_credential_has_distinct_seats_and_harness_namespaced_homes(ctx):
    codex, claude = add(ctx, "langdock"), add(ctx)
    assert codex["id"] != claude["id"]
    assert keyseats.get_secret(ctx, codex["id"], harness="codex") == SECRET
    assert keyseats.get_secret(ctx, claude["id"], harness="claude") == SECRET
    runtimes = [keyhome.prepare(ctx, seat["id"]) for seat in (codex, claude)]
    assert runtimes[0].home != runtimes[1].home
    # Even an identical key id cannot alias across harnesses in the home layout.
    same_id = codex["id"]
    homes = {codexhome.home_dir(codexhome.key_home_identity(harness, same_id), ctx._homes_root)
             for harness in ("codex", "claude")}
    assert len(homes) == 2


def test_claude_launch_keeps_secret_out_of_argv_and_disk(ctx, monkeypatch):
    seat = add(ctx, region="global")
    state = ctx.load_state()
    state.set_setting("key_fallback", True)
    state.set_setting("confirm_key_switch", False)
    state.save()
    calls = []

    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        assert SECRET not in repr(argv)
        assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
        assert env["ANTHROPIC_BASE_URL"] == "https://api.langdock.com/anthropic/global"
        assert "ANTHROPIC_API_KEY" not in env
        assert argv[argv.index("--model") + 1] == "claude-test"
        return 0

    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-wrong-key")
    monkeypatch.setattr(launcher.session_mod, "_proc_start", lambda *_: "test process")
    assert launcher.run(ctx, "claude", [], key=seat["id"], spawn=spawn,
                        price_get=lambda *_: (200, CATALOG), notify=lambda *_: None) == 0
    assert len(calls) == 1
    for path in ctx.data_dir.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_cli_add_accepts_anthropic_route_and_region_via_stdin(ctx, monkeypatch, capsys):
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SECRET + "\n"))
    monkeypatch.setattr(pricing, "_default_get", lambda *_: (200, CATALOG))
    argv = ["keys", "add", PROVIDER, "--region", "us", "--label", "Claude work",
            "--model", "claude-test", "--json"]
    assert cli.main(argv) == cli.EXIT_OK
    output = capsys.readouterr().out
    seat = json.loads(output)["seat"]
    assert seat["harness"] == "claude" and seat["region"] == "us"
    assert SECRET not in output + repr(argv)
