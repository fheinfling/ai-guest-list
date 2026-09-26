"""Metered terminal handoffs: scripted children, isolated Keychain, no network or real PTY."""
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from acctsw import handoff, keyhome, keyseats, launcher as L, providers, rollout
from acctsw import paths as P
from acctsw.util import iso, now
from tests.test_launcher import _two_codex, _two_claude
from tests.test_rollout import write_rollout, token_count_line, window
from tests.test_usage import codex_ok_body, claude_ok_body

SECRET = "sk-launcher-private-never-on-disk-7392"
MODEL = "guest/model"
THREAD = "019abcde-1234-7890-abcd-0123456789ab"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "unused-before-launch")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(handoff.session, "_proc_start", lambda *_: "test process")


def setup(ctx, *, resting=False, enabled=True, confirm=True, tool="codex"):
    state = _two_codex(ctx) if tool == "codex" else _two_claude(ctx)
    active = state.active(tool)
    reset = iso(now() + timedelta(hours=2))
    for email in state.accounts(tool):
        if resting or email != active:
            state.set_limited_until(tool, email, reset, source="usage")
    state.set_setting("key_fallback", enabled)
    state.set_setting("confirm_key_switch", confirm)
    state.save()
    seat = keyseats.add(ctx, providers.get_provider("openrouter" if tool == "codex" else "anthropic"),
                        SECRET, label="after hours", model=MODEL, get=lambda *_: (200, "{}"))
    return seat, reset


def usage_get(reset, pct=100, tool="codex"):
    body = (codex_ok_body(primary=pct, p_reset=reset) if tool == "codex" else
            claude_ok_body(five=pct, five_reset=reset))
    return lambda *_: (200, body)


def price_get(*_):
    return 200, json.dumps({"data": [{"id": MODEL, "pricing": {
        "prompt": "0.000002", "completion": "0.000008"}}]})


def answer(ctx, approved=True):
    pending = handoff.pending(ctx)
    assert len(pending) == 1
    assert pending[0]["session_id"] == THREAD
    assert pending[0]["price"]["rates"]["input"]["value"] == "2.000000"
    assert handoff.answer(ctx, pending[0]["id"], approved)


class Child:
    """One real subscription transcript, then optional key child; each hook is one PTY tick."""
    def __init__(self, ctx, hooks=(), *, output=True, exit_status=1, key_status=0):
        self.ctx, self.hooks, self.output = ctx, hooks, output
        self.exit_status, self.key_status = exit_status, key_status
        self.calls = []
        self.transcript = None
        self.stops = 0

    def __call__(self, argv, on_output, on_tick=None, *, env=None):
        self.calls.append((list(argv), env))
        if env is not None:
            assert env["OPENROUTER_API_KEY"] == SECRET
            copied = list((Path(env["CODEX_HOME"]) / "sessions").rglob("rollout-*.jsonl"))
            assert len(copied) == 1
            assert copied[0].read_bytes() == self.transcript.read_bytes()
            assert "flushed on shutdown" in copied[0].read_text()
            assert not (Path(env["CODEX_HOME"]) / "auth.json").exists()
            # Neither key stdout nor a key rollout limit may enter OAuth probes or switches.
            assert not on_output(b"Your workspace is out of credits.\n")
            with copied[0].open("a") as f:
                f.write(token_count_line(primary=window(100), timestamp=iso(now())) + "\n")
            assert not on_tick()
            return self.key_status
        self.transcript = write_rollout(self.ctx._codex_real / "sessions", thread=THREAD,
                                        cwd=os.getcwd())
        if self.output:
            assert not on_output(b"You've hit your usage limit\n")
        for hook in self.hooks:
            hook()
            if on_tick():
                self.stops += 1
                break
        with self.transcript.open("a") as f:
            f.write('{"flushed on shutdown":true}\n')
        return self.exit_status


def forbid_credentials(ctx, monkeypatch):
    spies = [Mock(side_effect=AssertionError("key path touched OAuth")) for _ in range(4)]
    monkeypatch.setattr(L, "switch", spies[0])
    monkeypatch.setattr(L, "sync_back", spies[1])
    monkeypatch.setattr(ctx, "set_live", spies[2])
    monkeypatch.setattr(ctx, "snapshot_set", spies[3])
    return spies


