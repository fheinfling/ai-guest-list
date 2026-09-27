"""Startup imports must survive script launch without a checkout on sys.path."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from app import menubar


@pytest.mark.parametrize("existing_package", [False, True])
def test_relocated_script_bootstraps_only_when_package_is_absent(tmp_path, existing_package):
    repo = Path(__file__).resolve().parents[1]
    shell = tmp_path / "relocated" / "app"
    shell.mkdir(parents=True)
    for name in ("__init__.py", "menubar.py", "terminal.py"):
        shutil.copyfile(repo / "app" / name, shell / name)
    code = r'''
import importlib.util
from pathlib import Path
import runpy
import sys
repo, script, existing = sys.argv[1:]
# Expose only the engine, as an editable engine-only install does. Never expose the
# source checkout via sys.path unless explicitly testing the already-importable case.
spec = importlib.util.spec_from_file_location("acctsw", Path(repo) / "acctsw" / "__init__.py")
engine = importlib.util.module_from_spec(spec)
sys.modules["acctsw"] = engine
spec.loader.exec_module(engine)
sys.modules["objc"] = None
if existing == "True":
    sys.path.append(repo)
    import app
    original = app
before = list(sys.path)
runpy.run_path(script)
from app.terminal import open_key_terminal, prepare_then_login
from app.menubar import usage_poll_interval
import app
assert sys.path == before
assert callable(open_key_terminal) and callable(prepare_then_login)
assert usage_poll_interval(True) < usage_poll_interval(False)
if existing == "True":
    assert app is original
else:
    assert Path(app.__file__).parent == Path(script).parent
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(repo), str(shell / "menubar.py"),
         str(existing_package)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_package_dependency_error_is_not_mistaken_for_missing_app(monkeypatch):
    def broken_import(name):
        raise ModuleNotFoundError("missing package dependency", name="dependency")

    monkeypatch.setattr(menubar.importlib, "import_module", broken_import)
    with pytest.raises(ModuleNotFoundError) as caught:
        menubar._ensure_app_package()
    assert caught.value.name == "dependency"


def test_traceback_omits_submitted_and_stored_secrets_in_exception_chain(capsys):
    submitted = "submitted-test-credential"
    stored = "stored-test-credential"
    try:
        try:
            raise ValueError(stored)
        except ValueError as cause:
            raise RuntimeError(submitted) from cause
    except RuntimeError as exc:
        menubar._log_exception(exc, submitted)
    output = capsys.readouterr().err
    assert "Traceback (most recent call last):" in output
    assert "test_traceback_omits_submitted_and_stored_secrets" in output
    assert "ValueError" in output and "RuntimeError" in output
    assert submitted not in output and stored not in output


def test_traceback_redacts_explicit_secret_even_in_frame_metadata(capsys):
    secret = "credential-in-filename"
    try:
        exec(compile("raise RuntimeError('provider failure')", secret, "exec"))
    except RuntimeError as exc:
        menubar._log_exception(exc, secret)
    output = capsys.readouterr().err
    assert secret not in output
    assert "[redacted]" in output
