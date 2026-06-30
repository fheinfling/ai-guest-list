"""Tests for the Headroom integration (global app-managed mode).

Routing no longer goes through `headroom install apply` (its launchd deploy is broken). We run the
proxy ourselves (start_proxy) and hand-write the provider
routing (_route_all); these tests cover that path plus the snapshot/restore + heal safety nets."""
import json
import os

import pytest

from acctsw import accounts as acct
from acctsw import bridge
from acctsw import headroom
from acctsw import launcher as L
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get

_REAL_PUSH = headroom.push_runtime_knobs  # captured before the autouse no-op patch below


@pytest.fixture(autouse=True)
def _no_real_runtime_push(monkeypatch):
    """Keep enable/heal tests hermetic: stub the loopback /admin/runtime-env push (real one is
    exercised directly in test_push_runtime_knobs_* via the captured _REAL_PUSH)."""
    monkeypatch.setattr(headroom, "push_runtime_knobs", lambda *a, **k: True)


def _two_codex(ctx):
    for em in ("a@x.com", "b@x.com"):
        ctx.cred["codex"].set_live(make_codex_blob(em))
        acct.add(ctx, ctx.load_state(), "codex", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "codex", "a@x.com")


class _Spawn:
    def __init__(self, status=0):
        self.calls = []
        self.status = status
    def __call__(self, argv, on_output):
        self.calls.append(list(argv))
        on_output(b"done\n")
        return self.status


def test_launcher_runs_plain_under_global_headroom(ctx, tmp_path, monkeypatch):
    """Headroom is global/app-managed now → cx/cl must NOT per-session-wrap (no double-route)."""
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    # keep the launcher's self-heal hermetic: no live proxy, and don't read/touch the real configs
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(headroom, "headroom_path", lambda: None)
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: False)
    spawn = _Spawn()
    L.run(ctx, "codex", ["--foo"], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][1:2] != ["wrap"]          # not wrapped
    assert "headroom" not in " ".join(spawn.calls[0][:2])


# --- helpers -----------------------------------------------------------------------------------

def _codex_cfg(tmp_path, monkeypatch, content='model = "gpt-5.5"\n'):
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    (tmp_path / "codex").mkdir(); (tmp_path / "claude").mkdir()
    cfg = tmp_path / "codex" / "config.toml"; cfg.write_text(content)
    return cfg


def _patch_proxy(monkeypatch, *, ready=True, start=True):
    """Stub the proxy lifecycle so enable/disable/heal tests never spawn a real proxy. Returns a dict
    that counts start/stop calls."""
    calls = {"start": 0, "stop": 0}
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: ready)

    def _start(store=None, **k):
        calls["start"] += 1
        return start

    def _stop(store=None, **k):
        calls["stop"] += 1

    monkeypatch.setattr(headroom, "start_proxy", _start)
    monkeypatch.setattr(headroom, "stop_proxy", _stop)
    return calls


# --- routing writers (hand-rolled provider config) ---------------------------------------------