def test_approved_relaunch_preserves_session_and_keeps_secret_only_in_child_env(ctx, monkeypatch):
    seat, reset = setup(ctx)
    spies = []
    def approve():
        assert not ctx.codex_home(f"key:codex:{seat['id']}").exists()
        answer(ctx)
        spies.extend(forbid_credentials(ctx, monkeypatch))
    child = Child(ctx, [approve], key_status=7)
    messages = []
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 notify=messages.append) == 7
    assert child.stops == 1 and len(child.calls) == 2
    argv, env = child.calls[1]
    assert argv[1:3] == ["resume", THREAD]
    assert argv[argv.index("--model") + 1] == MODEL
    assert env["CODEX_HOME"] == str(ctx.codex_home(f"key:codex:{seat['id']}"))
    assert os.environ["CODEX_HOME"] != env["CODEX_HOME"]
    assert SECRET not in os.environ.values()
    assert any(MODEL in m and "paid per token" in m and "taking the floor" in m for m in messages)
    assert SECRET not in repr([args for args, _ in child.calls]) + repr(messages)
    for path in ctx.data_dir.parent.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path
    for spy in spies:
        spy.assert_not_called()
    assert ctx.load_state().active("codex") == "a@x.com"
    assert not handoff.pending(ctx)


def test_declined_live_offer_leaves_original_child_and_exit_status(ctx, monkeypatch):
    _, reset = setup(ctx)
    monkeypatch.setattr(L, "POLL_INTERVAL_S", 0.01)
    child = Child(ctx, [lambda: answer(ctx, False)])
    messages = []
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 notify=messages.append) == 1
    assert child.stops == 0 and len(child.calls) == 1
    assert any("declined" in m for m in messages)
    assert not any("expired" in m for m in messages)


def test_expired_live_prompt_stops_waiting_and_frees_terminal(ctx, monkeypatch):
    _, reset = setup(ctx)
    clock = now()
    monkeypatch.setattr(handoff, "now", lambda: clock)
    def expire():
        nonlocal clock
        clock += handoff.DEFAULT_TIMEOUT + timedelta(seconds=1)
    child = Child(ctx, [expire])
    messages = []
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 notify=messages.append) == L.EXIT_GAVE_UP
    assert child.stops == 1 and len(child.calls) == 1
    assert any("expired" in m for m in messages)
    assert not any("declined" in m for m in messages)


@pytest.mark.parametrize("mode", ["off", "healthy", "nearly_full", "budget", "auto_off"])
def test_key_not_offered_when_subscription_policy_does_not_yield_floor(ctx, monkeypatch, mode):
    _, reset = setup(ctx, enabled=mode != "off")
    if mode == "auto_off":
        state = ctx.load_state()
        state.set_setting("auto_switch", False)
        state.save()
    monkeypatch.setattr(L, "POLL_INTERVAL_S", 0.01)
    request = Mock(side_effect=AssertionError("must not offer a key"))
    monkeypatch.setattr(handoff, "request", request)
    pct = {"healthy": 20, "nearly_full": 97}.get(mode, 100)
    child = Child(ctx)
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset, pct),
                 max_switches=0 if mode == "budget" else 6,
                 price_get=lambda *_: pytest.fail("must not fetch a key price")) == 1
    assert len(child.calls) == 1
    request.assert_not_called()


@pytest.mark.parametrize("reply", [True, False, None])
def test_cold_all_resting_confirmation_uses_heartbeat_and_honours_expiry(ctx, monkeypatch, reply):
    seat, reset = setup(ctx, resting=True)
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    sleeps, launches, messages = [], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        assert seconds == L.TICK_INTERVAL_S
        records = handoff.pending(ctx)
        assert not ctx.codex_home(f"key:codex:{seat['id']}").exists()
        if reply is not None:
            assert handoff.answer(ctx, records[0]["id"], reply)
    def spawn(argv, output, on_tick=None, *, env=None):
        launches.append(argv)
        assert env["OPENROUTER_API_KEY"] == SECRET
        assert "resume" not in argv
        return 0
    rc = L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset), price_get=price_get,
               sleep=sleep, notify=messages.append)
    assert rc == (0 if reply is True else L.EXIT_GAVE_UP)
    assert len(launches) == (1 if reply is True else 0)
    assert 1 <= len(sleeps) <= 61
    if reply is None:
        assert any("expired" in m for m in messages)
    elif reply is False:
        assert any("declined" in m for m in messages)


