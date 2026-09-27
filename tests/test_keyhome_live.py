"""Post-session containment using installed harnesses, fake keys and isolated homes.

Unlike preparation tests, these start a real thread and execute a shell command through
Codex's app-server. No model request or real credential is needed. Skipped only when Codex
is absent, unless AGL_REQUIRE_LIVE_HARNESS=1 requires failure instead. Claude's live
shell/snapshot probe follows the same rule. Reproduce the vulnerable controls separately with:

    PYTHONPATH=. python3 tests/test_keyhome_live.py /private/tmp/keyhome-experiment

The experiment deliberately retains FAKE-key snapshots outside the repository for inspection.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import pty
import queue
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

from acctsw import keyhome, keyseats, providers
from acctsw.context import Context


FAKE_SECRET = "sk-ACCTSW-FAKE-SECRET-SECURITY-AUDIT-" + "x" * 128


def require_harness(name):
    binary = shutil.which(name)
    if binary is None:
        message = f"Live credential containment NOT EXERCISED: {name} is absent from PATH"
        if os.environ.get("AGL_REQUIRE_LIVE_HARNESS") == "1":
            pytest.fail(f"{message}; harness was required by AGL_REQUIRE_LIVE_HARNESS=1",
                        pytrace=False)
        pytest.skip(f"{message}; install {name} to run the post-session containment regression")
    return binary


def package_entries(home):
    # Fresh key-seat homes must not download the large app-server daemon, even in CI.
    return sorted(str(p.relative_to(home)) for p in (home / "packages").glob("*"))


def scan(home):
    # Include hidden files and binary databases/WALs, not just config or text logs.
    return sorted(str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and FAKE_SECRET.encode() in p.read_bytes())


def run_codex(root, binary, provider="openai", control=None):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    ctx = Context.for_test(root)
    p = providers.get_provider(provider, base_url=(
        "https://gateway.invalid/v1" if provider == "openai_compatible" else None))
    if provider == "openai_compatible":
        p = replace(p, responses_support="verified")
    seat = keyseats.add(ctx, p, FAKE_SECRET, label="Fake containment audit", model="gpt-5.4",
                        get=lambda *args: (200, "{}"))
    runtime = keyhome.prepare(ctx, seat["id"], pin="containment-audit")
    config = runtime.home / "config.toml"
    if control:
        content = config.read_text().replace("shell_snapshot = false", "shell_snapshot = true")
        if control == "before":
            start = content.index("[shell_environment_policy]")
            end = content.index("[analytics]", start)
            content = content[:start] + content[end:]
        config.write_text(content)
    env = keyhome.build_env(ctx, runtime, env={
        "HOME": str(root), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash",
        "TMPDIR": str(root),
    })
    assert env[runtime.env_key] == FAKE_SECRET
    messages = queue.Queue()
    with (runtime.home / "audit-stderr.log").open("w") as stderr:
        child = subprocess.Popen([binary, "app-server"], cwd=root, env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=stderr, text=True)

        def read():
            for line in child.stdout:
                messages.put(json.loads(line))
            messages.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def send(id, method, params):
            child.stdin.write(json.dumps({"id": id, "method": method, "params": params}) + "\n")
            child.stdin.flush()

        def receive(predicate):
            deadline = time.monotonic() + 20
            while True:
                try:
                    message = messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise AssertionError("Codex did not complete the live containment probe") from None
                assert message is not None, "Codex exited before the live containment probe"
                assert "error" not in message, message
                if predicate(message):
                    return message

        try:
            send(1, "initialize", {"clientInfo": {"name": "keyhome-audit", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
            receive(lambda m: m.get("id") == 1)
            send(2, "thread/start", {"cwd": str(root), "approvalPolicy": "never"})
            started = receive(lambda m: m.get("id") == 2)
            thread = started["result"]["thread"]["id"]
            # Allow asynchronous snapshot creation to finish before the shell probe.
            # The positive controls require a real snapshot; a startup failure cannot pass.
            if control:
                deadline = time.monotonic() + 10
                while not list(runtime.home.glob("shell_snapshots/*.sh")):
                    assert time.monotonic() < deadline, "Positive control produced no snapshot"
                    time.sleep(.05)
            send(3, "thread/shellCommand", {"threadId": thread, "timeoutMs": 5000,
                 "command": f'if test -n "${{{runtime.env_key}-}}"; then echo KEY_PRESENT; '
                            'else echo KEY_ABSENT; fi'})
            completed = receive(lambda m: m.get("method") == "item/completed"
                                and m["params"]["item"]["type"] == "commandExecution")
            item = completed["params"]["item"]
            assert item["exitCode"] == 0, item
            # Observe the home while the process is alive as well as after shutdown.
            time.sleep(.25)
            live_matches = scan(runtime.home)
            live_packages = package_entries(runtime.home)
        finally:
            # Simulate an interrupted terminal/process: graceful Codex shutdown can unlink
            # an already-leaked snapshot and hide the vulnerability from a post-run scan.
            child.kill()
            child.wait(timeout=5)
            child.stdin.close()
            reader.join(timeout=5)
            child.stdout.close()
    return {"home": str(runtime.home), "shell": item["aggregatedOutput"].strip(),
            "live_matches": live_matches, "post_session_matches": scan(runtime.home),
            "live_packages": live_packages, "post_session_packages": package_entries(runtime.home),
            "snapshots": [str(p.relative_to(runtime.home))
                          for p in runtime.home.glob("shell_snapshots/*.sh")]}


@pytest.mark.parametrize("provider", ["openai", "langdock", "openrouter", "openai_compatible"])
def test_real_codex_post_session_secret_containment(provider):
    binary = require_harness("codex")
    # Keep the isolated home short enough for Codex's macOS socket limit.
    with tempfile.TemporaryDirectory(prefix="kh-live-", dir="/tmp") as temp:
        result = run_codex(Path(temp), binary, provider)
        assert result["shell"] == "KEY_ABSENT", result
        assert result["live_matches"] == [], result
        assert result["post_session_matches"] == [], result
        assert result["snapshots"] == [], result
        assert result["live_packages"] == [], result
        assert result["post_session_packages"] == [], result


def run_claude(root, binary, control=False):
    """Use Claude's interactive ! shell path so a snapshot really is created before scanning."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    ctx = Context.for_test(root)
    seat = keyseats.add(ctx, providers.get_provider("anthropic"), FAKE_SECRET,
                        label="Fake containment audit", model="claude-sonnet-4-6",
                        get=lambda *args: (200, "{}"))
    runtime = keyhome.prepare(ctx, seat["id"], pin="containment-audit")
    env = keyhome.build_env(ctx, runtime, env={
        "HOME": str(root), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash",
        "TMPDIR": str(root), "TERM": "xterm-256color", "CLAUDE_CODE_MAX_RETRIES": "0",
    })
    assert env[runtime.env_key] == FAKE_SECRET
    if control:
        env.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)
    (runtime.home / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(root): {"hasTrustDialogAccepted": True}},
    }))
    # An empty rc exercises the snapshot's functions/options/aliases path too.
    (root / (".zshrc" if env["SHELL"].endswith("zsh") else ".bashrc")).write_text("# audit\n")
    result_file = root / "shell-result"
    master, slave = pty.openpty()
    try:
        child = subprocess.Popen([binary], cwd=root, env=env, stdin=slave, stdout=slave,
                                 stderr=slave, start_new_session=True)
    finally:
        os.close(slave)
    terminal = bytearray()

    def pump_until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert child.poll() is None, "Claude exited before the shell probe"
            assert time.monotonic() < deadline, "Claude did not complete the shell probe"
            if select.select([master], [], [], .05)[0]:
                terminal.extend(os.read(master, 65536))

    try:
        pump_until(lambda: b"for shortcuts" in terminal)
        command = ('! if test -n "${ANTHROPIC_AUTH_TOKEN-}"; then echo KEY_PRESENT; '
                   f'else echo KEY_ABSENT; fi > {shlex.quote(str(result_file))}')
        os.write(master, command.encode())
        # Send Enter as a separate event; a pasted CR is sanitized by Claude's editor.
        time.sleep(.2)
        os.write(master, b"\r")
        pump_until(lambda: result_file.exists() and bool(result_file.read_text().strip()))
        snapshots = [str(p.relative_to(runtime.home))
                     for p in runtime.home.glob("shell-snapshots/*.sh")]
        assert snapshots, "Claude did not create a snapshot; an empty scan proves nothing"
        live_matches = scan(runtime.home)
        live_packages = package_entries(runtime.home)
    finally:
        child.kill()
        child.wait(timeout=5)
        os.close(master)
    return {"home": str(runtime.home), "shell": result_file.read_text().strip(),
            "live_matches": live_matches, "post_session_matches": scan(runtime.home),
            "live_packages": live_packages, "post_session_packages": package_entries(runtime.home),
            "snapshots": snapshots}


