from pathlib import Path

import pytest

from acctsw import __version__
from acctsw import enginerestart as er
from acctsw.enginerestart import (
    CHECK_INTERVAL_S,
    RESTART_QUIET_S,
    EngineRestart,
    InputTracker,
    newer_writer,
    restart_args,
    target_engine,
    visible_input,
)

THREAD = "7383292b-d447-4279-b899-1634d45286dd"


def test_constants():
    assert RESTART_QUIET_S == 3900.0
    assert CHECK_INTERVAL_S == 60.0


def test_engine_restart_carries_argv():
    argv = ["/x/acctsw", "run", "claude"]
    exc = EngineRestart(argv)
    assert isinstance(exc, Exception)
    assert exc.argv == argv and exc.argv is not argv


# --- visible_input ------------------------------------------------------------------------------

@pytest.mark.parametrize("seq", [
    b"\x1b[I", b"\x1b[O",                  # focus
    b"\x1b[A", b"\x1b[1;5C", b"\x1b[3~",   # arrows, modified arrows, delete
    b"\x1b[<35;86;2M", b"\x1b[<0;1;1m",    # SGR mouse
    b"\x1b[M !!",                          # legacy mouse
    b"\x1b[32;5;5M",                       # urxvt mouse
    b"\x1bOA", b"\x1bOP",                  # SS3
    b"\x1b]11;rgb:0/0/0\x07",              # OSC, BEL
    b"\x1b]11;rgb:0/0/0\x1b\\",            # OSC, ST
    b"\x1bP>|xterm\x1b\\",                 # DCS
    b"\x1bb",                              # Alt+b
    b"\x1b\r",                             # Alt+Enter
    b"\x1b",                               # lone Esc
])
def test_visible_input_strips_sequences(seq):
    assert visible_input(seq) == b""
    assert visible_input(b"a" + seq) == b"a"


def test_visible_input_keeps_text_around_sequences():
    assert visible_input(b"a\x1b[Ib\x1bOAc\x1b]0;x\x07d\r") == b"abcd\r"


def test_visible_input_keeps_pasted_text_but_not_markers():
    assert visible_input(b"\x1b[200~pasted\ntext\x1b[201~") == b"pasted\ntext"


def test_visible_input_keeps_controls_and_utf8():
    assert visible_input(b"\x7f\x03") == b"\x7f\x03"
    assert visible_input("é".encode()) == "é".encode()


def test_visible_input_drops_a_cut_off_sequence():
    assert visible_input(b"a\x1b[<35;8") == b"a"
    assert visible_input(b"a\x1b]0;tit") == b"a"
    assert visible_input(b"a\x1b[M !") == b"a"


def test_visible_input_empty():
    assert visible_input(b"") == b""


# --- InputTracker -------------------------------------------------------------------------------

def test_tracker_starts_clean():
    t = InputTracker()
    assert t.last_input is None
    assert t.draft is False


@pytest.mark.parametrize("data", [b"\x1b[I", b"\x1b[O", b"\x1b[<35;86;2M", b"\x1b[<0;1;1m",
                                  b"\x1b[M !!", b"\x1b[32;5;5M", b"\x1b[I\x1b[<35;1;1M\x1b[O", b""])
def test_focus_and_mouse_are_not_the_user_typing(data):
    t = InputTracker()
    t.note(data, 5.0)
    assert t.last_input is None
    assert t.draft is False


@pytest.mark.parametrize("data", [b"\x1b[A", b"\x1bOA", b"\x1b[3~", b"\x1b",
                                  b"\x7f", b"\x03", b"\t", b"\r", b"a", b"\x1b[I\x1b[A"])
def test_other_input_updates_last_input(data):
    t = InputTracker()
    t.note(data, 5.0)
    assert t.last_input == 5.0


def test_last_input_keeps_the_latest_real_key():
    t = InputTracker()
    t.note(b"\x1b[A", 1.0)
    t.note(b"\x1b[I", 2.0)
    t.note(b"\x1b[<35;86;2M", 3.0)
    assert t.last_input == 1.0


@pytest.mark.parametrize("data", [b"a", b" ", "é".encode(), b"a\rb", b"a\r\x03",
                                  b"\x1b[Ia"])
