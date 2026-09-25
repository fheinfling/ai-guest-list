"""PATH hydration + tool resolution for the GUI app (launchd gives it only a minimal PATH)."""
import os

from acctsw import context as C
from acctsw import paths as P


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


def test_managed_codex_home_env_is_not_used_as_the_shared_mirror(tmp_path, monkeypatch):
    """A child launched from another cwd must not inherit a seat home as Context.default's mirror."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(P.CODEX_HOMES / "plain@example.test"))
    assert P._canonical_codex_home() == P.HOME / ".codex"


def test_a_seat_home_in_the_pre_1_0_2_layout_is_still_rejected(tmp_path, monkeypatch):
    """The inherited value can name a home from the old layout, or one that no longer exists at all
    — the name alone has to be enough to recognise it as ours."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(P.CODEX_HOMES_LEGACY / "gone@example.test"))
    assert P._canonical_codex_home() == P.HOME / ".codex"


def test_external_codex_home_env_remains_a_supported_shared_mirror(tmp_path, monkeypatch):
    custom = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    assert P._canonical_codex_home() == custom


def test_a_custom_codex_home_elsewhere_in_the_store_is_honoured(monkeypatch):
    """Only the two managed seat roots are ours. A home a developer parks somewhere else under
    ~/.account-switcher is their choice, and switching must keep targeting it."""
    custom = P.APP_SRC_DIR / "dev-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    assert P._canonical_codex_home() == custom
