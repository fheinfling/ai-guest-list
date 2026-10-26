"""App/engine drift: identify the engine behind a wrapper by asking it, and say so when it's older.

The real failure: the app was 1.4.2, but ``~/.local/bin/acctsw`` was a hand-written
``exec "$VENV/bin/python" -m acctsw`` from a checkout that was never pulled (1.1.2). Every
terminal ran the old engine against the app's store and wrote Claude's Keychain item in the old
format, signing every session out; the regex-based staleness check judged the wrapper fine. Fake
engines here are tiny /bin/sh scripts in tmp, never the developer's wrappers.
"""
import json
import os
import subprocess

import pytest

from acctsw import __version__, cli, engine


def _fake_engine(path, *, capabilities=None, version="1.1.2", old_cli=False):
    """A shell script answering like an engine of ``version``; ``old_cli`` predates --capabilities."""
    if old_cli:
        caps = 'echo "usage: acctsw" >&2; exit 2'
    else:
        payload = {"capabilities": capabilities if capabilities is not None else ["run-key-v1"]}
        if version is not None:
            payload["version"] = version
        caps = f"echo '{json.dumps(payload)}'"
    path.write_text("#!/bin/sh\n"
                    f'case "$1" in\n  --capabilities) {caps} ;;\n'
                    f'  --version) echo "acctsw {version or "1.0.0"}" ;;\n'
                    '  *) exit 9 ;;\nesac\n')
    path.chmod(0o755)
    return path


def test_parse_version_and_ordering():
    assert engine.parse_version("acctsw 1.4.2") == (1, 4, 2)
    assert engine.parse_version("1.4") == engine.parse_version("1.4.0")
    assert engine.parse_version("dev") is None and engine.parse_version(None) is None
    assert engine.older_than("1.1.2", "1.4.2")
    assert engine.older_than("1.4.2", "1.10.0")          # numeric, not lexicographic
    assert not engine.older_than("1.4.2", "1.4.2")
    assert not engine.older_than("garbage", "1.4.2")     # unknown is never "older"


def test_capabilities_reports_version_before_any_store_access(capsys, monkeypatch):
    # The handshake must stay ahead of Context/Keychain: probes run against foreign wrappers.
    monkeypatch.setattr(cli.Context, "default", lambda: pytest.fail("touched the store"))
    assert cli.main(["--capabilities"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == __version__
    assert set(engine.CAPABILITIES) <= set(payload["capabilities"])


def test_probe_reads_version_from_the_capabilities_handshake(tmp_path):
    exe = _fake_engine(tmp_path / "acctsw", version="1.3.0", capabilities=["run-key-v1"])
    assert engine.probe(exe) == {"version": "1.3.0", "capabilities": ["run-key-v1"]}


@pytest.mark.parametrize("old_cli", [True, False])
def test_probe_falls_back_to_version_for_engines_before_the_handshake(tmp_path, old_cli):
    # 1.1.x either lacks --capabilities (argparse exit 2) or prints it without a version.
    exe = _fake_engine(tmp_path / "acctsw", version=None if not old_cli else "1.1.2",
                       old_cli=old_cli)
    if not old_cli:
        exe.write_text(exe.read_text().replace('echo "acctsw 1.0.0"', 'echo "acctsw 1.1.2"'))
    found = engine.probe(exe)
    assert found["version"] == "1.1.2"
    assert engine.compare(found)["reason"] == "older"


def test_probe_runs_with_python_redirects_stripped(tmp_path, monkeypatch):
    # The app inherits py2app's PYTHONHOME/PYTHONPATH; a terminal would not. Probe what it runs.
    seen = tmp_path / "env"
    exe = tmp_path / "acctsw"
    exe.write_text(f'#!/bin/sh\nenv > {seen}\necho \'{{"capabilities": [], "version": "1.0"}}\'\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PYTHONHOME", "/App.app/Contents/Resources")
    monkeypatch.setenv("PYTHONPATH", "/App.app/Contents/Resources/lib/python311.zip")
    assert engine.probe(exe)["version"] == "1.0"
    env = seen.read_text()
    assert "PYTHONHOME=" not in env and "PYTHONPATH=" not in env


@pytest.mark.parametrize("body", [None, "#!/bin/sh\nexit 1\n", "#!/bin/sh\necho nonsense\n",
                                  "#!/bin/sh\nsleep 5\n"])
def test_probe_unknown_engine_is_none(tmp_path, body):
    exe = tmp_path / "acctsw"
    if body is not None:
        exe.write_text(body)
        exe.chmod(0o755)
    assert engine.probe(exe, timeout=0.5) is None
    assert engine.compare(None) is None


def test_compare_older_missing_and_newer():
    assert engine.compare({"version": "1.1.2", "capabilities": []})["reason"] == "older"
    same_but_behind = engine.compare({"version": __version__, "capabilities": ["run-key-v1"]})
    assert same_but_behind["reason"] == "missing"
    assert "engine-id-v1" in same_but_behind["missing"]
    assert engine.compare({"version": __version__, "capabilities": list(engine.CAPABILITIES)}) is None
    # A newer terminal engine means THIS app is stale — the store's writer guard covers that.
    assert engine.compare({"version": "99.0", "capabilities": []}) is None


def test_drift_warnings_name_path_versions_and_fix():
    wrapper = {"path": "/u/.local/bin/acctsw", "exe": "$VENV/bin/python", "version": "1.1.2",
               "app_version": "1.4.2"}
    sessions = [{"tool": "claude", "pid": 1, "engine": "1.1.2"},
                {"tool": "codex", "pid": 2, "engine": None}]
    warnings = engine.drift_warnings(wrapper, sessions)
    kinds = {w["kind"]: w for w in warnings}
    text = kinds["wrapper"]["text"]
    assert "/u/.local/bin/acctsw" in text and "$VENV/bin/python" in text
    assert "1.1.2" in text and "1.4.2" in text and "git pull" in text and "acctsw install" in text
    assert kinds["wrapper"]["action"] == "adopt_engine"
    assert kinds["sessions"]["text"].startswith("2 terminal sessions run an older engine (1.1.2)")
    assert "restart them" in kinds["sessions"]["text"]
    one = engine.drift_warnings(None, sessions[1:])
    assert one[0]["text"].startswith("1 terminal session runs an older engine than")
    assert engine.drift_warnings(None, []) == []
    assert engine.drift_warnings(None, [], "too old")[0]["kind"] == "app"