def test_route_codex_idempotent_preserves_body_and_orders_keys(tmp_path, monkeypatch):
    """Re-applying must not duplicate our block, must keep the user's body, and must keep the
    top-level model_provider key ABOVE any [table] header (TOML scoping — headroom #260)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "gpt-5.5"\n[profiles.x]\nfoo = 1\n')
    headroom._route_codex()
    headroom._route_codex()                               # re-apply
    body = cfg.read_text()
    assert body.count('[model_providers.headroom]') == 1          # no dup table
    assert body.count('model_provider = "headroom"') == 1        # no dup key
    assert 'model = "gpt-5.5"' in body and 'foo = 1' in body     # user body preserved
    assert body.index('model_provider') < body.index('[profiles.x]')
    assert body.index('model_provider') < body.index('[model_providers.headroom]')
    import tomllib
    tomllib.loads(body)                                          # routed config is valid TOML


def test_unroute_codex_restores_original_body(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "gpt-5.5"\n')
    headroom._route_codex()
    headroom._unroute_codex()
    assert 'headroom' not in cfg.read_text()
    assert cfg.read_text().strip() == 'model = "gpt-5.5"'


def test_route_codex_requires_openai_auth_only_for_chatgpt(tmp_path, monkeypatch):
    """requires_openai_auth restores the account menu but forces an OAuth login (headroom #406), so
    it must appear ONLY for ChatGPT-OAuth users, never API-key users."""
    cfg = _codex_cfg(tmp_path, monkeypatch, '')
    from acctsw import paths as P
    (P.CODEX_HOME / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    headroom._route_codex()
    assert 'requires_openai_auth = true' in cfg.read_text()
    headroom._unroute_codex()
    (P.CODEX_HOME / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    headroom._route_codex()
    assert 'requires_openai_auth' not in cfg.read_text()


def test_route_unroute_claude_preserves_user_env(tmp_path, monkeypatch):
    _codex_cfg(tmp_path, monkeypatch)
    from acctsw import paths as P
    settings = P.CLAUDE_CONFIG_DIR / "settings.json"
    settings.write_text(json.dumps({"env": {"FOO": "bar"}, "other": 1}))
    headroom._route_claude()
    data = json.loads(settings.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{headroom.PROXY_PORT}"
    assert data["env"]["ENABLE_TOOL_SEARCH"] == "true"   # GH#746
    assert data["env"]["FOO"] == "bar" and data["other"] == 1
    headroom._unroute_claude()
    data2 = json.loads(settings.read_text())
    assert "ANTHROPIC_BASE_URL" not in data2["env"]
    assert data2["env"]["FOO"] == "bar" and data2["other"] == 1  # user keys untouched


def test_unroute_claude_leaves_foreign_base_url_alone(tmp_path, monkeypatch):
    """If ANTHROPIC_BASE_URL points somewhere that ISN'T our proxy, it's the user's — don't strip."""
    _codex_cfg(tmp_path, monkeypatch)
    from acctsw import paths as P
    settings = P.CLAUDE_CONFIG_DIR / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}))
    headroom._unroute_claude()
    assert json.loads(settings.read_text())["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


# --- proxy lifecycle ---------------------------------------------------------------------------

def test_start_proxy_passes_shaper_env_and_tracks_pid(tmp_path, monkeypatch):
    """start_proxy must launch `headroom proxy` with the shaper/holdout env (we own the child env now,
    no env-less plist) and record the pid so stop_proxy can kill it later."""
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "_port_busy", lambda *a, **k: False)   # hermetic vs a real local proxy
    seen = {}

    class _Proc:
        pid = 4321

    def fake_popen(argv, **k):
        seen["argv"] = argv
        seen["env"] = k.get("env", {})
        seen["new_session"] = k.get("start_new_session")
        return _Proc()

    states = iter([False, True])   # not ready at first → starts; ready on the next poll
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: next(states, True))
    ok = headroom.start_proxy(tmp_path / "store", popen=fake_popen, sleep=lambda *_: None)
    assert ok
    assert seen["argv"][1] == "proxy" and "--port" in seen["argv"]
    assert seen["new_session"] is True
    assert seen["env"]["HEADROOM_OUTPUT_SHAPER"] == "1"
    assert float(seen["env"]["HEADROOM_OUTPUT_HOLDOUT"]) > 0
    assert seen["env"]["HEADROOM_TELEMETRY"] == "off"     # hardening still applied
    assert (tmp_path / "store" / "headroom-proxy.pid").read_text() == "4321"


def test_start_proxy_returns_true_immediately_if_already_serving(tmp_path, monkeypatch):
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: True)
    monkeypatch.setattr(headroom, "_port_listener_pid", lambda *a, **k: 1234)
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: True)   # it's genuinely our proxy
    called = []
    assert headroom.start_proxy(tmp_path / "store", popen=lambda *a, **k: called.append(1)) is True
    assert called == []                                   # idempotent: never spawned a second proxy


def test_start_proxy_adopts_foreign_pid_so_it_can_be_stopped(tmp_path, monkeypatch):
    """If a proxy we didn't start (e.g. one that outlived a crash) already serves the port, start_proxy
    must record its PID so a later stop_proxy/heal can still kill it — never leave it untracked."""
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: True)
    monkeypatch.setattr(headroom, "_port_listener_pid", lambda *a, **k: 9999)
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: True)   # verified Headroom proxy
    assert headroom.start_proxy(tmp_path / "store", popen=lambda *a, **k: None) is True
    assert headroom._proxy_pidfile(tmp_path / "store").read_text() == "9999"   # adopted, now stoppable


def test_start_proxy_refuses_foreign_ready_listener(tmp_path, monkeypatch):
    """SECURITY: a non-Headroom process that squats the port and fakes /readyz must NOT be adopted or
    routed to — else it would harvest the OAuth bearer tokens the tools send through the proxy."""
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: True)     # squatter answers /readyz
    monkeypatch.setattr(headroom, "_port_listener_pid", lambda *a, **k: 4242)
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: False)      # ...but it's NOT our proxy
    called = []
    assert headroom.start_proxy(tmp_path / "store", popen=lambda *a, **k: called.append(1)) is False
    assert called == []                                                   # never spawned/routed
    assert not headroom._proxy_pidfile(tmp_path / "store").exists()        # never adopted the squatter


def test_start_proxy_fails_fast_on_foreign_port(tmp_path, monkeypatch):
    """A non-Headroom process on the port → fail fast (no 30s poll), and never spawn our proxy."""
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: False)   # not our proxy
    monkeypatch.setattr(headroom, "_port_busy", lambda *a, **k: True)     # but port is held
    spawned = []
    ok = headroom.start_proxy(tmp_path / "store", popen=lambda *a, **k: spawned.append(1))
    assert ok is False and spawned == []


def test_start_proxy_bounded_by_deadline(tmp_path, monkeypatch):
    """Readiness polling is bounded by a wall-clock deadline, not attempts×timeout — it must give up
    (and tear down) rather than block the op_lock for minutes when the proxy never answers."""
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: False)   # never becomes ready
    monkeypatch.setattr(headroom, "_port_busy", lambda *a, **k: False)
    stopped = []
    monkeypatch.setattr(headroom, "stop_proxy", lambda *a, **k: stopped.append(1))

    class _Proc:
        pid = 5
    clock = iter([0.0, 10.0, 20.0, 31.0])                # advances past the 30s deadline
    ok = headroom.start_proxy(tmp_path / "store", popen=lambda *a, **k: _Proc(),
                              sleep=lambda *_: None, clock=lambda: next(clock))
    assert ok is False and stopped == [1]                # gave up and tore down


def test_stop_proxy_kills_pid_and_clears_file(tmp_path, monkeypatch):
    pidf = tmp_path / "store" / "headroom-proxy.pid"
    pidf.parent.mkdir(parents=True)
    pidf.write_text("4321")
    killed = []
    monkeypatch.setattr(headroom, "_pid_alive", lambda pid: pid == 4321 and not killed)
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: pid == 4321)   # identity confirmed
    headroom.stop_proxy(tmp_path / "store",
                        kill=lambda pid, sig: killed.append((pid, sig)), sleep=lambda *_: None)
    assert killed and killed[0][0] == 4321
    assert not pidf.exists()


def test_proxy_ready_parses_readyz():
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ready": true}'

    assert headroom.proxy_ready(urlopen=lambda u, timeout=None: _Resp()) is True

    class _NotReady(_Resp):
        def read(self): return b'{"ready": false}'

    assert headroom.proxy_ready(urlopen=lambda u, timeout=None: _NotReady()) is False

    def boom(u, timeout=None):
        raise OSError("connection refused")
    assert headroom.proxy_ready(urlopen=boom) is False    # best-effort: never raises


def test_global_running_reflects_readyz(monkeypatch):
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: True)
    assert headroom.global_running() is True
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: False)
    assert headroom.global_running() is False


# --- global enable/disable (app-managed) -------------------------------------------------------

def test_global_enable_snapshots_and_routes(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    ok, msg = headroom.global_enable(tmp_path / "store")
    assert ok, msg
    assert (tmp_path / "store" / "headroom-global-backup" / "manifest.json").exists()  # snapshot taken
    assert 'model_provider = "headroom"' in cfg.read_text()                            # routed


def test_global_enable_routes_codex_and_claude(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok
    from acctsw import paths as P
    codex = cfg.read_text()
    assert 'model_provider = "headroom"' in codex and 'model = "orig"' in codex
    settings = json.loads((P.CLAUDE_CONFIG_DIR / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{headroom.PROXY_PORT}"
    assert settings["env"]["ENABLE_TOOL_SEARCH"] == "true"


def test_global_enable_starts_proxy_before_routing(tmp_path, monkeypatch):
    """Ordering invariant: the proxy must be confirmed healthy BEFORE any routing is written, or a
    client could pick up the config and hit a dead port."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    order = []
    monkeypatch.setattr(headroom, "proxy_ready", lambda *a, **k: False)
    monkeypatch.setattr(headroom, "stop_proxy", lambda *a, **k: None)

    def _start(store=None, **k):
        order.append("start")
        return True

    def _route(*a, **k):
        order.append("route")
    monkeypatch.setattr(headroom, "start_proxy", _start)
    monkeypatch.setattr(headroom, "_route_all", _route)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok and order == ["start", "route"]


def test_global_enable_rolls_back_when_proxy_not_ready(tmp_path, monkeypatch):
    """Proxy never comes up healthy → full rollback (never leave a dead-proxy route)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch, start=False)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok is False
    assert cfg.read_text() == 'model = "orig"\n'                # config untouched
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_global_enable_rolls_back_when_routing_fails(tmp_path, monkeypatch):
    """Proxy healthy but writing the routing config raises → undo + restore original exactly."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(headroom, "_route_all", boom)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok is False
    assert cfg.read_text() == 'model = "orig"\n'
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_snapshot_global_is_idempotent(tmp_path, monkeypatch):
    """A 2nd enable must NOT re-snapshot a routed config (keeps the original backstop)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    headroom.snapshot_global(tmp_path / "store")
    cfg.write_text('model_provider = "headroom"\n')   # now routed
    headroom.snapshot_global(tmp_path / "store")       # idempotent: must keep the ORIGINAL
    ok, failures = headroom.restore_global(tmp_path / "store")
    assert ok and not failures
    assert cfg.read_text() == 'model = "orig"\n'


def test_global_disable_unroutes_and_keeps_user_body(tmp_path, monkeypatch):
    """Happy path: surgical unroute strips our block + stops the proxy while preserving the user's
    own config, and clears the backup on a clean removal."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    calls = _patch_proxy(monkeypatch)
    headroom.snapshot_global(tmp_path / "store")
    headroom._route_codex()                                     # additive injection (keeps body)
    assert headroom._is_injected("codex")
    ok, _ = headroom.global_disable(tmp_path / "store")
    assert ok
    assert 'model_provider = "headroom"' not in cfg.read_text()
    assert 'model = "orig"' in cfg.read_text()                  # user body preserved
    assert calls["stop"] >= 1                                   # proxy torn down
    assert not (tmp_path / "store" / "headroom-global-backup").exists()  # backup cleared on success


def test_enable_replaces_then_disable_restores_user_codex_provider(tmp_path, monkeypatch):
    """A Codex user who already set model_provider/openai_base_url: enabling must REPLACE them (valid
    TOML with a single key — not a duplicate that stops Codex launching), and disabling must restore
    their originals verbatim from the snapshot."""
    cfg = _codex_cfg(
        tmp_path, monkeypatch,
        'model_provider = "openai"\nopenai_base_url = "http://127.0.0.1:1234/v1"\nmodel = "o1"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok
    routed = cfg.read_text()
    import tomllib
    tomllib.loads(routed)                                       # valid TOML — no duplicate key
    assert routed.count("model_provider =") == 1               # ours REPLACED theirs, not appended
    assert 'model_provider = "headroom"' in routed
    ok, _ = headroom.global_disable(tmp_path / "store")
    assert ok
    assert cfg.read_text() == ('model_provider = "openai"\n'
                               'openai_base_url = "http://127.0.0.1:1234/v1"\nmodel = "o1"\n')


def test_enable_disable_restores_user_claude_env(tmp_path, monkeypatch):
    """A Claude user who already had ENABLE_TOOL_SEARCH/other env set must get their exact original
    values back after an enable/disable cycle (routing overwrites them while it's on)."""
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    from acctsw import paths as P
    settings = P.CLAUDE_CONFIG_DIR / "settings.json"
    settings.write_text(json.dumps({"env": {"ENABLE_TOOL_SEARCH": "false", "FOO": "bar"}}))
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    ok, _ = headroom.global_enable(tmp_path / "store")
    assert ok
    routed = json.loads(settings.read_text())["env"]
    assert routed["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{headroom.PROXY_PORT}"
    assert routed["ENABLE_TOOL_SEARCH"] == "true"              # forced on while routed (GH#746)
    ok, _ = headroom.global_disable(tmp_path / "store")
    assert ok
    assert json.loads(settings.read_text())["env"] == {"ENABLE_TOOL_SEARCH": "false", "FOO": "bar"}


def test_global_disable_graceful_keeps_proxy_alive(tmp_path, monkeypatch):
    """reap_proxy=False (graceful toggle-OFF): unroute the configs but DON'T stop the proxy, so a
    session that pinned the port at launch keeps working while new sessions go direct."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    calls = _patch_proxy(monkeypatch)
    headroom.snapshot_global(tmp_path / "store")
    headroom._route_codex()
    ok, _ = headroom.global_disable(tmp_path / "store", reap_proxy=False)
    assert ok
    assert 'model_provider = "headroom"' not in cfg.read_text()   # new sessions go direct
    assert 'model = "orig"' in cfg.read_text()                    # user body restored
    assert calls["stop"] == 0                                     # proxy LEFT alive for open sessions


def test_disable_no_backup_uses_surgical_strip(tmp_path, monkeypatch):
    """Orphan/desync with no snapshot: surgical unroute is the fallback and still clears routing."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    headroom._route_codex()                                    # routed, but NO snapshot taken
    assert not headroom.has_backup(tmp_path / "store")
    ok, _ = headroom.global_disable(tmp_path / "store")
    assert ok and "headroom" not in cfg.read_text() and 'model = "orig"' in cfg.read_text()


def test_bridge_headroom_toggle_enables(ctx, monkeypatch):
    seen = {}
    def _on(store): seen["on"] = True; return (True, "ok")
    def _off(store, **k): seen["off"] = True; return (True, "ok")
    monkeypatch.setattr(headroom, "global_enable", _on)
    monkeypatch.setattr(headroom, "global_disable", _off)
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] and seen.get("on") and ctx.load_state().settings()["headroom"] is True
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert r["ok"] and seen.get("off") and ctx.load_state().settings()["headroom"] is False


def test_bridge_headroom_toggle_reverts_on_enable_failure(ctx, monkeypatch):
    monkeypatch.setattr(headroom, "global_enable", lambda store: (False, "no headroom"))
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] is False
    assert ctx.load_state().settings()["headroom"] is False     # toggle reverted


def test_bridge_headroom_disable_failure_keeps_setting_on(ctx, monkeypatch):
    """A FAILED disable must leave the setting ON so launch/health recovery keeps trying — never
    abandon a still-routed config pointed at a dying proxy with the setting flipped off."""
    monkeypatch.setattr(headroom, "global_enable", lambda store: (True, "ok"))
    monkeypatch.setattr(headroom, "global_disable", lambda store, **k: (False, "restore incomplete"))
    bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert r["ok"] is False
    assert ctx.load_state().settings()["headroom"] is True      # stays ON → recovery keeps trying


def test_bridge_toggle_off_is_graceful(ctx, monkeypatch):
    """User toggle-OFF must call global_disable with reap_proxy=False so an open session pinned to the
    proxy isn't dropped (the proxy is only reaped on quit/health-fail)."""
    seen = {}
    monkeypatch.setattr(headroom, "global_enable", lambda store: (True, "ok"))

    def _off(store, *, reap_proxy=True):
        seen["reap_proxy"] = reap_proxy
        return (True, "ok")

    monkeypatch.setattr(headroom, "global_disable", _off)
    bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert seen["reap_proxy"] is False


# --- heal(): serialized self-heal keyed off ACTUAL state (not the setting) ---------------------

def test_heal_noop_when_proxy_running(tmp_path, monkeypatch):
    _codex_cfg(tmp_path, monkeypatch)
    _patch_proxy(monkeypatch, ready=True)
    changed, msg = headroom.heal(tmp_path / "store")
    assert changed is False and msg == "healthy"               # healthy proxy → never torn down


def test_heal_strips_orphaned_injection_when_proxy_dead(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    calls = _patch_proxy(monkeypatch, ready=False)
    headroom.snapshot_global(tmp_path / "store")              # original captured
    headroom._route_codex()                                   # routing injected, proxy dead
    changed, _ = headroom.heal(tmp_path / "store")
    assert changed is True
    assert 'model_provider = "headroom"' not in cfg.read_text()
    assert 'model = "orig"' in cfg.read_text()                # surgical unroute kept the user body
    assert calls["stop"] >= 1                                 # dead proxy torn down
    assert not (tmp_path / "store" / "headroom-global-backup").exists()


def test_proxy_maybe_running_reads_pidfile_liveness(tmp_path, monkeypatch):
    import subprocess
    store = tmp_path / "store"; store.mkdir()
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: True)   # identity satisfied
    assert headroom.proxy_maybe_running(store) is False          # no pidfile
    (store / "headroom-proxy.pid").write_text("not-a-pid")
    assert headroom.proxy_maybe_running(store) is False          # garbage
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"]); dead.wait()
    (store / "headroom-proxy.pid").write_text(str(dead.pid))
    assert headroom.proxy_maybe_running(store) is False          # dead pid
    (store / "headroom-proxy.pid").write_text(str(os.getpid()))
    assert headroom.proxy_maybe_running(store) is True           # live + identity
    # PID-reuse guard: live PID but NOT our proxy (recycled) → must read as not running
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: False)
    assert headroom.proxy_maybe_running(store) is False


