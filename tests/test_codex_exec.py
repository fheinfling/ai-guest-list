"""`codex exec` jobs continue NON-interactively after a seat hop.

The field bug: a hop relaunched every codex child as the TUI's `codex resume <thread>`, so an
unattended `cx exec` job opened a TUI nobody was watching and hung forever. A prompt piped on stdin
hung too: on a PTY the pipe's EOF never reached codex.
"""
import json
import os
import signal
import sys
import time
from datetime import timedelta

import pytest

from acctsw import codexexec as X
from acctsw import launcher as L
from acctsw import paths as P
from acctsw import session as S
from acctsw.errors import AcctswError
from acctsw.launcher import build_cmd, run
from acctsw.util import iso, now
from tests.test_launcher import REFILL_BANNER, _appender, _codex_sessions, _soon, _two_codex
from tests.test_rollout import task_complete_error_line, write_rollout
from tests.test_usage import codex_ok_body, fake_get

THREAD = "019a0000-1111-7222-8333-444455556666"


# --- argv: recognise the job, rebuild it as `exec … resume <thread> <prompt>` -------------------

def test_relaunch_keeps_every_exec_option_and_drops_prompt_and_images():
    job = X.parse(["exec", "-s", "workspace-write", "--skip-git-repo-check", "-m", "gpt-x",
                   "-c", "model_reasoning_effort=xhigh", "-C", "sub", "--add-dir", "/tmp/s",
                   "-i", "a.png", "b.png", "--json", "-o", "last.txt", "do the thing"])
    assert job.resumable and job.command == "" and job.cwd == "sub"
    assert job.resume_argv("/bin/codex", THREAD) == [
        "/bin/codex", "exec", "-s", "workspace-write", "--skip-git-repo-check", "-m", "gpt-x",
        "-c", "model_reasoning_effort=xhigh", "-C", "sub", "--add-dir", "/tmp/s", "--json",
        "-o", "last.txt", "resume", THREAD, X.CONTINUE_PROMPT]


def test_root_options_and_the_e_alias_and_attached_values():
    job = X.parse(["-m", "root-model", "--config=a=1", "-Cdir", "e", "--model=gpt-x",
                   "-s=read-only", "--image=a.png", "-"])
    assert job.resumable and job.cwd == "dir"
    assert job.resume_argv("codex", THREAD) == [
        "codex", "-m", "root-model", "--config=a=1", "-Cdir", "exec", "--model=gpt-x",
        "-s=read-only", "resume", THREAD, X.CONTINUE_PROMPT]
    # The paid seat names its own model: every user model option goes, ours goes right after exec.
    assert job.resume_argv("codex", THREAD, drop_model=True, extra=["--model", "paid"]) == [
        "codex", "--config=a=1", "-Cdir", "exec", "--model", "paid", "-s=read-only",
        "resume", THREAD, X.CONTINUE_PROMPT]


def test_an_exec_resume_keeps_its_thread_and_global_options_after_the_subcommand():
    job = X.parse(["exec", "--skip-git-repo-check", "resume", THREAD, "go on", "--json",
                   "-i", "x.png"])
    assert job.command == "resume" and job.thread == THREAD and job.resumable
    assert job.resume_argv("codex", job.thread) == [
        "codex", "exec", "--skip-git-repo-check", "--json", "resume", THREAD, X.CONTINUE_PROMPT]
    last = X.parse(["exec", "resume", "--last", "go on"])
    assert last.resumable and last.thread is None          # the thread comes from the rollout
    fork = X.parse(["exec", "fork", THREAD, "branch off"])
    assert fork.resumable and fork.thread is None          # a fork writes a NEW thread


def test_option_values_follow_clap():
    # Exec-level images are greedy (num_args 1..): clap reads `resume` here as another image.
    assert X.parse(["exec", "-i", "a.png", "resume", THREAD]).command == ""
    # A subcommand's image takes exactly one value; the next word is the session id.
    assert X.parse(["exec", "resume", "-i", "x.png", THREAD, "go"]).thread == THREAD
    assert X.parse(["exec", "-C=sub", "p"]).cwd == "sub"
    assert X.parse(["exec", "--cd=sub", "p"]).cwd == "sub"