def test_real_claude_post_session_secret_containment():
    binary = require_harness("claude")
    with tempfile.TemporaryDirectory(prefix="kh-live-", dir="/tmp") as temp:
        result = run_claude(Path(temp), binary)
        assert result["shell"] == "KEY_ABSENT", result
        assert result["live_matches"] == [], result
        assert result["post_session_matches"] == [], result
        assert result["live_packages"] == [], result
        assert result["post_session_packages"] == [], result


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]).resolve()
    binary = shutil.which("codex")
    assert binary, "Codex is required"
    for control in ("before", "policy-only", None):
        result = run_codex(root / (control or "after"), binary, control=control)
        print(json.dumps({"case": control or "after", **result}, indent=2), flush=True)
        grep = subprocess.run(["rg", "--hidden", "--no-ignore", "-a", "-l", "-F",
                               FAKE_SECRET, result["home"]], capture_output=True, text=True)
        print(f"rg exit={grep.returncode}\n{grep.stdout}", flush=True)
        assert grep.returncode == (0 if control else 1)
        assert bool(result["post_session_matches"]) == bool(control)
        if not control:
            assert result["shell"] == "KEY_ABSENT"
    binary = shutil.which("claude")
    assert binary, "Claude Code is required"
    for control in (True, False):
        case = "claude-before" if control else "claude-after"
        result = run_claude(root / case, binary, control=control)
        print(json.dumps({"case": case, **result}, indent=2), flush=True)
        grep = subprocess.run(["rg", "--hidden", "--no-ignore", "-a", "-l", "-F",
                               FAKE_SECRET, result["home"]], capture_output=True, text=True)
        print(f"rg exit={grep.returncode}\n{grep.stdout}", flush=True)
        assert grep.returncode == 1
        assert result["shell"] == ("KEY_PRESENT" if control else "KEY_ABSENT")
