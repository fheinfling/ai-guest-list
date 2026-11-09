"""Subscription names are validated before any state or credential write."""
from contextlib import contextmanager

import pytest

from acctsw import accounts, bridge, cli
from acctsw.errors import AcctswError
from tests.conftest import make_codex_blob


def seed(ctx, tool="codex", email="me@x", name="Mine"):
    state = ctx.load_state()
    state.upsert_seat(tool, email, name=name)
    state.save()
    return state


@pytest.mark.parametrize("value, expected", [
    ("  My\t new\n seat  ", "My new seat"), ("x" * 32, "x" * 32),
    ("😀" * 32, "😀" * 32), ("mINE", "mINE"), (" \t ", ""),
])
def test_rename_normalizes_and_persists(ctx, value, expected):
    state = seed(ctx)
    before = state.get_seat("codex", "me@x").copy()
    result = accounts.rename(ctx, state, "codex", "me@x", value)
    assert result["name"] == expected
    assert ctx.load_state().get_seat("codex", "me@x")["name"] == expected
    assert {k: v for k, v in result.items() if k != "name"} == {k: v for k, v in before.items() if k != "name"}
    view = accounts.list_seats(ctx.load_state(), "codex")[0]
    assert view["name"] == (expected or "me")
    assert view["custom_name"] == expected


@pytest.mark.parametrize("tool, email, name, error", [
    ("codex", "me@x", "x" * 33, "Keep it to 32 characters."),
    ("bad", "me@x", "Next", "unknown tool: bad"),
    ("codex", "ghost@x", "Next", "unknown seat: codex:ghost@x"),
])
def test_rename_errors_leave_store_untouched(ctx, tool, email, name, error):
    state = seed(ctx)
    before = state.path.read_bytes()
    with pytest.raises(AcctswError) as exc:
        accounts.rename(ctx, state, tool, email, name)
    assert str(exc.value) == error
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("name", [None, 42, []])
def test_validated_name_rejects_non_strings(ctx, name):
    state = seed(ctx)
    before = state.path.read_bytes()
    with pytest.raises(AcctswError, match=r"^Name must be text\.$"):
        accounts.validated_name(state, "codex", "me@x", name)
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("field", ["tool", "email", "name"])
def test_bridge_rename_missing_fields_keep_correlation(ctx, field):
    state = seed(ctx)
    before = state.path.read_bytes()
    message = {"action": "rename", "tool": "codex", "email": "me@x",
               "name": "Next", "rename_request_id": "malformed"}
    del message[field]
    assert bridge.handle(ctx, message) == {
        "ok": False, "error": f"missing field: '{field}'", "rename_request_id": "malformed"}
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("field, value, error", [
    ("tool", "unknown", "unknown tool: unknown"),
    *[("email", value, "Email must be text.") for value in (None, 42, [], {})],
    *[("name", value, "Name must be text.") for value in (None, 42, [])],
])
def test_bridge_rename_bad_values_keep_correlation(ctx, field, value, error):
    state = seed(ctx)
    before = state.path.read_bytes()
    message = {"action": "rename", "tool": "codex", "email": "me@x",
               "name": "Next", "rename_request_id": "malformed"}
    message[field] = value
    assert bridge.handle(ctx, message) == {
        "ok": False, "error": error, "rename_request_id": "malformed"}
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("other_tool, other_name, candidate, display", [
    ("claude", "Other Seat", "  oTHER\t Seat ", "Other Seat"),
    ("codex", "Other Seat", "other seat", "Other Seat"),
    ("claude", "", "OTHER", "other"),
])
def test_clash_compares_other_effective_names_across_tools(ctx, other_tool, other_name, candidate, display):
    seed(ctx)
    state = seed(ctx, other_tool, "other@x", "initial")
    state.get_seat(other_tool, "other@x")["name"] = other_name
    state.save()
    before = state.path.read_bytes()
    with pytest.raises(AcctswError) as exc:
        accounts.rename(ctx, state, "codex", "me@x", candidate)
    assert str(exc.value) == f'"{display}" is already the {other_tool.title()} seat other@x. Pick another name.'
    assert state.path.read_bytes() == before


def test_blank_and_existing_duplicates_never_block_other_actions(ctx):
    seed(ctx, "claude", "other@x", "me")
    state = seed(ctx)
    accounts.rename(ctx, state, "codex", "me@x", "")
    assert accounts.list_seats(state, "codex")[0]["name"] == "me"
    # Old duplicates are kept, including a case-only edit of one's own name.
    state.get_seat("codex", "me@x")["name"] = "Same"
    state.get_seat("claude", "other@x")["name"] = "Same"
    accounts.rename(ctx, state, "codex", "me@x", "sAME")
    assert state.get_seat("claude", "other@x")["name"] == "Same"
    accounts.rename(ctx, state, "codex", "me@x", "Different")