def test_stop_proxy_never_kills_a_recycled_pid(tmp_path, monkeypatch):
    """A stale pidfile pointing at a live but unrelated process (PID reuse) must NOT be signalled."""
    store = tmp_path / "store"; store.mkdir()
    (store / "headroom-proxy.pid").write_text(str(os.getpid()))   # alive, but it's pytest, not a proxy
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: False)
    killed = []
    headroom.stop_proxy(store, kill=lambda pid, sig: killed.append((pid, sig)), sleep=lambda _s: None)
    assert killed == []                                          # no signal sent to the bystander
    assert not (store / "headroom-proxy.pid").exists()           # stale pidfile still cleared


def test_heal_reaps_orphan_proxy_when_app_gone(tmp_path, monkeypatch):
    """Proxy UP but the app is GONE → orphan. heal(app_running=False) must strip the leftover routing
    AND reap the proxy (a hard-killed app never ran its quit teardown)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    calls = _patch_proxy(monkeypatch, ready=True)            # proxy still healthy...
    headroom.snapshot_global(tmp_path / "store")
    headroom._route_codex()                                  # ...with routing left injected
    healed, _ = headroom.heal(tmp_path / "store", app_running=False)
    assert healed is True
    assert 'model_provider = "headroom"' not in cfg.read_text()
    assert 'model = "orig"' in cfg.read_text()
    assert calls["stop"] >= 1                                # orphan proxy reaped


def test_heal_reaps_orphan_proxy_when_app_gone_clean_config(tmp_path, monkeypatch):
    """Even with config already clean (graceful-OFF left the proxy alive), an app-gone proxy is an
    orphan and must be stopped."""
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    calls = _patch_proxy(monkeypatch, ready=True)
    healed, _ = headroom.heal(tmp_path / "store", app_running=False)
    assert healed is True
    assert calls["stop"] >= 1


def test_heal_reaps_wedged_orphan_by_pid_when_app_gone(tmp_path, monkeypatch):
    """A graceful-OFF proxy that's alive-by-PID but NOT answering /readyz (wedged/starting) must
    still be reaped when the app is gone — heal keys off the pidfile, not just proxy_ready()."""
    store = tmp_path / "store"; store.mkdir()
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    calls = _patch_proxy(monkeypatch, ready=False)          # /readyz NOT responding (wedged)
    monkeypatch.setattr(headroom, "_pid_is_proxy", lambda pid: True)  # identity confirmed
    (store / "headroom-proxy.pid").write_text(str(os.getpid()))  # but the process is alive
    healed, _ = headroom.heal(store, app_running=False)
    assert healed is True
    assert calls["stop"] >= 1                                # reaped by PID despite ready=False


def test_heal_keeps_running_proxy_when_app_alive(tmp_path, monkeypatch):
    """The orphan-reap must NOT fire while the app is alive — a healthy managed proxy stays up."""
    _codex_cfg(tmp_path, monkeypatch)
    calls = _patch_proxy(monkeypatch, ready=True)
    changed, msg = headroom.heal(tmp_path / "store", app_running=True)
    assert changed is False and msg == "healthy"
    assert calls["stop"] == 0


def test_heal_reports_failure_when_restore_incomplete(tmp_path, monkeypatch):
    """heal() must NOT claim success when the restore failed — else reconcile clears the setting and
    the UI shows 'restored' over a still-injected, dead-proxy config."""
    cfg = _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch, ready=False)
    headroom.snapshot_global(tmp_path / "store")
    cfg.write_text('model_provider = "headroom"\n')
    monkeypatch.setattr(headroom, "_remove_and_restore",
                        lambda *a, **k: (False, "config restore incomplete"))
    healed, msg = headroom.heal(tmp_path / "store")
    assert healed is False and "incomplete" in msg


def test_heal_noop_when_clean(tmp_path, monkeypatch):
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    _patch_proxy(monkeypatch, ready=False)
    changed, msg = headroom.heal(tmp_path / "store")
    assert changed is False and msg == "clean"                # nothing injected → nothing to do


def test_heal_reasserts_knobs_when_proxy_running(tmp_path, monkeypatch):
    """heal() runs on every poll; when the proxy is healthy it re-pushes the knobs (best-effort, in
    case a proxy we didn't start is the one serving)."""
    _patch_proxy(monkeypatch, ready=True)
    pushed = []
    headroom.heal(tmp_path / "store", push=lambda *a, **k: pushed.append(1) or True)
    assert pushed == [1]


def test_reconcile_keeps_setting_when_heal_fails(ctx, monkeypatch):
    monkeypatch.setattr(headroom, "heal", lambda store, **k: (False, "config restore incomplete"))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    healed, _ = headroom.reconcile(ctx)
    assert healed is False and ctx.load_state().settings()["headroom"] is True   # stays ON to retry


def test_global_enable_aborts_on_dirty_baseline(tmp_path, monkeypatch):
    """If config is already routed with no backup and the strip can't clean it, enable must abort —
    never baseline a routed config as the user's 'original'."""
    _codex_cfg(tmp_path, monkeypatch, 'model_provider = "headroom"\n')   # routed, no backup yet
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    monkeypatch.setattr(headroom, "_unroute_all", lambda *a, **k: None)  # strip can't clean it
    ok, msg = headroom.global_enable(tmp_path / "store")
    assert ok is False and "baseline" in msg
    assert not (tmp_path / "store" / "headroom-global-backup" / "manifest.json").exists()


def test_disable_reports_failure_when_still_injected_no_backup(tmp_path, monkeypatch):
    """Unroute fails and there's no backup → config still routed. Must NOT report success (else
    reconcile clears the setting over a broken config)."""
    _codex_cfg(tmp_path, monkeypatch, 'model_provider = "headroom"\n')   # injected, no backup
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    monkeypatch.setattr(headroom, "_unroute_all", lambda *a, **k: None)  # unroute can't clean
    ok, msg = headroom.global_disable(tmp_path / "store")
    assert ok is False and "still present" in msg


def test_disable_is_op_lock_serialized(tmp_path, monkeypatch):
    """Quit teardown goes through global_disable, which must hold op_lock (serialized vs enable/heal)
    — not a detached, unserialized remove."""
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    store = tmp_path / "store"
    with headroom.op_lock(store):                              # hold the lock...
        # ...a non-blocking disable must NOT proceed (proves it tries to acquire the same lock)
        ok, msg = headroom.global_disable(store, blocking=False)
    assert ok is False and msg == "headroom busy (another operation in progress)"


def test_is_injected_handles_non_utf8(tmp_path, monkeypatch):
    cfg = _codex_cfg(tmp_path, monkeypatch)
    cfg.write_bytes(b'\xff\xfe model_provider = "headroom"')   # invalid UTF-8 + a real marker
    assert headroom._is_injected("codex") is True             # doesn't raise; still finds the marker


def test_bridge_headroom_toggle_survives_exception(ctx, monkeypatch):
    """A raised exception in global_enable must still return a result (toggle never hangs)."""
    def boom(store): raise OSError("disk full")
    monkeypatch.setattr(headroom, "global_enable", boom)
    r = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert r["ok"] is False and "OSError" in r["error"]
    assert ctx.load_state().settings()["headroom"] is False   # enable failed → setting OFF


def test_is_injected_detects_claude_settings_json(tmp_path, monkeypatch):
    """Claude routing lands in settings.json (env.ANTHROPIC_BASE_URL), NOT CLAUDE.md — _is_injected
    must scan every touched file or global_disable could delete the only backup while still routed."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    (tmp_path / "codex").mkdir(); (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{headroom.PROXY_PORT}"}}))
    assert headroom._is_injected("claude") is True            # detected though CLAUDE.md is absent


def test_verify_rtk_migrates_legacy_digest(tmp_path, monkeypatch):
    """An upgrade that changes the digest algorithm must NOT read as a supply-chain tamper: a
    recorded legacy (hex-of-hex) digest is silently migrated, not rejected."""
    from acctsw.util import sha256_text
    fake = tmp_path / "rtk"; fake.write_bytes(b"rtk-binary-v1")
    monkeypatch.setattr(headroom, "rtk_path", lambda: fake)
    store = tmp_path / "store"; store.mkdir()
    (store / "rtk.sha256").write_text(sha256_text(fake.read_bytes().hex()))   # legacy format
    ok, msg = headroom.verify_rtk(store)
    assert ok and "migrated" in msg
    ok2, msg2 = headroom.verify_rtk(store)                                    # now verifies clean
    assert ok2 and "verified" in msg2


def test_is_injected_ignores_prose_mentions(tmp_path, monkeypatch):
    """A user's own config that merely mentions Headroom/headroomlabs in prose must NOT count as
    injected — only the actual config-syntax routing directives do — else the restore backstop would
    overwrite their edits (data loss)."""
    cfg = _codex_cfg(tmp_path, monkeypatch, "# I love Headroom and headroomlabs is great\n")
    assert headroom._is_injected("codex") is False        # prose mentions, no real directive
    cfg.write_text('model_provider = "headroom"\n')
    assert headroom._is_injected("codex") is True         # actual routing directive → injected


def test_reconcile_clears_setting_when_healed(ctx, monkeypatch):
    """reconcile() unifies policy: if dead routing was stripped, the save-credit setting is cleared
    so cx/cl and the GUI agree on state."""
    monkeypatch.setattr(headroom, "heal", lambda store, **k: (True, "removed"))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    changed, _ = headroom.reconcile(ctx)
    assert changed and ctx.load_state().settings()["headroom"] is False


def test_needs_reconcile_false_when_unused(ctx, monkeypatch):
    """The cx/cl hot path must skip the readiness probe when save-credit was never used."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "CODEX_HOME", ctx.data_dir / "nope-codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", ctx.data_dir / "nope-claude")
    assert headroom.needs_reconcile(ctx) is False   # no setting, no backup, no injection


def test_op_lock_nonblocking_returns_false_when_held(tmp_path):
    store = tmp_path / "store"
    with headroom.op_lock(store) as a:
        assert a is True
        with headroom.op_lock(store, blocking=False) as b:
            assert b is False   # already held → non-blocking acquisition fails fast (no freeze)


def test_op_lock_file_lives_outside_backup_dir(tmp_path):
    """The op-lock must not live inside the backup dir, or rmtree-ing the backup mid-hold swaps the
    lock's inode and silently breaks serialization."""
    store = tmp_path / "store"
    with headroom.op_lock(store):
        pass
    assert (store / ".headroom-oplock").exists()
    assert not (store / "headroom-global-backup" / ".oplock").exists()


def test_push_runtime_knobs_posts_shaper_and_holdout():
    """The real push must POST both the shaper switch AND a >0 holdout to /admin/runtime-env on the
    proxy port — that's what turns shaping on for the daemon and enables MEASURED savings."""
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    assert _REAL_PUSH(urlopen=fake_urlopen) is True
    assert seen["url"] == f"http://127.0.0.1:{headroom.PROXY_PORT}/admin/runtime-env"
    assert seen["body"]["HEADROOM_OUTPUT_SHAPER"] == "1"
    assert float(seen["body"]["HEADROOM_OUTPUT_HOLDOUT"]) > 0


def test_push_runtime_knobs_silent_when_proxy_down():
    def boom(req, timeout=None):
        raise OSError("connection refused")
    assert _REAL_PUSH(urlopen=boom) is False  # best-effort: never raises


def test_global_enable_pushes_knobs_after_routing(tmp_path, monkeypatch):
    """A successful enable re-asserts the shaper/holdout on the live proxy (best-effort backstop)."""
    _codex_cfg(tmp_path, monkeypatch, 'model = "orig"\n')
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    _patch_proxy(monkeypatch)
    pushed = []
    ok, _ = headroom.global_enable(tmp_path / "store",
                                   push=lambda *a, **k: pushed.append(1) or True)
    assert ok and pushed == [1]


def test_seed_baseline_runs_learn_once(tmp_path, monkeypatch):
    monkeypatch.setattr(headroom, "headroom_path", lambda: "/fake/headroom")
    calls = []

    def run(args, **k):
        calls.append(args)
        import types; return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    store = tmp_path / "store"
    ok, _ = headroom.seed_baseline(store, run=run)
    assert ok and calls[-1][1:3] == ["learn", "--verbosity"] and headroom.baseline_seeded(store)
    calls.clear()
    ok2, msg = headroom.seed_baseline(store, run=run)        # marker → second call is a no-op
    assert ok2 and "already" in msg and calls == []


def test_harden_env_sets_telemetry_off():
    e = headroom.harden_env({})
    assert e["HEADROOM_TELEMETRY"] == "off"
    assert e["LITELLM_TELEMETRY"] == "False"
    assert e["DO_NOT_TRACK"] == "1"


def test_package_pinned_to_audited_version():
    assert headroom.PACKAGE.endswith("==" + headroom.PINNED_VERSION)


def test_verify_rtk_tofu(tmp_path, monkeypatch):
    fake = tmp_path / "rtk"
    fake.write_bytes(b"rtk-binary-v1")
    monkeypatch.setattr(headroom, "rtk_path", lambda: fake)
    store = tmp_path / "store"
    assert headroom.verify_rtk(store)[0] is True       # records on first sight
    assert headroom.verify_rtk(store)[0] is True       # verifies unchanged
    fake.write_bytes(b"rtk-binary-TAMPERED")
    ok, msg = headroom.verify_rtk(store)
    assert ok is False and "changed" in msg            # tamper detected


def test_headroom_path_prefers_managed_venv(tmp_path, monkeypatch):
    """The packaged .app has no headroom on PATH/next to its frozen python — it must find the
    on-demand managed venv (~/.account-switcher/hr-venv) first."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)
    binp = tmp_path / "hr-venv" / "bin"
    binp.mkdir(parents=True)
    (binp / "headroom").write_text("")
    assert headroom.headroom_path() == str(binp / "headroom")


def test_ensure_installed_creates_managed_venv_and_installs_pinned(tmp_path, monkeypatch):
    """When headroom isn't available, ensure_installed builds the managed venv from a real base python
    and pip-installs the PINNED headroom-ai[proxy] into it (not into the frozen app python)."""
    from acctsw import paths as P
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)
    monkeypatch.setattr(headroom, "_base_python", lambda: "/opt/python3.11")
    state = {"ok": False}
    monkeypatch.setattr(headroom, "available", lambda: state["ok"])
    calls = []

    def fake_run(argv, **k):
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "hr-venv" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "hr-venv" / "bin" / "python").write_text("")
        if "install" in argv and headroom.PACKAGE in argv:
            (tmp_path / "hr-venv" / "bin" / "headroom").write_text("")
            state["ok"] = True
        import types
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(headroom.subprocess, "run", fake_run)
    assert headroom.ensure_installed() is True
    assert any(a[0] == "/opt/python3.11" and a[1:3] == ["-m", "venv"] for a in calls)  # built from base py
    assert any(headroom.PACKAGE in a for a in calls)                                   # pinned pkg installed


def test_bridge_headroom_install(ctx):
    # headroom is already in the venv, so ensure_installed is a fast no-op returning available
    r = bridge.handle(ctx, {"action": "headroom_install"})
    assert r["ok"] is True and r["installed"] is True
    assert r["state"]["headroom_available"] is True