@pytest.mark.parametrize("args, why", [
    (["exec", "--ephemeral", "p"], "--ephemeral"),
    (["exec", "--worktree", "p"], "--worktree"),
    (["--worktree", "exec", "p"], "--worktree"),
    (["exec", "review", "--uncommitted"], "review"),
    (["exec", "--frobnicate", "p"], "--frobnicate"),
    (["exec", "resume"], "does not name"),
    (["--brand-new-root-flag", "x", "exec", "p"], "--brand-new-root-flag"),
    (["exec", "--help"], "help"),
])
def test_jobs_that_cannot_be_resumed_say_why(args, why):
    job = X.parse(args)
    assert job is not None and not job.resumable and why in job.blocker
    with pytest.raises(AcctswError):
        job.resume_argv("codex", THREAD)


@pytest.mark.parametrize("args", [
    [], ["resume", THREAD], ["fix the exec path"], ["--", "exec"], ["-m", "gpt", "do it"],
    ["-h"], ["--unknown", "prompt"],
])
def test_interactive_invocations_are_not_exec_jobs(args):
    assert X.parse(args) is None


def test_unknown_root_option_after_a_dash_dash_value_still_marks_the_job():
    job = X.parse(["-c", "--", "--unknown", "exec", "--", "p"])
    assert job is not None and not job.resumable and "--unknown" in job.blocker


def test_a_known_option_missing_its_value_is_named():
    assert "-o option has no value" in X.parse(["exec", "p", "-o"]).blocker


def test_a_prompt_after_double_dash_is_never_a_subcommand():
    job = X.parse(["exec", "--json", "--", "resume", "that old idea"])
    assert job.command == "" and job.resumable
    assert job.resume_argv("codex", THREAD)[-3:] == ["resume", THREAD, X.CONTINUE_PROMPT]


def test_resume_argv_refuses_without_a_thread():
    with pytest.raises(AcctswError):
        X.parse(["exec", "p"]).resume_argv("codex", None)


# --- pipe_spawn: no PTY for an unattended child -------------------------------------------------

@pytest.fixture
def stdio(tmp_path, monkeypatch):
    """Real fds for the supervisor's stdin/stdout/stderr, so pipe_spawn copies to files."""
    opened = []

    def install(stdin_bytes=b""):
        (tmp_path / "in").write_bytes(stdin_bytes)
        files = {name: open(tmp_path / name, mode) for name, mode in
                 (("in", "rb"), ("out", "wb"), ("err", "wb"))}
        opened.extend(files.values())
        monkeypatch.setattr(sys, "stdin", files["in"])
        monkeypatch.setattr(sys, "stdout", files["out"])
        monkeypatch.setattr(sys, "stderr", files["err"])
        return lambda: tuple((tmp_path / n).read_bytes() for n in ("out", "err"))
    yield install
    monkeypatch.undo()
    for f in opened:
        f.close()


def _py(code):
    return [sys.executable, "-c", code]


def test_piped_prompt_reaches_the_child_with_its_eof(stdio):
    read = stdio(b"line one\nline two\n")
    seen = []
    rc = L.pipe_spawn(_py("import sys; d = sys.stdin.read(); sys.stdout.write(d.upper()); "
                          "sys.stderr.write('progress\\n'); sys.exit(3)"),
                      lambda chunk: seen.append(chunk) and False, tick_interval=0.05)
    out, err = read()
    assert rc == 3
    assert out == b"LINE ONE\nLINE TWO\n"        # byte-exact: no CRLF, no echo of the input
    assert err == b"progress\n"                  # stderr stays stderr
    assert b"".join(seen).count(b"progress") == 1 and b"LINE ONE" in b"".join(seen)


def test_a_resumed_job_reads_dev_null_not_the_consumed_stdin(stdio):
    read = stdio(b"the original prompt\n")
    rc = L.pipe_spawn(_py("import sys; print(repr(sys.stdin.read()))"), lambda _: False,
                      stdin_null=True, tick_interval=0.05)
    assert rc == 0
    assert read()[0] == b"''\n"


def test_stop_drains_a_child_that_floods_while_shutting_down(stdio):
    """SIGTERM handling that writes more than a pipe holds must not wedge the stop path."""
    stdio()
    child = ("import signal, sys, time\n"
             "def bye(*_):\n"
             "    sys.stderr.write('x' * 1_000_000); sys.stderr.flush(); sys.exit(0)\n"
             "signal.signal(signal.SIGTERM, bye)\n"
             "print('ready', flush=True)\n"
             "time.sleep(60)\n")
    ticks = []
    started = time.monotonic()
    rc = L.pipe_spawn(_py(child), lambda chunk: b"ready" in chunk,
                      on_tick=lambda: ticks.append(1) and False, tick_interval=0.05)
    assert rc == 0                               # it exited on its own, not by SIGKILL
    assert time.monotonic() - started < 4.5      # well inside the 5s SIGTERM grace


