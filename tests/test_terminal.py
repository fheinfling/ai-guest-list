"""The browser sign-in launches via a `*.command` script + `open` (LaunchServices), NOT osascript.

Two things this guards:
  1. `open` needs no macOS Automation/TCC permission, so a fresh machine can actually open the sign-in
     (the field bug). We assert the launcher shells out to `open <file.command>`, not `osascript`.
  2. The script must scrub the frozen app's PYTHONPATH/PYTHONHOME so the login shell (→ system python3)
     doesn't break with "can't find module 'encodings'".
It also covers resolving the CLI's ABSOLUTE path so a GUI app's minimal PATH can't hide `codex`/`claude`.
"""
import os
import json
import plistlib
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app import terminal


def _capture_open(monkeypatch):
    seen = {}
    real_run = terminal.subprocess.run

    def fake_run(argv, **kw):
        if argv and argv[-1] == "--capabilities":
            return real_run(argv, **kw)
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
    # -a Terminal forces a REAL terminal to run it (a remapped .command handler would silently no-op)
    assert seen["argv"][1:3] == ["-a", "Terminal"]
    assert seen["argv"][-1].endswith(".command")
    # a cleaned env is passed to `open` and is free of the frozen interpreter vars
    assert seen["env"] is not None
    assert "PYTHONPATH" not in seen["env"] and "PYTHONHOME" not in seen["env"]
    # the script the shell runs unsets the interpreter vars itself and then runs the command
    assert "unset " in seen["script"] and "PYTHONPATH" in seen["script"] and "PYTHONHOME" in seen["script"]
    assert "/abs/codex login" in seen["script"]
    assert seen["script"].startswith("#!/bin/zsh")
    # runs through a HARD-CODED login+interactive zsh (always present, always parses -lic) so PATH
    # (node etc.) matches a terminal — NOT $SHELL, which fish/tcsh would reject on the combined flags.
    assert "/bin/zsh -lic" in seen["script"]
    assert "$SHELL" not in seen["script"]
    # the script deletes itself once the login shell returns → no leaked temp file
    assert 'rm -f -- "$0"' in seen["script"]
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


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_resolve_login_command_uses_this_engine(ctx, monkeypatch, tool):
    commands = []
    def checked(capability):
        commands.append(capability)
        return ["/absolute/app engine", "-m", "acctsw"]
    monkeypatch.setattr(terminal, "checked_engine_command", checked)
    ctx.codex_bin = ctx.claude_bin = None
    assert shlex.split(terminal.resolve_login_command(ctx, tool)) == [
        "/absolute/app engine", "-m", "acctsw", "signin", tool]
    seat = "alpha+test@example.test"
    assert shlex.split(terminal.resolve_login_command(ctx, tool, seat=seat))[-4:] == [
        "signin", tool, "--seat", seat]
    assert commands == ["isolated-signin-v1", "isolated-signin-v1"]