def test_printable_text_is_a_draft(data):
    t = InputTracker()
    t.note(data, 1.0)
    assert t.draft is True


@pytest.mark.parametrize("enter", [b"\r", b"text\r", b"\r\x1b[I", b"\x1b[Oab\r"])
def test_enter_clears_the_draft(enter):
    t = InputTracker()
    t.note(b"draft", 1.0)
    t.note(enter, 2.0)
    assert t.draft is False


@pytest.mark.parametrize("data", [b"\x03", b"\x15", b"\x1b[A", b"\x1bOA", b"\x1b[97;1u", b"\x1b", b"\x1b\r"])
def test_any_other_key_may_have_left_text_in_the_box(data):
    # Up-arrow recalls history, Tab completes, kitty mode encodes letters: all look like escapes.
    for start in (b"", b"draft"):
        t = InputTracker()
        t.note(start, 1.0)
        t.note(data, 1.1)
        assert t.draft is True, data


def test_ctrl_j_is_a_newline_in_the_box_not_a_submit():
    t = InputTracker()
    t.note(b"first line\n", 1.0)
    assert t.draft is True


@pytest.mark.parametrize("parts", [[b"line\\\r"], [b"line\\", b"\r"], [b"\\\r"]])
def test_backslash_enter_is_a_newline_in_the_box_not_a_submit(parts):
    t = InputTracker()
    for part in parts:
        t.note(part, 1.0)
    assert t.draft is True


def test_backspacing_a_box_empty_is_no_draft():
    t = InputTracker()
    t.note(b"ab\x7f\x7f", 1.0)
    assert t.draft is False


@pytest.mark.parametrize("line", [b"/resume\r", b"/clear\r", b"/resume abc\r", b"/my-skill\r",
                                  b"/plugin:skill x\r", b"\x1b[A\r", b"/CLEAR\r",
                                  b"\x1b[200~/resume\x1b[201~\r"])
def test_a_submitted_line_that_may_change_conversation_is_remembered(line):
    t = InputTracker()
    t.note(line, 1.0)
    assert t.moved is True
    t.note(b"hello\r", 2.0)
    assert t.moved is True   # for the rest of this child
    t.reset()
    assert t.moved is False


@pytest.mark.parametrize("line", [b"hello\r", b"/status\r", b"/model opus\r", b"/Users/me/x.py fix\r",
                                  b"\r", b"\x1bnew text\r", b"x/resume\r"])
def test_ordinary_lines_stay_in_the_conversation(line):
    t = InputTracker()
    t.note(line, 1.0)
    assert t.moved is False
    assert t.draft is False


def test_reset_is_a_new_childs_empty_box():
    t = InputTracker()
    t.note(b"draft", 1.0)
    t.reset()
    assert t.draft is False
    assert t.last_input is None


@pytest.mark.parametrize("data", [b"\x1b[12;40R", b"\x1b[?62;4c", b"\x1b]11;rgb:0/0/0\x07", b"\x1b[I"])
def test_terminal_replies_are_not_the_user(data):
    t = InputTracker()
    t.note(data, 1.0)
    assert t.draft is False
    assert t.last_input is None


def test_pasted_text_is_a_draft():
    t = InputTracker()
    t.note(b"\x1b[200~pasted\x1b[201~", 1.0)
    assert t.draft is True
    assert t.last_input == 1.0


def test_paste_ending_in_a_newline_is_not_a_submit():
    t = InputTracker()
    t.note(b"\x1b[200~line one\n\x1b[201~", 1.0)
    assert t.draft is True
    t.note(b"\r", 2.0)
    assert t.draft is False


def test_paste_spanning_reads_stays_a_draft():
    t = InputTracker()
    t.note(b"\x1b[200~first", 1.0)
    t.note(b"second\n", 1.01)
    assert t.draft is True
    t.note(b"\x1b[201~", 1.02)
    assert t.draft is True
    t.note(b"\r", 2.0)
    assert t.draft is False


def test_enter_right_after_a_paste_submits():
    t = InputTracker()
    t.note(b"\x1b[200~line\n\x1b[201~\r", 1.0)
    assert t.draft is False