def test_heartbeat_stops_a_silent_child(stdio):
    stdio()
    rc = L.pipe_spawn(_py("import time; time.sleep(60)"), lambda _: False,
                      on_tick=lambda: True, tick_interval=0.05)
    assert rc == -signal.SIGTERM


# --- run(): a hop carries an exec job over with `exec … resume <thread> <prompt>` ----------------

def _exec_rollout(ctx, cwd, *, thread=THREAD):
    path = write_rollout(_codex_sessions(ctx), thread=thread, cwd=cwd)
    head, rest = path.read_text().split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update(originator="codex_exec", source="exec")
    path.write_text(json.dumps(meta) + "\n" + rest)
    return path


class ExecSpawn:
    """Scripted children, like test_launcher.FakeSpawn, that also record the stdin choice."""

    def __init__(self, scripts):
        self.scripts, self.calls, self.stdin_null, self.stops = scripts, [], [], 0

    def __call__(self, argv, on_output, on_tick=None, *, env=None, stdin_null=False):
        out, status, hooks = self.scripts[len(self.calls)]
        self.calls.append(list(argv))
        self.stdin_null.append(stdin_null)
        if out and on_output(out):
            self.stops += 1
            return status
        for hook in hooks:
            if isinstance(hook, tuple):    # ("exit", act): act, then quit before the heartbeat
                hook[1]()
                return status
            if callable(hook):
                hook()
            if on_tick is not None and on_tick():
                self.stops += 1
                return status
        return status


def _healthy_get(*_args, **_kw):
    return 200, codex_ok_body(primary=10.0, secondary=10.0)


ARGS = ["exec", "-s", "workspace-write", "--skip-git-repo-check", "-m", "gpt-x", "fix it"]
RESUMED = ["exec", "-s", "workspace-write", "--skip-git-repo-check", "-m", "gpt-x",
           "resume", THREAD, X.CONTINUE_PROMPT]


def test_limit_hop_resumes_the_exec_job_non_interactively(ctx, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    path = _exec_rollout(ctx, os.getcwd())
    spawn = ExecSpawn([
        (None, 1, [_appender(path, task_complete_error_line(timestamp=_soon()))]),
        (None, 0, []),
    ])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=lambda m: None) == 0
    exe = build_cmd(ctx, "codex", [])[0]
    assert spawn.calls == [[exe, *ARGS], [exe, *RESUMED]]
    assert spawn.stdin_null == [False, True]
    assert ctx.load_state().active("codex") == "b@x.com"
    assert not list(ctx.data_dir.glob("thread-codex-*.json"))   # the claim ends with the job


def test_limit_written_just_before_exit_is_still_read_and_resumed(ctx, monkeypatch, tmp_path):
    """codex exec writes its limit line and quits inside one heartbeat; the exit path reads it."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    path = _exec_rollout(ctx, os.getcwd())
    spawn = ExecSpawn([
        (None, 1, [("exit", _appender(path, task_complete_error_line(timestamp=_soon())))]),
        (None, 0, []),
    ])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=lambda m: None) == 0
    assert spawn.calls[1][1:] == RESUMED


def test_manual_switch_resumes_the_exec_job_non_interactively(ctx, monkeypatch, tmp_path):
    from acctsw import bridge
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    path = _exec_rollout(ctx, os.getcwd())

    def click():
        _appender(path, '{"type":"event_msg","payload":{"type":"agent_message"}}')()
        assert bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "b@x.com"})["ok"]

    spawn = ExecSpawn([(None, -15, [None, click]), (None, 0, [])])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=lambda m: None) == 0
    assert spawn.calls[1][1:] == RESUMED and spawn.stdin_null == [False, True]


def test_a_sibling_jobs_claimed_thread_is_never_resumed(ctx, monkeypatch, tmp_path):
    """Two exec workers in one repo: the other supervisor's claim rules its thread out."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(S, "_proc_start", lambda pid: "same start")
    _two_codex(ctx)
    theirs = _exec_rollout(ctx, os.getcwd(), thread="019a-theirs")
    mine = _exec_rollout(ctx, os.getcwd())
    sibling = os.getppid()
    (ctx.data_dir / f"thread-codex-{sibling}.json").write_text(json.dumps(
        {"pid": sibling, "process_start": "same start", "thread": "019a-theirs"}))

    def both_write():
        _appender(theirs, '{"type":"event_msg","payload":{"type":"agent_message"}}')()
        _appender(mine, task_complete_error_line(timestamp=_soon()))()

    spawn = ExecSpawn([(None, 1, [both_write]), (None, 0, [])])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=lambda m: None) == 0
    assert spawn.calls[1][1:] == RESUMED
    assert (ctx.data_dir / f"thread-codex-{sibling}.json").exists()   # never touch theirs


