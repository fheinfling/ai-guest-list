"""Live, end-to-end test of the Headroom global toggle.

Unlike test_headroom.py (which stubs the proxy), this starts the REAL `headroom proxy`, writes REAL
provider routing, proves the data path (a request to the routed Anthropic URL actually reaches the
proxy), then tears everything down and confirms nothing is left listening on the port.

Safety:
  • Fully isolated — routing goes to TEMP config dirs and the pidfile/backup/log to a TEMP store; the
    real ~/.codex and ~/.claude are never touched.
  • Refuses to run if something is already on the port (won't stomp an existing proxy).
  • No billing — the data-path probe sends a dummy key; upstream returns 401 (no tokens charged).
  • Hard teardown in a finally block kills the proxy by PID so an assertion failure can't orphan it.

Skipped by default. Run it explicitly:

    HEADROOM_LIVE=1 .venv/bin/python -m pytest tests/test_headroom_live.py -s -v
"""
import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request

import pytest

LIVE = os.environ.get("HEADROOM_LIVE") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="live test (starts a real proxy) — set HEADROOM_LIVE=1 to run")

PORT = 8787


def _port_busy(port=PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _poke_anthropic(port=PORT):
    """POST to the routed Anthropic endpoint with a dummy key. Returns the HTTP status the PROXY
    served (401/400/200 → it handled the /v1/messages route), or None if the connection was refused
    (proxy not actually serving the route)."""
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "headroom live probe"}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": "sk-headroom-live-probe-dummy",
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code                       # proxy served the route; upstream/auth rejected it
    except urllib.error.URLError:
        return None                         # refused / unreachable → route NOT served


def _hard_kill(store):
    """Belt-and-suspenders: never leave a proxy orphaned on the port if an assertion blows up."""
    from acctsw import headroom
    headroom.stop_proxy(store)
    if not _port_busy():
        return
    import subprocess
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout
    for pid in (p for p in out.split() if p.isdigit()):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def test_headroom_global_toggle_live(tmp_path, monkeypatch):
    from acctsw import headroom
    from acctsw import paths as P

    if _port_busy():
        pytest.skip(f"port {PORT} already in use — refusing to stomp an existing proxy")

    # Isolate EVERY path: routing → temp config dirs; pidfile/log/backup → temp store.
    monkeypatch.setattr(P, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(P, "CLAUDE_CONFIG_DIR", tmp_path / "claude")
    (tmp_path / "codex").mkdir()
    (tmp_path / "claude").mkdir()
    (tmp_path / "codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    store = tmp_path / "store"

    hr_log = os.path.expanduser("~/.headroom/logs/proxy.log")
    log_size_before = os.path.getsize(hr_log) if os.path.exists(hr_log) else 0

    try:
        # --- ENABLE: real proxy + real routing -------------------------------------------------
        t0 = time.monotonic()
        ok, msg = headroom.global_enable(store)
        print(f"\n[enable] ok={ok} msg={msg!r}  ({time.monotonic() - t0:.1f}s)")
        assert ok, f"global_enable failed: {msg}"
        assert headroom.proxy_ready(PORT), "proxy /readyz not healthy after enable"

        # routing landed in the isolated configs (and preserved the user's body)
        codex = (tmp_path / "codex" / "config.toml").read_text()
        assert 'model_provider = "headroom"' in codex, codex
        assert 'model = "gpt-5.5"' in codex, "user body clobbered"
        settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
        assert settings["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{PORT}"
        assert settings["env"]["ENABLE_TOOL_SEARCH"] == "true"
        print(f"[routing] codex + claude configs written to {tmp_path}")

        # the proxy is a live, PID-tracked process
        pid = int(headroom._proxy_pidfile(store).read_text())
        assert headroom._pid_alive(pid), f"tracked proxy pid {pid} not alive"
        print(f"[proxy] pid={pid} alive, listening on {PORT}")

        # --- DATA PATH: a request to the routed URL actually reaches the proxy ------------------
        status = _poke_anthropic(PORT)
        print(f"[data-path] POST /v1/messages → proxy served HTTP {status} (401/400 = routed, no bill)")
        assert status is not None, "connection refused — proxy did not serve the /v1/messages route"
        assert status != 404, "proxy returned 404 — the Anthropic route is not wired"

        time.sleep(1)
        if os.path.exists(hr_log):
            grew = os.path.getsize(hr_log) - log_size_before
            print(f"[data-path] ~/.headroom/logs/proxy.log grew {grew} bytes since enable")
    finally:
        # --- DISABLE + hard teardown -----------------------------------------------------------
        ok, msg = headroom.global_disable(store)
        print(f"[disable] ok={ok} msg={msg!r}")
        _hard_kill(store)

    # --- TEARDOWN VERIFIED ---------------------------------------------------------------------
    assert not headroom.proxy_ready(PORT), "proxy still answering /readyz after disable"
    assert not _port_busy(), f"something still listening on {PORT} after disable"
    assert not headroom._proxy_pidfile(store).exists(), "pidfile not cleared"
    codex2 = (tmp_path / "codex" / "config.toml").read_text()
    assert "headroom" not in codex2, f"routing not stripped: {codex2}"
    assert 'model = "gpt-5.5"' in codex2, "user body lost on teardown"
    print("[teardown] proxy stopped, port free, routing removed, user config intact ✓")