@pytest.mark.parametrize("whole", [b"\x1b[<35;86;2M", b"\x1b[I", b"\x1b[O", b"\x1b[M !!"])
def test_split_mouse_or_focus_is_not_typed_text(whole):
    for cut in range(1, len(whole)):
        t = InputTracker()
        t.note(whole[:cut], 1.0)
        t.note(whole[cut:], 1.01)
        assert t.draft is False, (whole, cut)


@pytest.mark.parametrize("whole,draft", [(b"\x1b[A", True), (b"\x1bOA", True),
                                         (b"\x1b]11;rgb:0/0/0\x07", False), (b"\x1b[200~", False)])
def test_split_sequence_counts_once_whole(whole, draft):
    for cut in range(1, len(whole)):
        t = InputTracker()
        t.note(whole[:cut], 1.0)
        t.note(whole[cut:], 1.01)
        assert t.draft is draft, (whole, cut)


def test_split_paste_marker_still_opens_the_paste():
    t = InputTracker()
    t.note(b"\x1b[20", 1.0)
    t.note(b"0~line\n", 1.01)
    assert t.draft is True


def test_text_after_a_real_esc_press_counts():
    # The Esc key alone is a cut-off sequence for a moment, not a prefix for the next key forever.
    t = InputTracker()
    t.note(b"\x1b", 1.0)
    t.note(b"abc", 5.0)
    assert t.draft is True
    assert t.last_input == 5.0


def test_runaway_string_sequence_is_forgotten():
    t = InputTracker()
    t.note(b"\x1b]" + b"x" * (er._MAX_CARRY + 1), 1.0)
    t.note(b"abc", 1.01)
    assert t.draft is True


# --- newer_writer -------------------------------------------------------------------------------

def test_newer_writer_only_when_strictly_newer():
    data = {"engine": {"newest_writer": "1.7.0"}}
    assert newer_writer(data, "1.6.1") == "1.7.0"
    assert newer_writer(data, "1.7.0") is None
    assert newer_writer(data, "1.7") is None
    assert newer_writer(data, "1.8.0") is None


def test_newer_writer_defaults_to_this_engine():
    assert newer_writer({"engine": {"newest_writer": __version__}}) is None
    assert newer_writer({"engine": {"newest_writer": "999.0"}}) == "999.0"


@pytest.mark.parametrize("data", [
    {}, None, [], "x", {"engine": None}, {"engine": "1.7"}, {"engine": []}, {"engine": {}},
    {"engine": {"newest_writer": None}}, {"engine": {"newest_writer": 2}},
    {"engine": {"newest_writer": "garbage"}}, {"engine": {"newest_writer": ""}},
])
def test_newer_writer_tolerates_malformed_state(data):
    assert newer_writer(data, "1.6.1") is None


def test_newer_writer_unknown_own_version():
    assert newer_writer({"engine": {"newest_writer": "1.7"}}, "dev") is None


# --- target_engine ------------------------------------------------------------------------------

def _probe(result):
    return lambda exe: result


def test_target_engine_strictly_newer(tmp_path):
    exe = tmp_path / "acctsw"
    assert target_engine("1.6.1", exe=exe, probe=_probe({"version": "1.6.2"})) == str(exe)
    assert target_engine("1.6.1", exe=str(exe), probe=_probe({"version": "2.0"})) == str(exe)


@pytest.mark.parametrize("found", [
    None, {}, [], {"version": None}, {"version": "garbage"}, {"version": 2},
    {"version": "1.6.1"}, {"version": "1.6.1.0"}, {"version": "1.6.1-dev"}, {"version": "1.6.0"},
    {"version": "0.9"},
])
def test_target_engine_refuses_missing_unknown_equal_older(tmp_path, found):
    assert target_engine("1.6.1", exe=tmp_path / "acctsw", probe=_probe(found)) is None


def test_target_engine_unknown_own_version(tmp_path):
    assert target_engine("dev", exe=tmp_path / "acctsw", probe=_probe({"version": "2.0"})) is None


