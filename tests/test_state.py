import os
import stat

from acctsw.state import State, DEFAULT_SETTINGS


def test_empty_state_defaults(tmp_path):
    s = State.load(tmp_path / "state.json")
    assert s.active("codex") is None
    assert s.accounts("claude") == {}
    assert s.settings() == DEFAULT_SETTINGS


def test_savings_level_default_and_forward_compatible(tmp_path):
    # new setting ships the quality-neutral "conservative" default (no silent input-context compression)
    assert DEFAULT_SETTINGS["savings_level"] == "conservative"
    assert State.load(tmp_path / "state.json").settings()["savings_level"] == "conservative"
    # ...and an OLD state file (no savings_level key) auto-adopts the default on load.
    import json
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"version": 1, "tools": {}, "settings": {"theme": "dark"}}))
    assert State.load(p).settings()["savings_level"] == "conservative"


def test_upsert_and_active(tmp_path):
    s = State.load(tmp_path / "state.json")
    seat = s.upsert_seat("codex", "a@x.com", name="work")
    assert seat["name"] == "work"
    assert seat["limited_until"] is None
    s.set_active("codex", "a@x.com")
    assert s.active("codex") == "a@x.com"


def test_remove_seat_clears_active(tmp_path):
    s = State.load(tmp_path / "state.json")
    s.upsert_seat("codex", "a@x.com")
    s.set_active("codex", "a@x.com")
    assert s.remove_seat("codex", "a@x.com") is True
    assert s.active("codex") is None
    assert s.remove_seat("codex", "a@x.com") is False


def test_save_load_roundtrip_and_perms(tmp_path):
    p = tmp_path / "state.json"
    s = State.load(p)
    s.upsert_seat("claude", "c@x.com", name="seat")
    s.set_active("claude", "c@x.com")
    s.set_setting("theme", "light")
    s.save()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    s2 = State.load(p)
    assert s2.active("claude") == "c@x.com"
    assert s2.settings()["theme"] == "light"


def test_load_merges_new_default_settings(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"version":1,"tools":{},"settings":{"theme":"light"}}')
    s = State.load(p)
    # missing defaults filled in, explicit value preserved
    assert s.settings()["theme"] == "light"
    assert s.settings()["auto_switch"] is True
    assert s.active("codex") is None  # tool scaffolding repaired