def test_unidentified_thread_never_kills_the_job_or_opens_a_tui(ctx, monkeypatch, tmp_path):
    """A trusted hard banner with no rollout to say which thread is ours: the job keeps running."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    msgs = []
    spawn = ExecSpawn([(REFILL_BANNER.encode() + b"\n", 1, [None, None])])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=msgs.append) == 1
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert sum("can't move to another seat" in m for m in msgs) == 1


@pytest.mark.parametrize("extra", [["--ephemeral"], ["--worktree"], ["--frobnicate"]])
def test_non_resumable_job_keeps_its_seat_and_its_exit_status(ctx, monkeypatch, tmp_path, extra):
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    path = _exec_rollout(ctx, os.getcwd())
    msgs = []
    spawn = ExecSpawn([
        (None, 1, [_appender(path, task_complete_error_line(timestamp=_soon())), None, None]),
    ])
    assert run(ctx, "codex", ["exec", *extra, "p"], spawn=spawn, get=_healthy_get,
               notify=msgs.append) == 1
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert any("can't move to another seat" in m for m in msgs)


def test_exit_safety_net_does_not_relaunch_an_unidentified_exec_job(ctx, monkeypatch, tmp_path):
    """The usage endpoint confirms the seat is out after a plain non-zero exit — but with no thread
    to resume, relaunching could only restart the job or open a TUI."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    reset = iso(now() + timedelta(hours=3))
    get = fake_get({P.CODEX_USAGE_URL: (200, codex_ok_body(primary=100.0, p_reset=reset))})
    spawn = ExecSpawn([(b"stream error\n", 1, [])])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=get, notify=lambda m: None) == 1
    assert len(spawn.calls) == 1


def test_real_spawn_is_swapped_for_pipes_only_for_exec(ctx, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    used = []
    monkeypatch.setattr(L, "pipe_spawn", lambda argv, *a, **kw: used.append("pipe") or 0)
    assert run(ctx, "codex", ["exec", "p"], notify=lambda m: None) == 0
    assert used == ["pipe"]


GROW = '{"type":"event_msg","payload":{"type":"agent_message"}}'


def test_only_this_jobs_exec_rollout_is_attached(ctx, monkeypatch, tmp_path):
    """A TUI session in the same repo and an exec job elsewhere are moving too; neither is ours."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    tui = write_rollout(_codex_sessions(ctx), thread="019a-tui", cwd=os.getcwd())
    elsewhere = _exec_rollout(ctx, str(tmp_path / "other"), thread="019a-elsewhere")
    mine = _exec_rollout(ctx, os.getcwd())

    def all_write():
        _appender(tui, GROW)()
        _appender(elsewhere, GROW)()
        _appender(mine, task_complete_error_line(timestamp=_soon()))()

    spawn = ExecSpawn([(None, 1, [all_write]), (None, 0, [])])
    assert run(ctx, "codex", ARGS, spawn=spawn, get=_healthy_get, notify=lambda m: None) == 0
    assert spawn.calls[1][1:] == RESUMED


def test_a_resumed_job_follows_its_own_thread_id(ctx, monkeypatch, tmp_path):
    """`exec resume <id>` knows its thread: an unclaimed exec sibling in the repo is irrelevant."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    sibling = _exec_rollout(ctx, os.getcwd(), thread="019a-sibling")
    mine = _exec_rollout(ctx, os.getcwd())

    def both_write():
        _appender(sibling, GROW)()
        _appender(mine, task_complete_error_line(timestamp=_soon()))()

    spawn = ExecSpawn([(None, 1, [both_write]), (None, 0, [])])
    assert run(ctx, "codex", ["exec", "resume", THREAD, "keep going"], spawn=spawn,
               get=_healthy_get, notify=lambda m: None) == 0
    assert spawn.calls[1][1:] == ["exec", "resume", THREAD, X.CONTINUE_PROMPT]


def test_a_resumed_job_ignores_limits_in_a_siblings_rollout(ctx, monkeypatch, tmp_path):
    """Its own thread id decides which file is ours — not "newest exec file in this repo"."""
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    sibling = _exec_rollout(ctx, os.getcwd(), thread="019a-sibling")
    mine = _exec_rollout(ctx, os.getcwd())
    bodies = [codex_ok_body(primary=100.0, p_reset=iso(now() + timedelta(hours=3)))]

    def out(*_args, **_kw):   # a probe would find this seat out; any landing seat is fine
        return 200, (bodies.pop(0) if bodies else codex_ok_body(primary=10.0, secondary=10.0))

    def both_write():
        _appender(mine, GROW, bump=1.0)()
        _appender(sibling, task_complete_error_line(timestamp=_soon()))()

    spawn = ExecSpawn([(None, 0, [both_write, None])])
    assert run(ctx, "codex", ["exec", "resume", THREAD, "keep going"], spawn=spawn, get=out,
               notify=lambda m: None) == 0
    assert len(spawn.calls) == 1 and spawn.stops == 0


def test_manual_switch_never_restarts_a_non_resumable_job(ctx, monkeypatch, tmp_path):
    from acctsw import bridge
    monkeypatch.chdir(tmp_path)
    _two_codex(ctx)
    msgs = []

    def click():
        assert bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "b@x.com"})["ok"]

    spawn = ExecSpawn([(None, 0, [click, None])])
    assert run(ctx, "codex", ["exec", "--ephemeral", "p"], spawn=spawn, get=_healthy_get,
               notify=msgs.append) == 0
    assert len(spawn.calls) == 1 and spawn.stops == 0
    assert sum("can't move to another seat" in m and "--ephemeral" in m for m in msgs) == 1