def test_target_engine_no_loop_once_on_the_new_engine(tmp_path):
    exe = tmp_path / "acctsw"
    found = {"version": "1.7.0"}
    assert target_engine("1.6.1", exe=exe, probe=_probe(found)) == str(exe)
    assert target_engine("1.7.0", exe=exe, probe=_probe(found)) is None


def test_target_engine_default_path_is_the_local_bin_wrapper():
    seen = []

    def probe(exe):
        seen.append(str(exe))
        return {"version": "999.0"}

    # conftest points Path.home at a temp dir.
    expected = str(Path.home() / ".local" / "bin" / "acctsw")
    assert target_engine(probe=probe) == expected
    assert seen == [expected]


def test_target_engine_default_probe_handles_a_missing_wrapper(tmp_path):
    assert target_engine("1.0", exe=tmp_path / "nope") is None


# --- restart_args: claude -----------------------------------------------------------------------

def test_claude_real_invocation():
    args = ["--dangerously-skip-permissions", "--resume", THREAD, "--chrome"]
    assert restart_args("claude", args, THREAD) == [
        "--dangerously-skip-permissions", "--chrome", "--resume", THREAD]


def test_claude_fresh_session_gets_the_resume():
    assert restart_args("claude", [], THREAD) == ["--resume", THREAD]
    assert restart_args("claude", ["--chrome"], THREAD) == ["--chrome", "--resume", THREAD]


def test_claude_fresh_session_without_thread_keeps_flags_only():
    assert restart_args("claude", [], None) == []
    assert restart_args("claude", ["--chrome", "--model", "opus"], None) == [
        "--chrome", "--model", "opus"]


@pytest.mark.parametrize("old", [
    ["--resume", "old"], ["-r", "old"], ["--resume=old"], ["-r=old"], ["--resume"], ["-r"],
    ["--session-id", "old"], ["--session-id=old"], ["--continue"], ["-c"],
    ["--continue", "--session-id", "old"],
])
def test_claude_old_resume_options_are_replaced(old):
    assert restart_args("claude", [*old, "--chrome"], THREAD) == ["--chrome", "--resume", THREAD]
    assert restart_args("claude", ["--chrome", *old], THREAD) == ["--chrome", "--resume", THREAD]


@pytest.mark.parametrize("old", [
    ["--resume", "old"], ["-r", "old"], ["--resume=old"], ["--resume"], ["-r"],
    ["--session-id", "old"], ["--session-id=old"], ["--continue"], ["-c"],
])
def test_claude_resume_without_thread_is_refused(old):
    assert restart_args("claude", old, None) is None


@pytest.mark.parametrize("flag", sorted(er._CLAUDE_FLAGS))
def test_claude_every_boolean_flag_is_kept(flag):
    assert restart_args("claude", [flag], THREAD) == [flag, "--resume", THREAD]


@pytest.mark.parametrize("flag", sorted(er._CLAUDE_VALUES))
def test_claude_every_value_flag_is_kept(flag):
    assert restart_args("claude", [flag, "v"], THREAD) == [flag, "v", "--resume", THREAD]
    assert restart_args("claude", [flag], THREAD) is None
    assert restart_args("claude", [flag, "--chrome"], THREAD) is None


def test_claude_value_flag_forms():
    assert restart_args("claude", ["--model=opus"], None) == ["--model=opus"]
    assert restart_args("claude", ["--tools", ""], None) == ["--tools", ""]
    assert restart_args("claude", ["-n", "work", "--chrome"], None) == ["-n", "work", "--chrome"]


def test_claude_boolean_flag_with_a_value_is_refused():
    assert restart_args("claude", ["--chrome=yes"], THREAD) is None