def test_price_fetch_and_confirmation_never_hold_state_flock(ctx, monkeypatch):
    _, reset = setup(ctx, resting=True, confirm=False)
    original = ctx.locked
    depth = 0
    @contextmanager
    def locked():
        nonlocal depth
        assert depth == 0
        with original():
            depth += 1
            try:
                yield
            finally:
                depth -= 1
    monkeypatch.setattr(ctx, "locked", locked)
    def fetch(*args):
        assert depth == 0
        assert not ctx.load_state().data.get("handoffs")
        return price_get(*args)
    def spawn(argv, output, on_tick=None, *, env=None):
        assert depth == 0
        assert env is not None
        return 0
    assert L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset), price_get=fetch) == 0


@pytest.mark.parametrize("change", ["model", "removed", "fallback_off", "auto_off", "subscription_recovers"])
def test_approval_is_rechecked_before_any_key_child(ctx, monkeypatch, change):
    seat, reset = setup(ctx)
    def race():
        answer(ctx)
        state = ctx.load_state()
        if change == "model":
            state.data["keys"][seat["id"]]["model"] = "different-model"
        elif change == "removed":
            del state.data["keys"][seat["id"]]
        elif change in ("auto_off", "fallback_off"):
            state.set_setting("auto_switch" if change == "auto_off" else "key_fallback", False)
        else:
            state.set_limited_until("codex", "b@x.com", None)
        state.save()
    child = Child(ctx, [race])
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get) == 1
    assert child.stops == 0
    assert len(child.calls) == 1
    assert not ctx.codex_home(f"key:codex:{seat['id']}").exists()


def test_child_exiting_while_prompt_pending_still_waits_for_consent(ctx):
    _, reset = setup(ctx)
    child = Child(ctx)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        answer(ctx)
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 sleep=sleep) == 0
    assert sleeps == [L.TICK_INTERVAL_S]
    assert len(child.calls) == 2


def test_post_exit_exhaustion_uses_same_fallback(ctx):
    _, reset = setup(ctx)
    child = Child(ctx, output=False)
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 sleep=lambda _: answer(ctx)) == 0
    assert len(child.calls) == 2


def test_unknown_prices_are_offered_as_unknown_not_invented(ctx):
    _, reset = setup(ctx, resting=True)
    def sleep(_):
        record, = handoff.pending(ctx)
        assert record["price"]["status"] == "unknown"
        assert handoff.answer(ctx, record["id"], True)
    assert L.run(ctx, "codex", [], spawn=lambda *a, **kw: 0, get=usage_get(reset),
                 price_get=lambda *_: (503, ""), sleep=sleep) == 0