@pytest.mark.parametrize("flow", ["key", "signin"])
@pytest.mark.parametrize("mode", ["source", "bundle", "alias"])
@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_app_terminal_bypasses_preserved_old_wrappers(ctx, tmp_path, monkeypatch, mode, tool, flow):
    """Execute the selected engine with a stale wrapper, hostile cwd/env, and quoted paths.

    The bundle shim models Python's bundled search path while recording its startup env;
    source/alias cases run real Python directly. Only the final paid launcher is stubbed.
    """
    from acctsw import install, keyseats

    real_python = sys.executable
    root = tmp_path / "App's installation $(not-a-command)"
    shutil.copytree(Path(install.__file__).parent, root / "acctsw",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "acctsw" / "__main__.py").write_text('''
import json, os, sys
from pathlib import Path
from acctsw import authflows, cli, launcher
if sys.argv[1:] != ["--capabilities"]:
    cli.Context.default = classmethod(lambda cls: cls.for_test(Path(os.environ["TEST_STORE"])))
    def launch(ctx, tool, args, **kwargs):
        print(json.dumps({"tool": tool, "args": args, "key": kwargs["key"],
                          "engine": cli.__file__, "home": os.environ.get("TEST_BUNDLE_HOME"),
                          "ca": os.environ.get("SSL_CERT_FILE")}))
        return 0
    launcher.run = launch
    def signin(ctx, tool, *, seat=None, **kwargs):
        return launch(ctx, tool, [], key=seat)
    authflows.signin = signin
    launcher.exec_stock = lambda *a, **k: sys.exit("must never reach stock")
raise SystemExit(cli.main())
''')
    # A pre-feature checkout forwards every run argument to the stock agent.
    old = tmp_path / "old checkout"
    (old / "acctsw").mkdir(parents=True)
    (old / "acctsw" / "__init__.py").write_text("")
    marker = tmp_path / "stock-was-invoked"
    (old / "acctsw" / "__main__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise SystemExit(\"error: unknown option '--key'\")\n")
    bindir = install.BIN_DIR
    bindir.mkdir(parents=True)
    for name in install.BIN_NAMES:
        path = bindir / name
        path.write_text(install._wrapper_script(name, real_python, old, bindir))
        path.chmod(0o755)
    before = {p.name: p.read_bytes() for p in bindir.iterdir()}
    install.ensure_launchers(bin_dir=bindir, python=real_python, pkg_root=root, wire_rc=False)
    assert before == {p.name: p.read_bytes() for p in bindir.iterdir()}

    python = real_python
    if mode != "source":
        bundle = tmp_path / "Moved App's name.app" / "Contents"
        executable = bundle / "MacOS" / "python"
        executable.parent.mkdir(parents=True)
        if mode == "alias":
            executable.symlink_to(real_python)
            (bundle / "Info.plist").write_bytes(plistlib.dumps({
                "PythonInfoDict": {"py2app": {"alias": True}},
            }))
        else:
            executable.write_text(
                '#!/bin/sh\nexport TEST_BUNDLE_HOME="$PYTHONHOME"\n'
                'unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE __PYVENV_LAUNCHER__\n'
                f'PYTHONPATH={shlex.quote(str(root))} exec {shlex.quote(real_python)} "$@"\n')
            executable.chmod(0o755)
        python = str(executable)
    monkeypatch.setattr(install.sys, "executable", python)
    monkeypatch.setattr(install, "__file__", str(root / "acctsw" / "install.py"))
    monkeypatch.chdir(old)  # -P must prevent importing this checkout
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(old))
    monkeypatch.setenv("PYTHONHOME", "/missing/foreign/python")
    monkeypatch.setenv("PYTHONEXECUTABLE", "/missing/python")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/missing/venv")
    monkeypatch.setenv("TEST_STORE", str(tmp_path / "child-store"))
    seat_id = "seat's id; $(not-a-command)"
    monkeypatch.setattr(keyseats, "get", lambda *a: {"id": seat_id, "harness": tool})
    commands = []
    monkeypatch.setattr(terminal, "open_in_terminal", commands.append)

    if flow == "key":
        terminal.open_key_terminal(ctx, seat_id)
    else:
        terminal.open_in_terminal(terminal.resolve_login_command(ctx, tool, seat=seat_id))
    # Run the exact command after login-shell initialization, including hostile Python vars.
    result = subprocess.run(["/bin/sh", "-c", commands[0]], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[0])
    assert payload["tool"] == tool and payload["key"] == seat_id and payload["args"] == []
    assert Path(payload["engine"]).is_relative_to(root)
    if mode == "bundle":
        assert payload["home"] == str(bundle / "Resources")
        assert payload["ca"] == str(bundle / "Resources" / "openssl.ca" / "cert.pem")
    assert not marker.exists()
    assert before == {p.name: p.read_bytes() for p in bindir.iterdir()}


@pytest.mark.parametrize("failure", ["old", "unsupported", "malformed", "wrong-shape", "missing", "timeout"])
def test_key_terminal_reports_unusable_engine_before_opening(ctx, monkeypatch, failure):
    from acctsw import keyseats
    monkeypatch.setattr(keyseats, "get", lambda *a: {"id": "seat-id", "harness": "claude"})
    opened = []
    monkeypatch.setattr(terminal, "open_in_terminal", opened.append)

    def probe(argv, **kwargs):
        assert argv[-1] == "--capabilities" and "--key" not in argv
        assert kwargs["timeout"] == 30
        if failure == "missing":
            raise FileNotFoundError("private diagnostic")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, stderr="private diagnostic")
        output = {"unsupported": '{"capabilities": []}',
                  "wrong-shape": '{"capabilities": "run-key-v1"}'}
        return types.SimpleNamespace(returncode=2 if failure == "old" else 0,
                                     stdout=output.get(failure, "invalid JSON"),
                                     stderr="private diagnostic")

    monkeypatch.setattr(terminal.subprocess, "run", probe)
    with pytest.raises(RuntimeError, match="reinstall the app.*source installation") as exc:
        terminal.open_key_terminal(ctx, "seat-id")
    assert "private diagnostic" not in str(exc.value)
    assert opened == []


def test_capabilities_does_not_access_user_state(monkeypatch, capsys):
    from acctsw import cli
    monkeypatch.setattr(cli.Context, "default", lambda: pytest.fail("must not access user state"))
    assert cli.main(["--capabilities"]) == 0
    assert "run-key-v1" in json.loads(capsys.readouterr().out)["capabilities"]


def test_capabilities_rejects_a_command(capsys):
    from acctsw import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["--capabilities", "run", "codex", "--key", "seat-id"])
    assert exc.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("harness", ["claude\ntouch /tmp/pwned", "bogus", None])
def test_key_terminal_rejects_an_unknown_harness(ctx, monkeypatch, harness):
    from acctsw import keyseats
    monkeypatch.setattr(keyseats, "get", lambda *a: {"id": "seat-id", "harness": harness})
    monkeypatch.setattr(terminal, "checked_engine_command", lambda *a: pytest.fail("must not probe"))
    monkeypatch.setattr(terminal, "open_in_terminal", lambda *a: pytest.fail("must not open"))
    with pytest.raises(ValueError, match="unknown tool"):
        terminal.open_key_terminal(ctx, "seat-id")