def test_own_claim_is_not_a_siblings(ctx, monkeypatch):
    monkeypatch.setattr(S, "_proc_start", lambda pid: "same start")
    S.claim_thread(ctx.data_dir, "codex", THREAD)
    assert S.threads_claimed_by_others(ctx.data_dir, "codex") == set()
    S.release_thread(ctx.data_dir, "codex")
    assert not list(ctx.data_dir.glob("thread-codex-*.json"))


# --- the paid key seat continues an exec job the same way ---------------------------------------

@pytest.fixture
def K(monkeypatch):
    """The paid-seat fixtures, with their scripted child writing an EXEC rollout."""
    from tests import test_launcher_keyseats as K

    def exec_rollout(dir, **kw):
        path = write_rollout(dir, **kw)
        head, rest = path.read_text().split("\n", 1)
        meta = json.loads(head)
        meta["payload"].update(originator="codex_exec", source="exec")
        path.write_text(json.dumps(meta) + "\n" + rest)
        return path

    monkeypatch.setattr(K, "write_rollout", exec_rollout)
    return K


def test_key_seat_resumes_the_exec_job_non_interactively(ctx, K):
    _, reset = K.setup(ctx)
    stdin = []

    class Child(K.Child):
        def __call__(self, argv, on_output, on_tick=None, *, env=None, stdin_null=False):
            stdin.append(stdin_null)
            return super().__call__(argv, on_output, on_tick, env=env)

    child = Child(ctx, [lambda: K.answer(ctx)], key_status=0)
    assert L.run(ctx, "codex", ["exec", "-m", "mine", "--json", "do it"], spawn=child,
                 get=K.usage_get(reset), price_get=K.price_get, notify=lambda m: None) == 0
    argv, _ = child.calls[1]
    # The paid model replaces the user's -m; the job resumes its own thread, never the TUI.
    assert argv[1:5] == ["exec", "--model", K.MODEL, "-c"]
    assert argv[5].startswith("model_provider=")
    assert argv[6:] == ["--json", "resume", K.THREAD, X.CONTINUE_PROMPT]
    assert stdin == [False, True]


def test_key_seat_is_never_offered_for_a_non_resumable_exec_job(ctx, monkeypatch, K):
    from unittest.mock import Mock
    _, reset = K.setup(ctx)
    request = Mock(side_effect=AssertionError("must not offer a key"))
    monkeypatch.setattr(L.handoff, "request", request)

    class Child(K.Child):
        def __call__(self, argv, on_output, on_tick=None, *, env=None, stdin_null=False):
            return super().__call__(argv, on_output, on_tick, env=env)

    child = Child(ctx)
    assert L.run(ctx, "codex", ["exec", "--frobnicate", "p"], spawn=child,
                 get=K.usage_get(reset), price_get=K.price_get, notify=lambda m: None) == 1
    request.assert_not_called()