def test_claude_key_child_does_not_sync_subscription_credentials(ctx, monkeypatch):
    _, reset = setup(ctx, tool="claude")
    calls, spies = [], []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append(argv)
        if env is None:
            path = P.CLAUDE_CONFIG_DIR / "projects" / "test-project" / f"{THREAD}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"sessionId": THREAD, "cwd": os.getcwd()}) + "\n")
            assert not output(b"You've hit your usage limit\n")
            record, = handoff.pending(ctx)
            assert record["session_id"] == THREAD
            assert handoff.answer(ctx, record["id"], True)
            spies.extend(forbid_credentials(ctx, monkeypatch))
            assert on_tick()
            return 1
        assert argv[1:] == ["--resume", THREAD, "--model", MODEL]
        assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
        assert env["ANTHROPIC_MODEL"] == MODEL
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert (Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "test-project" / f"{THREAD}.jsonl").exists()
        return 0
    assert L.run(ctx, "claude", [], spawn=spawn, get=usage_get(reset, tool="claude"),
                 price_get=price_get) == 0
    assert len(calls) == 2
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize("spare_pct", [20, 97, 100])
def test_hard_limit_preflight_only_offers_key_when_spare_is_actually_resting(ctx, spare_pct):
    _, reset = setup(ctx)
    state = ctx.load_state()
    state.set_limited_until("codex", "b@x.com", None)
    state.save()
    calls = []
    def spawn(argv, output, on_tick=None, *, env=None):
        calls.append((argv, env))
        if len(calls) == 1:
            write_rollout(ctx._codex_real / "sessions", thread=THREAD, cwd=os.getcwd())
            stopped = output(b"Your workspace is out of credits.\n")
            assert stopped == (spare_pct < 100)
            if not stopped:
                answer(ctx)
                assert on_tick()
            return 1
        assert (env is not None) == (spare_pct == 100)
        return 0
    assert L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset, spare_pct),
                 price_get=price_get) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("exit_status", [-2, 130, 143])
def test_user_abort_cancels_pending_confirmation_without_waiting(ctx, exit_status):
    _, reset = setup(ctx)
    child = Child(ctx, exit_status=exit_status)
    assert L.run(ctx, "codex", [], spawn=child, get=usage_get(reset), price_get=price_get,
                 sleep=lambda _: pytest.fail("abort must not wait")) == exit_status
    assert len(child.calls) == 1
    assert not ctx.load_state().data.get("handoffs")


@pytest.mark.parametrize("stage", ["prepare", "env", "spawn"])
def test_key_failures_do_not_reconcile_oauth_or_echo_secret(ctx, monkeypatch, stage):
    seat, reset = setup(ctx)
    spies, messages = [], []
    def fail(*a, **kw):
        raise RuntimeError(SECRET)
    def approve():
        answer(ctx)
        spies.extend(forbid_credentials(ctx, monkeypatch))
        if stage != "spawn":
            monkeypatch.setattr(keyhome, "prepare" if stage == "prepare" else "build_env", fail)
    child = Child(ctx, [approve])
    def spawn(*a, **kw):
        if kw.get("env") is not None:
            raise RuntimeError("child spawn failed")
        return child(*a, **kw)
    if stage == "spawn":
        with pytest.raises(RuntimeError, match="child spawn failed"):
            L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset), price_get=price_get,
                  notify=messages.append)
    else:
        assert L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset), price_get=price_get,
                     notify=messages.append) == L.EXIT_GAVE_UP
    assert SECRET not in repr(messages)
    # Failed preparation never launched a key child. Its original subscription may still reconcile;
    # once preparation produced a key runtime, even an exceptional teardown MUST skip that path.
    for spy in spies[:2]:
        spy.assert_not_called()
    if stage != "prepare":
        for spy in spies[2:]:
            spy.assert_not_called()


@pytest.mark.parametrize("args", [["resume", THREAD], ["resume", "--last"]])
def test_cold_resume_copies_exact_conversation(ctx, args):
    _, reset = setup(ctx, resting=True, confirm=False)
    path = write_rollout(ctx._codex_real / "sessions", thread=THREAD, cwd=os.getcwd())
    def spawn(argv, output, on_tick=None, *, env=None):
        assert argv[1:3] == ["resume", THREAD]
        copied = Path(env["CODEX_HOME"]) / "sessions" / path.relative_to(ctx._codex_real / "sessions")
        assert copied.read_bytes() == path.read_bytes()
        return 0
    assert L.run(ctx, "codex", args, spawn=spawn, get=usage_get(reset), price_get=price_get) == 0


def test_cold_key_uses_approved_model_despite_original_model_argument(ctx):
    _, reset = setup(ctx, resting=True, confirm=False)
    def spawn(argv, output, on_tick=None, *, env=None):
        assert argv.count("--model") == 2  # one CLI option, one literal prompt after --
        assert argv[argv.index("--model") + 1] == MODEL
        assert argv[-3:] == ["--", "--model", "literal-prompt"]
        assert "subscription-model" not in argv
        return 0
    assert L.run(ctx, "codex", ["--model", "subscription-model", "--", "--model", "literal-prompt"],
                 spawn=spawn, get=usage_get(reset), price_get=price_get) == 0


