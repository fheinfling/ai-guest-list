"""Popover sign-in names reach the isolated engine; all login and store IO is fake."""
import pytest

from acctsw import accounts, cli
from tests.test_authflows import blob, fakes


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_signin_name_is_validated_and_saved_under_the_capture_lock(ctx, fakes, monkeypatch, tool):
    email = "seat@example.com"
    fakes.owner, fakes.login_blob = email, blob(tool, email)
    save = type(ctx.load_state()).save
    writes = []

    def checked_save(state):
        seat = state.get_seat(tool, email)
        if seat:
            assert ctx._store_lock_held()
            writes.append(seat.get("name"))
        return save(state)

    monkeypatch.setattr(type(ctx.load_state()), "save", checked_save)
    assert cli.main(["signin", tool, "--name", "  Example\t Seat  "]) == 0
    assert writes and set(writes) == {"Example Seat"}
    assert ctx.load_state().get_seat(tool, email)["name"] == "Example Seat"
    assert ctx.load_state().active(tool) == email


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_an_impossible_name_fails_before_the_browser_opens(ctx, fakes, tool):
    before = ctx.state_file.read_bytes() if ctx.state_file.exists() else None
    assert cli.main(["signin", tool, "--name", "x" * 33]) == 1
    assert fakes.logins == [] and fakes.homes == []
    assert (ctx.state_file.read_bytes() if ctx.state_file.exists() else None) == before


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_a_taken_name_keeps_the_login_and_says_to_rename(ctx, fakes, capsys, tool):
    email = "seat@example.com"
    fakes.owner, fakes.login_blob = email, blob(tool, email)
    other_tool = "claude" if tool == "codex" else "codex"
    accounts.add(ctx, ctx.load_state(), other_tool, name="Taken", email="other@example.com",
                 blob=blob(other_tool, "other@example.com"))
    assert cli.main(["signin", tool, "--name", "taken"]) == 0
    state = ctx.load_state()
    assert state.active(tool) == email
    assert (state.get_seat(tool, email).get("name") or "").casefold() != "taken"
    assert ctx.snapshot_get(tool, email) == blob(tool, email)
    out = capsys.readouterr().out
    assert "name not applied" in out and "already the" in out


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_signin_without_name_preserves_an_existing_label(ctx, fakes, tool):
    email = "seat@example.com"
    fakes.owner, fakes.login_blob = email, blob(tool, email)
    accounts.add(ctx, ctx.load_state(), tool, name="Existing", email=email, blob=fakes.login_blob)
    assert cli.main(["signin", tool]) == 0
    assert ctx.load_state().get_seat(tool, email)["name"] == "Existing"