@pytest.mark.parametrize("args", [
    ["fix the bug"],
    ["--chrome", "fix the bug"],
    ["fix the bug", "--chrome"],
    ["--resume", "old", "fix the bug"],
    ["--continue", "fix the bug"],
    ["--session-id", "old", "fix the bug"],
    ["--model", "opus", "fix the bug"],
    ["--add-dir", "/a", "fix the bug"],   # variadic in claude: can't tell it from a prompt
    [""],
    ["-"],
    ["--"],
    ["--", "fix the bug"],
    ["--chrome", "--", "fix the bug"],
    ["-p"], ["--print"], ["-p", "hi"], ["--chrome", "--print"],
    ["--fork-session"], ["--resume", "old", "--fork-session"], ["-c", "--fork-session"],
    ["--key", "work"], ["--key=work"],
    ["--unknown"], ["--unknown=x"], ["-x"],
    ["mcp", "list"], ["agents"],
    ["--session-id"], ["--session-id", "--chrome"],
    ["-rold"], ["-cr", "old"], ["--continue=x"],
    ["--bg"], ["--background"], ["--desktop"], ["--cloud"], ["--environment", "x"],
    ["--from-pr"], ["--teleport"], ["--remote-control"], ["-w"], ["--worktree", "x"], ["--tmux"],
    ["--file", "a:b"], ["-d"], ["--debug"], ["--debug", "hooks"], ["--prompt-suggestions"],
    ["-h"], ["--help"], ["-v"], ["--version"],
    ["--output-format", "json"], ["--input-format", "text"], ["--json-schema", "{}"],
    ["--max-budget-usd", "1"], ["--permission-prompts", "none"],
    ["--no-session-persistence"], ["--replay-user-messages"],
])
@pytest.mark.parametrize("thread", [THREAD, None])
def test_claude_refused(args, thread):
    assert restart_args("claude", args, thread) is None


# --- restart_args: codex ------------------------------------------------------------------------

def test_codex_exec_job_is_refused():
    assert restart_args("codex", ["exec", "-m", "x", "-s", "read-only", "prompt"], THREAD) is None
    assert restart_args("codex", ["exec", "-m", "x", "-s", "read-only", "prompt"], None) is None


def test_codex_fresh_session_gets_the_resume():
    assert restart_args("codex", [], THREAD) == ["resume", THREAD]
    assert restart_args("codex", ["-m", "x", "--search"], THREAD) == [
        "resume", THREAD, "-m", "x", "--search"]


def test_codex_fresh_session_without_thread_keeps_flags_only():
    assert restart_args("codex", [], None) == []
    assert restart_args("codex", ["-m", "x", "--search"], None) == ["-m", "x", "--search"]


@pytest.mark.parametrize("args", [
    ["resume", "old", "-m", "x"],
    ["-m", "x", "resume", "old"],
    ["-m", "x", "resume", "--last"],
    ["resume", "--last", "-m", "x"],
    ["resume", "-m", "x"],
    ["resume", "--all", "-m", "x"],
    ["resume", "old", "--all", "--include-non-interactive", "-m", "x"],
])
def test_codex_old_resume_becomes_the_thread(args):
    assert restart_args("codex", args, THREAD) == ["resume", THREAD, "-m", "x"]


@pytest.mark.parametrize("args", [
    ["resume"], ["resume", "--last"], ["resume", "--all"], ["resume", "old"], ["-m", "x", "resume"],
])
def test_codex_resume_without_thread_is_refused(args):
    assert restart_args("codex", args, None) is None


@pytest.mark.parametrize("flag", sorted(er._CODEX_FLAGS))
def test_codex_every_boolean_flag_is_kept(flag):
    assert restart_args("codex", [flag], THREAD) == ["resume", THREAD, flag]


@pytest.mark.parametrize("flag", sorted(er._CODEX_VALUES))
def test_codex_every_value_flag_is_kept(flag):
    assert restart_args("codex", [flag, "v"], THREAD) == ["resume", THREAD, flag, "v"]
    assert restart_args("codex", [flag], THREAD) is None
    assert restart_args("codex", [flag, "--search"], THREAD) is None


def test_codex_attached_value_forms():
    assert restart_args("codex", ["--model=x", "-sread-only", "-c=a=1"], None) == [
        "--model=x", "-sread-only", "-c=a=1"]


def test_short_flags_mean_different_things_per_tool():
    # -p is print for claude, profile for codex; -c continue for claude, config for codex.
    assert restart_args("claude", ["-p", "x"], THREAD) is None
    assert restart_args("codex", ["-p", "x"], THREAD) == ["resume", THREAD, "-p", "x"]
    assert restart_args("claude", ["-c"], THREAD) == ["--resume", THREAD]
    assert restart_args("codex", ["-c"], THREAD) is None


