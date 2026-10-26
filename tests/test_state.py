import os
import stat
import json

from acctsw.state import State, DEFAULT_SETTINGS


def test_empty_state_defaults(tmp_path):
    s = State.load(tmp_path / "state.json")
    assert s.active("codex") is None
    assert s.accounts("claude") == {}
    assert s.settings() == DEFAULT_SETTINGS
    assert s.settings()["supervise_shell"] is True
    assert s.settings()["confirm_key_switch"] is True
    assert s.settings()["key_fallback"] is False
    assert s.data["keys"] == {}


def test_no_retired_headroom_settings(tmp_path):
    # the retired "save credit" feature left no default settings behind
    assert "headroom" not in DEFAULT_SETTINGS and "savings_level" not in DEFAULT_SETTINGS


def test_upsert_and_active(tmp_path):
    s = State.load(tmp_path / "state.json")
    seat = s.upsert_seat("codex", "a@x.com", name="work")
    assert seat["name"] == "work"
    assert seat["limited_until"] is None
    s.set_active("codex", "a@x.com")
    assert s.active("codex") == "a@x.com"
    s.set_last_on_floor("codex", "a@x.com", "2026-07-31T10:00:00+00:00")
    assert seat["last_on_floor"] == "2026-07-31T10:00:00+00:00"


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
    assert s.settings()["supervise_shell"] is True
    assert s.active("codex") is None  # tool scaffolding repaired
    assert s.settings()["confirm_key_switch"] is True
    assert s.settings()["key_fallback"] is False
    assert s.data["keys"] == {}


def test_key_defaults_preserve_existing_and_future_fields_without_saving(tmp_path):
    p = tmp_path / "state.json"
    data = {"rev": 17, "keys": {"id": {"model": "m", "future_field": 42}},
            "settings": {"confirm_key_switch": False, "key_fallback": True, "future": "yes"},
            "future_section": {"value": 1}}
    p.write_text(json.dumps(data))
    before = p.read_bytes()
    s = State.load(p)
    assert s.data["keys"] == data["keys"]
    assert s.data["future_section"] == data["future_section"]
    assert s.settings()["confirm_key_switch"] is False
    assert s.settings()["key_fallback"] is True
    assert s.settings()["future"] == "yes"
    assert s.data["rev"] == 17
    assert p.read_bytes() == before
    s.save()
    assert State.load(p).data["rev"] == 18


# --- shared-state writer guard (app/engine drift) -------------------------------------------

import pytest

from acctsw import __version__, state as state_mod
from acctsw.errors import EngineTooOld


def test_save_stamps_the_writer_and_old_files_still_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "tools": {}}))   # written before the stamp existed
    s = State.load(path)
    assert state_mod.writer_block(s.data) is None
    s.save()
    stamp = json.loads(path.read_text())["engine"]
    assert stamp == {"last_writer": __version__, "newest_writer": __version__,
                     "min_writer": state_mod.MIN_CREDENTIAL_WRITER}


def test_writer_floor_and_newest_only_rise(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"engine": {"last_writer": "99.0", "newest_writer": "99.0",
                                           "min_writer": "98.0"}}))
    s = State.load(path)
    # Saving from an older (guarded) engine — e.g. a settings toggle — can't erase the claim.
    s.save()
    stamp = json.loads(path.read_text())["engine"]
    assert stamp == {"last_writer": __version__, "newest_writer": "99.0", "min_writer": "98.0"}


def test_writer_block_refuses_an_engine_below_the_floor(tmp_path, monkeypatch):
    data = {"engine": {"newest_writer": "2.0.0", "min_writer": "1.9.0"}}
    reason = state_mod.writer_block(data, version="1.4.2")
    assert "1.4.2" in reason and "2.0.0" in reason and "1.9.0" in reason
    assert "git pull" in reason and "acctsw install" in reason
    assert state_mod.writer_block(data, version="1.9.0") is None
    assert state_mod.writer_block({"engine": "junk"}, version="0.1") is None
    monkeypatch.setenv(state_mod.ALLOW_OLD_ENGINE_ENV, "1")
    assert state_mod.writer_block(data, version="1.4.2") is None


def _claimed_by_newer(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data["engine"] = {"last_writer": "99.0", "newest_writer": "99.0", "min_writer": "99.0"}
    path.write_text(json.dumps(data))


def test_too_old_engine_refuses_switch_and_live_writes(ctx):
    from tests.conftest import make_codex_blob
    from acctsw import accounts, switch
    st = ctx.load_state()
    accounts.add(ctx, st, "codex", email="a@x.com", blob=make_codex_blob("a@x.com"))
    _claimed_by_newer(ctx.state_file)
    st = ctx.load_state()
    before = ctx.cred["codex"].get_live()
    with pytest.raises(EngineTooOld):
        switch.switch(ctx, st, "codex", "a@x.com", manual=True)
    with pytest.raises(EngineTooOld):
        ctx.set_live("codex", make_codex_blob("a@x.com"))
    assert ctx.cred["codex"].get_live() == before


def test_too_old_engine_runs_the_stock_tool_instead_of_supervising(ctx, monkeypatch, capsys):
    from acctsw import cli, launcher
    _claimed_by_newer(ctx.state_file)
    calls = []
    monkeypatch.setattr(launcher, "exec_stock", lambda c, tool, args: calls.append((tool, args)) or 0)
    monkeypatch.setattr(launcher, "run", lambda *a, **k: pytest.fail("supervised a newer store"))
    ns = type("NS", (), {"tool": "claude", "args": ["--resume"]})()
    assert cli._cmd_run(ctx, ns) == 0
    assert calls == [("claude", ["--resume"])]
    assert "not supervising" in capsys.readouterr().err
