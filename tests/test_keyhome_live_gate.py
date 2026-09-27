"""The live containment gate must distinguish unavailable coverage from a pass."""
import pytest

from test_keyhome_live import require_harness


@pytest.mark.parametrize("name", ["codex", "claude"])
@pytest.mark.parametrize("required", [False, True])
def test_missing_harness_is_only_optional_without_strict_mode(monkeypatch, tmp_path, name, required):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("AGL_REQUIRE_LIVE_HARNESS", raising=False)
    if required:
        monkeypatch.setenv("AGL_REQUIRE_LIVE_HARNESS", "1")
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome) as caught:
        require_harness(name)
    message = str(caught.value)
    assert f"{name} is absent from PATH" in message
    assert "containment NOT EXERCISED" in message
    if required:
        assert "harness was required by AGL_REQUIRE_LIVE_HARNESS=1" in message


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_strict_mode_accepts_installed_harness(monkeypatch, tmp_path, name):
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("AGL_REQUIRE_LIVE_HARNESS", "1")
    assert require_harness(name) == str(binary)
