"""PATH hydration + tool resolution for the GUI app (launchd gives it only a minimal PATH)."""
import os

from acctsw import context as C


def test_hydrate_path_adds_common_dirs_without_dupes(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    C.hydrate_path()
    parts = os.environ["PATH"].split(os.pathsep)
    assert "/usr/bin" in parts and "/bin" in parts        # existing entries kept
    assert "/opt/homebrew/bin" in parts                    # a GUI-missing dir was added
    before = os.environ["PATH"]
    C.hydrate_path()                                       # idempotent — no duplicate entries
    assert os.environ["PATH"] == before


def test_which_tool_finds_binary_in_common_dir_off_path(tmp_path, monkeypatch):
    """A tool in a common bin dir that the (GUI) PATH omits is still found."""
    tool = tmp_path / "faketool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(C, "_COMMON_BIN_DIRS", (str(tmp_path),))
    monkeypatch.setenv("PATH", "/nonexistent-xyz")        # tool is NOT on PATH
    assert C.which_tool("faketool") == str(tool)
    assert C.which_tool("nope-not-here") is None


def test_which_tool_prefers_path_hit(tmp_path, monkeypatch):
    onpath = tmp_path / "onpath"
    onpath.mkdir()
    tool = onpath / "faketool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(C, "_COMMON_BIN_DIRS", ())
    monkeypatch.setenv("PATH", str(onpath))
    assert C.which_tool("faketool") == str(tool)
