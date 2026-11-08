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
    # A same-version engine from before self-restart is behind too; a release bump makes it "older".
    before_restart = [c for c in engine.CAPABILITIES if c != "engine-restart-v1"]
    assert engine.compare({"version": __version__, "capabilities": before_restart}) == {
        "reason": "missing", "version": __version__, "missing": ["engine-restart-v1"]}
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
    assert kinds["sessions"]["text"].startswith("2 terminals run an older engine than this app (")
    assert "Claude terminal (engine 1.1.2)" in kinds["sessions"]["text"]
    assert "Codex terminal (an older engine)" in kinds["sessions"]["text"]
    assert "Restart them" in kinds["sessions"]["text"]
    one = engine.drift_warnings(None, sessions[1:])
    assert one[0]["text"].startswith("A Codex terminal runs an older engine. Restart it")
    assert engine.drift_warnings(None, []) == []
    assert engine.drift_warnings(None, [], "too old")[0]["kind"] == "app"


def test_sessions_that_will_restart_themselves_raise_no_warning():
    sessions = [{"tool": "claude", "pid": 1, "engine": "1.7.0", "self_restart": True},
                {"tool": "codex", "pid": 2, "engine": "1.7.1", "self_restart": True}]
    assert engine.drift_warnings(None, sessions) == []


@pytest.mark.parametrize("flag", [False, None, "true"])
def test_sessions_that_cant_restart_themselves_keep_the_warning(flag):
    warnings = engine.drift_warnings(None, [{"tool": "codex", "pid": 2, "engine": "1.3.1",
                                             "self_restart": flag}])
    assert [w["kind"] for w in warnings] == ["sessions"]
    assert warnings[0]["text"].startswith("A Codex terminal runs engine 1.3.1. Restart it")


@pytest.mark.parametrize("version,unsafe", [("1.5.0", False), ("1.3.1", False), ("1.3.0", True),
                                            (None, True)])
def test_only_an_engine_below_the_floor_is_said_to_risk_signing_you_out(version, unsafe):
    warning = engine.drift_warnings(None, [{"tool": "claude", "pid": 1, "engine": version}])[0]
    assert warning["severity"] == ("hard" if unsafe else "soft")
    text = warning["text"]
    assert ("can't sign you out" in text) is unsafe
    assert ("latest switching" in text) is not unsafe


def test_app_and_wrapper_drift_stay_hard():
    warnings = engine.drift_warnings({"path": "/old/acctsw", "version": "1.6.1"}, [], "too old")
    assert [(w["kind"], w["severity"]) for w in warnings] == [("app", "hard"), ("wrapper", "hard")]


def test_safe_session_notice_names_the_terminal_in_local_time_and_home_folder(monkeypatch):
    import time
    from pathlib import Path
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        warning, = engine.drift_warnings(None, [{
            "tool": "claude", "pid": 42, "email": "business@x.com", "engine": "1.6.1",
            "started_at": "2026-11-08T18:23:00Z", "cwd": str(Path.home() / "Work/now"),
        }])
        assert warning == {"kind": "sessions", "severity": "soft", "text": (
            "A Claude terminal on business@x.com (started 19:23 in ~/Work/now) runs engine 1.6.1. "
            "Restart it when convenient to get the latest switching."), "pids": [42]}
    finally:
        monkeypatch.undo()
        time.tzset()


def test_session_notice_caps_names_but_checks_the_safety_of_every_session():
    sessions = [{"tool": "codex", "pid": i, "email": f"seat{i}@x.com", "engine": "1.6.1"}
                for i in range(4)]
    sessions[-1]["engine"] = "1.3.0"
    warning, = engine.drift_warnings(None, sessions)
    assert warning["severity"] == "hard"
    assert "Codex terminal on seat0@x.com" in warning["text"]
    assert "Codex terminal on seat1@x.com" in warning["text"]
    assert "+2 more" in warning["text"]
    assert "seat2@x.com" not in warning["text"] and "seat3@x.com" not in warning["text"]
    assert "so a seat switch can't sign you out" in warning["text"]


def test_mixed_sessions_count_and_name_only_those_needing_a_manual_restart():
    capable = {"tool": "claude", "pid": 1, "engine": "1.6.9", "self_restart": True}
    one = engine.drift_warnings(None, [capable, {"tool": "codex", "pid": 2, "engine": "1.3.0",
                                                 "capabilities": []}])
    assert len(one) == 1
    assert one[0]["text"].startswith("A Codex terminal runs engine 1.3.0. Restart it")
    assert "1.6.9" not in one[0]["text"] and one[0]["pids"] == [2]
    two = engine.drift_warnings(None, [capable, {"tool": "codex", "pid": 2, "engine": "1.3.0",
                                                 "capabilities": []},
                                       {"tool": "codex", "pid": 3, "engine": None,
                                        "capabilities": []}])
    assert two[0]["text"].startswith("2 terminals run an older engine than this app (")
    assert "Restart them" in two[0]["text"] and "1.6.9" not in two[0]["text"]
    assert two[0]["pids"] == [2, 3]