def test_price_fetch_failure_cannot_echo_secret(ctx, monkeypatch):
    _, reset = setup(ctx, resting=True)
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "0")
    messages = []
    def fail(*_):
        raise RuntimeError(SECRET)
    assert L.run(ctx, "codex", [], spawn=lambda *a, **kw: pytest.fail("no consent"),
                 get=usage_get(reset), price_get=fail, notify=messages.append) == L.EXIT_GAVE_UP
    assert SECRET not in repr(messages)
    assert not ctx.load_state().data.get("handoffs")


def test_pty_spawn_passes_key_env_to_exec_without_parent_mutation(monkeypatch):
    class Executed(Exception):
        pass
    def execute(exe, argv, env):
        assert SECRET not in repr(argv)
        assert env["OPENROUTER_API_KEY"] == SECRET
        assert env["CODEX_HOME"] == "/private/key-home"
        assert "PYTHONHOME" not in env
        raise Executed
    monkeypatch.setattr(L.pty, "fork", lambda: (0, -1))
    monkeypatch.setattr(L.os, "execvpe", execute)
    with pytest.raises(Executed):
        L.pty_spawn(["codex", "resume", THREAD], lambda _: False,
                    env=keyhome.ChildEnv(OPENROUTER_API_KEY=SECRET, CODEX_HOME="/private/key-home",
                                         PYTHONHOME="/frozen/python"))
    assert SECRET not in os.environ.values()


def test_run_local_auth_exclusion_cannot_hide_healthy_subscription_from_key_gate(ctx, monkeypatch):
    _, reset = setup(ctx)
    state = ctx.load_state()
    state.set_limited_until("codex", "b@x.com", None)
    state.save()
    calls = []
    def get(*_):
        return (401, "") if len(calls) == 1 else (200, codex_ok_body(primary=100, p_reset=reset))
    def spawn(argv, output, on_tick=None):
        calls.append(argv)
        if len(calls) == 1:
            assert output(b"refresh token was revoked\n")
        else:
            write_rollout(ctx._codex_real / "sessions", thread=THREAD, cwd=os.getcwd())
            assert not output(b"You've hit your usage limit\n")
        return 1
    assert L.run(ctx, "codex", [], spawn=spawn, get=get,
                 price_get=lambda *_: pytest.fail("healthy excluded subscription still vetoes key")) == 1
    assert len(calls) == 2
    assert not ctx.load_state().data.get("handoffs")


def test_expiry_with_rest_wait_enabled_does_not_enter_hours_long_wait(ctx, monkeypatch):
    _, reset = setup(ctx, resting=True)
    monkeypatch.setenv(L.WAIT_ON_ALL_RESTING_ENV, "1")
    sleeps = []
    def sleep(seconds):
        assert seconds == L.TICK_INTERVAL_S
        sleeps.append(seconds)
    assert L.run(ctx, "codex", [], spawn=lambda *a, **kw: pytest.fail("unanswered consent"),
                 get=usage_get(reset), price_get=price_get, sleep=sleep) == L.EXIT_GAVE_UP
    assert sum(sleeps) <= handoff.DEFAULT_TIMEOUT.total_seconds() + L.TICK_INTERVAL_S
    assert not ctx.load_state().data.get("handoffs")


def test_ambiguous_session_is_never_offered_as_a_paid_resume(ctx, monkeypatch):
    _, reset = setup(ctx)
    messages = []
    def spawn(argv, output, on_tick=None):
        for thread in (THREAD, "another-thread"):
            write_rollout(ctx._codex_real / "sessions", thread=thread, cwd=os.getcwd())
        assert not output(b"You've hit your usage limit\n")
        return 1
    assert L.run(ctx, "codex", [], spawn=spawn, get=usage_get(reset),
                 price_get=lambda *_: pytest.fail("cannot price an ambiguous handoff"),
                 notify=messages.append) == 1
    assert any("couldn't pin down this session" in m for m in messages)
    assert not ctx.load_state().data.get("handoffs")
