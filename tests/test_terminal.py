"""The browser sign-in launches via a `*.command` script + `open` (LaunchServices), NOT osascript.

Two things this guards:
  1. `open` needs no macOS Automation/TCC permission, so a fresh machine can actually open the sign-in
     (the field bug). We assert the launcher shells out to `open <file.command>`, not `osascript`.
  2. The script must scrub the frozen app's PYTHONPATH/PYTHONHOME so the login shell (→ system python3)
     doesn't break with "can't find module 'encodings'".
It also covers resolving the CLI's ABSOLUTE path so a GUI app's minimal PATH can't hide `codex`/`claude`.
"""
import os
import types

import pytest

from app import terminal


def _capture_open(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env")
        if argv and argv[0] == "open":
            seen["script"] = open(argv[-1]).read()
            seen["mode"] = os.stat(argv[-1]).st_mode
            os.unlink(argv[-1])  # don't leave temp files behind in the test run
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    return seen


def test_open_in_terminal_launches_via_open_and_scrubs_python_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/frozen/AI Guest List.app/Contents/Resources/lib/python311.zip:")
    monkeypatch.setenv("PYTHONHOME", "/frozen/home")
    seen = _capture_open(monkeypatch)

    terminal.open_in_terminal("/abs/codex login")

    # LaunchServices `open`, NOT osascript/AppleEvents (which would need Automation permission).
    assert seen["argv"][0] == "open"
    assert seen["argv"][-1].endswith(".command")
    # a cleaned env is passed to `open` and is free of the frozen interpreter vars
    assert seen["env"] is not None
    assert "PYTHONPATH" not in seen["env"] and "PYTHONHOME" not in seen["env"]
    # the script the shell runs unsets the interpreter vars itself and then runs the command
    assert "unset " in seen["script"] and "PYTHONPATH" in seen["script"] and "PYTHONHOME" in seen["script"]
    assert "/abs/codex login" in seen["script"]
    assert seen["script"].startswith("#!/bin/zsh")
    # the login runs through a LOGIN + INTERACTIVE shell so PATH (node + version-manager shims) matches
    # the user's own terminal — a bare non-login script would miss them.
    assert '"${SHELL:-/bin/zsh}" -lic' in seen["script"]
    # the .command must be executable or `open` would fail to run it
    assert seen["mode"] & 0o100


def test_open_in_terminal_noop_on_empty_command(monkeypatch):
    called = []
    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: called.append(1))
    terminal.open_in_terminal("")
    assert called == []


def test_open_in_terminal_raises_and_surfaces_reason_when_open_fails(monkeypatch):
    def fake_run(argv, **kw):
        # the fake writes no file, so cleanup/read is skipped; return a descriptive failure
        return types.SimpleNamespace(returncode=1, stderr="LSOpenURLsWithRole failed")
    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as e:
        terminal.open_in_terminal("/abs/codex login")
    assert "LSOpenURLsWithRole failed" in str(e.value)  # the real reason is surfaced, not just "try again"


def test_resolve_login_command_uses_absolute_binary(ctx):
    ctx.codex_bin = "/opt/homebrew/bin/codex"
    ctx.claude_bin = "/opt/homebrew/bin/claude"
    assert terminal.resolve_login_command(ctx, "codex") == "/opt/homebrew/bin/codex login"
    assert terminal.resolve_login_command(ctx, "claude") == "/opt/homebrew/bin/claude auth login"


def test_resolve_login_command_falls_back_to_bare_when_unresolved(ctx):
    # a CLI on an rc-only shim (asdf/volta/nvm) isn't on the GUI app's PATH → ctx.codex_bin is None.
    # We must NOT hard-fail (that regressed those users); fall back to the bare name — the login shell
    # sources their rc and finds it.
    ctx.codex_bin = None
    assert terminal.resolve_login_command(ctx, "codex") == "codex login"
    ctx.claude_bin = None
    assert terminal.resolve_login_command(ctx, "claude") == "claude auth login"


def test_resolve_login_command_quotes_a_spacey_path(ctx):
    ctx.codex_bin = "/Users/a b/bin/codex"
    cmd = terminal.resolve_login_command(ctx, "codex")
    # a path with a space must be shell-quoted so the login shell runs the right binary
    assert "'/Users/a b/bin/codex'" in cmd and cmd.endswith("login")


def test_json_escape():
    assert terminal.json_escape('a "b" c') == '"a \\"b\\" c"'
