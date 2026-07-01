"""The Terminal login spawn must not leak the frozen app's PYTHONPATH into the shells it opens.

py2app's native launcher setenv's PYTHONPATH=.../python311.zip into the process environment; any
child that inherits it (Terminal → its shells → system python3) breaks with "can't find module
'encodings'". open_in_terminal must scrub the interpreter-redirect vars before spawning osascript.
"""
from app import terminal


def test_open_in_terminal_scrubs_python_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/frozen/AI Guest List.app/Contents/Resources/lib/python311.zip:")
    monkeypatch.setenv("PYTHONHOME", "/frozen/home")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env")
        return None

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    terminal.open_in_terminal("codex login")

    assert seen["argv"][0] == "osascript"
    # a cleaned env must be passed explicitly (not inherited) and free of the interpreter vars
    assert seen["env"] is not None
    assert "PYTHONPATH" not in seen["env"]
    assert "PYTHONHOME" not in seen["env"]
    # AND the command run in Terminal's shell must unset them itself — env= only cleans the osascript
    # process, but an already-running Terminal.app runs `do script` in its OWN environment.
    script = seen["argv"][-1]
    assert "unset " in script and "PYTHONPATH" in script and "PYTHONHOME" in script
    assert "codex login" in script


def test_open_in_terminal_noop_on_empty_command(monkeypatch):
    called = []
    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: called.append(1))
    terminal.open_in_terminal("")
    assert called == []