@pytest.mark.parametrize("sub", ["exec", "e", "login", "logout", "review", "mcp", "fork", "app",
                                 "update", "doctor", "apply", "queue", "cloud", "help"])
@pytest.mark.parametrize("thread", [THREAD, None])
def test_codex_other_subcommands_are_refused(sub, thread):
    assert restart_args("codex", [sub], thread) is None
    assert restart_args("codex", ["-m", "x", sub], thread) is None


@pytest.mark.parametrize("args", [
    ["fix the bug"],
    ["--search", "fix the bug"],
    ["fix the bug", "resume"],
    ["resume", "old", "fix the bug"],
    ["resume", "--last", "fix the bug"],      # with --last codex reads this word as the prompt
    ["resume", "fix the bug", "--last"],
    ["resume", "resume", "fix"],
    [""],
    ["-"],
    ["--"],
    ["--", "fix the bug"],
    ["resume", "old", "--"],
    ["--last"], ["--all"], ["--include-non-interactive"],   # only exist after `resume`
    ["-i", "a.png"], ["--image", "a.png"], ["--image=a.png"], ["-ia.png"],
    ["resume", "old", "-i", "a.png"],
    ["--worktree"], ["resume", "old", "--worktree"],
    ["--key", "work"], ["--key=work"],
    ["--unknown"], ["--unknown=x"], ["-x"], ["--search=x"],
    ["-h"], ["--help"], ["-V"], ["--version"],
])
@pytest.mark.parametrize("thread", [THREAD, None])
def test_codex_refused(args, thread):
    assert restart_args("codex", args, thread) is None


# --- restart_args: shared -----------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_a_thread_that_looks_like_a_flag_is_refused(tool):
    assert restart_args(tool, [], "--evil") is None


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_an_empty_thread_is_no_thread(tool):
    assert restart_args(tool, [], "") == []


def test_unknown_tool_is_refused():
    assert restart_args("gemini", [], THREAD) is None


@pytest.mark.parametrize("tool,args", [("claude", ["--chrome", "--resume", "x"]),
                                       ("codex", ["-m", "x", "resume", "y"])])
def test_input_args_are_not_mutated(tool, args):
    before = list(args)
    restart_args(tool, args, THREAD)
    assert args == before


# --- idle gate ----------------------------------------------------------------------------------

def test_quiet_outlasts_the_longest_scheduled_wakeup():
    assert er.RESTART_QUIET_S > 3600


def test_engine_restart_carries_the_billed_seat():
    assert EngineRestart(["x"], seat="a@b").seat == "a@b"
    assert EngineRestart(["x"]).seat is None


@pytest.mark.parametrize("text,seconds", [("00:05", 5), ("02:03", 123), ("01:00:00", 3600),
                                          ("2-01:00:01", 176401)])
def test_elapsed_parses_ps_times(text, seconds):
    assert er._elapsed(text) == seconds


def _ps(table):
    class Done:
        stdout = table
    return lambda *a, **k: Done()


def test_servers_from_the_childs_startup_are_not_work():
    # supervisor 1 > child 2 (1h) > MCP 3 (59m59s) > its helper 4 (59m58s); stranger 9 is elsewhere.
    table = "1 0 02:00:00\n2 1 01:00:00\n3 2 59:59\n4 3 59:58\n9 8 00:01\n"
    assert er.has_late_descendants(1, run=_ps(table)) is False


@pytest.mark.parametrize("line", ["3 2 10:00", "5 4 00:05"])
def test_anything_started_later_is_the_sessions_own_work(line):
    table = "1 0 02:00:00\n2 1 01:00:00\n4 2 59:59\n" + line + "\n"
    assert er.has_late_descendants(1, run=_ps(table)) is True


def test_an_unreadable_process_table_defers():
    def boom(*a, **k):
        raise OSError("no ps")
    assert er.has_late_descendants(1, run=boom) is True
    assert er.has_late_descendants(1, run=_ps("2 1 nonsense\n")) is True