@pytest.mark.parametrize("name", ["taken", "x" * 33])
def test_add_rejects_bad_names_before_writing_credentials(ctx, name):
    seed(ctx, "claude", "other@x", "Taken")
    state = ctx.load_state()
    blob = make_codex_blob("new@x")
    before = state.path.read_bytes()
    with pytest.raises(AcctswError):
        accounts.add(ctx, state, "codex", name=name, email="new@x", blob=blob)
    assert ctx.snapshot_get("codex", "new@x") is None
    assert state.path.read_bytes() == before


def test_add_normalizes_supplied_name_but_omitted_name_keeps_old_behavior(ctx):
    seed(ctx, "claude", "other@x", "New")
    state = ctx.load_state()
    result = accounts.add(ctx, state, "codex", email="new@x", blob=make_codex_blob("new@x"))
    assert result["name"] == "New"  # existing default label, even though it clashes
    result = accounts.add(ctx, state, "codex", email="new@x", blob=make_codex_blob("new@x"), name=" A\t B ")
    assert result["name"] == "A B"
    result = accounts.add(ctx, state, "codex", email="new@x", blob=make_codex_blob("new@x"), name=" ")
    assert accounts.list_seats(state, "codex")[0]["name"] == "new"


@pytest.mark.parametrize("action", ["paste", "import_current", "snapshot"])
@pytest.mark.parametrize("name", ["taken", "x" * 33])
def test_bridge_add_paths_validate_before_side_effects(ctx, action, name):
    seed(ctx, "claude", "other@x", "Taken")
    old = make_codex_blob("old@x")
    ctx.cred["codex"].set_live(old)
    result = bridge.handle(ctx, {"action": action, "tool": "codex", "name": name,
                                 "blob": make_codex_blob("new@x")})
    assert result["ok"] is False
    assert result["error"] == ('"Taken" is already the Claude seat other@x. Pick another name.'
                               if name == "taken" else "Keep it to 32 characters.")
    assert ctx.cred["codex"].get_live() == old
    assert not ctx.load_state().accounts("codex")


def test_bridge_rename_loads_mutates_and_saves_under_lock(ctx, monkeypatch):
    seed(ctx)
    events = []
    locked = False
    original_lock, load = ctx.locked, ctx.load_state

    @contextmanager
    def lock():
        nonlocal locked
        with original_lock():
            locked = True
            events.append("lock")
            yield
            locked = False
            events.append("unlock")

    def checked_load():
        state = load()
        if locked:
            events.append("load")
            save = state.save
            def checked_save():
                assert locked
                events.append("save")
                save()
            state.save = checked_save
        return state

    monkeypatch.setattr(ctx, "locked", lock)
    monkeypatch.setattr(ctx, "load_state", checked_load)
    result = bridge.handle(ctx, {"action": "rename", "tool": "codex", "email": "me@x",
                                 "name": "  Next  ", "rename_request_id": "r1"})
    assert result["ok"] is True
    assert result["rename_request_id"] == "r1"
    assert result["state"]["tools"]["codex"]["seats"][0]["name"] == "Next"
    assert events == ["lock", "load", "save", "unlock"]
    error = bridge.handle(ctx, {"action": "rename", "tool": "codex", "email": "me@x",
                                "name": "x" * 33, "rename_request_id": "r2"})
    assert error == {"ok": False, "error": "Keep it to 32 characters.", "rename_request_id": "r2"}


def test_cli_rename_exit_output_clear_and_help(ctx, monkeypatch, capsys):
    seed(ctx)
    monkeypatch.setattr(cli.Context, "default", classmethod(lambda cls: ctx))
    assert cli.main(["rename", "codex", "me@x", "  Next  "]) == 0
    assert capsys.readouterr().out == "✓ seat renamed — codex:me@x (Next)\n"
    assert cli.main(["rename", "codex", "me@x", ""]) == 0
    assert "(me)" in capsys.readouterr().out
    assert cli.main(["rename", "codex", "ghost@x", "Next"]) == 1
    assert "unknown seat: codex:ghost@x" in capsys.readouterr().err
    assert cli.main(["rename", "codex", "me@x", "x" * 33]) == 1
    assert "Keep it to 32 characters." in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["rename", "codex", "me@x"])
    assert exc.value.code == 2
    assert "rename" in cli.build_parser().format_help()
