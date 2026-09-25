"""Metered-seat consent survives independent app/launcher processes without carrying secrets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import multiprocessing
import os
from queue import Empty
import stat
import subprocess

import pytest

from acctsw import handoff, keyseats, session
from acctsw.context import Context
from acctsw.pricing import Model, Rate, parse_catalog
from acctsw.providers import get_provider
from acctsw.util import iso, now

SECRET = "sk-test-secret-never-in-confirmation"


@pytest.fixture
def seat(ctx):
    seat = keyseats.add(ctx, get_provider("openrouter"), SECRET, label="Metered", model="m",
                        get=lambda *args: (200, "{}"))
    with ctx.locked():
        state = ctx.load_state()
        state.upsert_seat("codex", "work@x", name="Work")
        state.save()
    return seat


def _request(ctx, seat, **kwargs):
    return handoff.request(ctx, "codex", "work@x", seat["id"], **kwargs)


@pytest.mark.parametrize("approved", [True, False])
def test_round_trip_and_one_time_consumption(ctx, seat, approved):
    at = now()
    id = _request(ctx, seat, session_id="thread-1", at=at)
    record = handoff.pending(ctx, at=at)[0]
    assert record["id"] == id
    assert record["tool"] == "codex"
    assert record["from_seat"] == {"id": "work@x", "label": "Work"}
    assert record["key_seat"] == {k: seat[k] for k in ("id", "label", "provider", "model")}
    assert record["session_id"] == "thread-1"
    assert record["pid"] == os.getpid()
    assert record["notify"] is True
    assert record["expires_at"] == iso(at + handoff.DEFAULT_TIMEOUT)
    before = ctx.load_state().data["rev"]
    assert handoff.resolve(ctx, id, at=at)["status"] == "pending"
    assert handoff.pending(ctx, tool="claude", at=at) == []
    assert ctx.load_state().data["rev"] == before  # polling isn't a mutation
    assert handoff.answer(ctx, id, approved, at=at)
    assert not handoff.answer(ctx, id, not approved, at=at)
    assert handoff.pending(ctx, at=at) == []
    result = handoff.resolve(ctx, id, at=at)
    assert result["status"] == ("approved" if approved else "declined")
    assert result["decision_source"] == "user"
    assert result["answered_at"] == iso(at)
    assert handoff.resolve(ctx, id, at=at) is None
    assert id not in ctx.load_state().data["handoffs"]
    assert stat.S_IMODE(ctx.state_file.stat().st_mode) == 0o600
    # Consent is per hop, never remembered for the seat or session.
    next_id = _request(ctx, seat, session_id="thread-1", at=at)
    assert next_id != id
    assert handoff.resolve(ctx, next_id, at=at)["status"] == "pending"


@pytest.mark.parametrize("first_reader", ["pending", "answer", "resolve"])
def test_expiry_is_enforced_by_every_reader(ctx, seat, first_reader):
    at = now()
    id = _request(ctx, seat, at=at, timeout=timedelta(seconds=10))
    assert handoff.resolve(ctx, id, at=at + timedelta(seconds=9))["status"] == "pending"
    deadline = at + timedelta(seconds=10)
    if first_reader == "pending":
        assert handoff.pending(ctx, at=deadline) == []
    elif first_reader == "answer":
        assert not handoff.answer(ctx, id, True, at=deadline)
    result = handoff.resolve(ctx, id, at=deadline)
    assert result["status"] == "expired"
    assert result["finished_at"] == iso(deadline)
    assert handoff.pending(ctx, at=deadline) == []


def test_dead_requester_leaves_no_prompt(ctx, seat):
    id = _request(ctx, seat)
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead.wait()
    with ctx.locked():
        state = ctx.load_state()
        state.data["handoffs"][id]["pid"] = dead.pid
        state.save()
    assert handoff.pending(ctx) == []
    assert not handoff.answer(ctx, id, True)
    assert handoff.resolve(ctx, id)["status"] == "dead"


@pytest.mark.parametrize("start, expected", [("Different Start", "dead"), ("", "dead"),
                                             (None, "pending"), ("Stable Start", "pending")])
def test_recycled_pid_and_ps_fallback(ctx, seat, monkeypatch, start, expected):
    monkeypatch.setattr(session, "_proc_start", lambda pid: "Stable Start")
    id = _request(ctx, seat)
    monkeypatch.setattr(session, "_proc_start", lambda pid: start)
    assert handoff.resolve(ctx, id)["status"] == expected
    assert bool(handoff.pending(ctx)) is (expected == "pending")


def test_auto_approval_is_recorded_and_consumed(ctx, seat):
    with ctx.locked():
        state = ctx.load_state()
        state.set_setting("confirm_key_switch", False)
        state.set_setting("notify", False)
        state.save()
    before = ctx.load_state().data["rev"]
    id = _request(ctx, seat)
    stored = ctx.load_state()
    record = stored.data["handoffs"][id]
    assert stored.data["rev"] == before + 1
    assert record["status"] == "approved"
    assert record["decision_source"] == "setting"
    assert record["answered_at"] == record["created_at"]
    assert record["notify"] is False
    assert handoff.pending(ctx) == []
    assert handoff.resolve(ctx, id)["status"] == "approved"
    assert handoff.resolve(ctx, id) is None


def test_unknown_price_is_explicit(ctx, seat):
    id = _request(ctx, seat)
    price = handoff.resolve(ctx, id)["price"]
    assert price["status"] == "unknown"
    assert price["estimate"] is True
    assert price["rates"]["input"] == {"status": "unknown", "value": None, "same_as": None}
    assert price["rates"]["output"]["value"] is None
    assert price["verified_at"] is None
    assert price["age_seconds"] is None


def test_price_snapshot_retains_decimal_units_and_age(ctx, seat):
    at = now()
    fetched = iso(at - timedelta(hours=25))
    model = parse_catalog(get_provider("openrouter"), '''{"data": [{"id": "m", "pricing": {
        "prompt": "0.00000123456789", "completion": "0", "request": "0.0003"}}]}''',
        verified_at=fetched)[0]
    id = _request(ctx, seat, model=model, at=at)
    # A subsequent catalog/model change must not change what is being approved.
    model.rates["input"] = Rate("known", "99")
    price = handoff.resolve(ctx, id, at=at)["price"]
    assert price["status"] == "known"
    assert price["rates"]["input"]["value"] == "1.23456789000000"
    assert price["rates"]["output"]["value"] == "0"
    assert price["rates"]["request"]["value"] == "0.0003"
    assert price["token_unit"] == "per_million_tokens"
    assert price["request_unit"] == "per_request"
    assert price["verified_at"] == fetched
    assert price["age_seconds"] == 25 * 3600
    assert price["potentially_stale"] is True


def test_partial_and_tiered_price_is_not_invented(ctx, seat):
    model = Model("openrouter", "m", "m", "responses", currency="EUR",
                  rates={"input": Rate("known", "2"), "cached_input": Rate("known", "1")},
                  long_context_threshold=100_000,
                  long_context_rates={"input": Rate("known", "4")})
    id = _request(ctx, seat, model=model)
    price = handoff.resolve(ctx, id)["price"]
    assert price["status"] == "partial"
    assert price["currency"] == "EUR"
    assert price["rates"]["output"]["value"] is None
    assert price["long_context_threshold"] == 100_000
    assert price["long_context_rates"]["input"]["value"] == "4"
    assert price["long_context_rates"]["cached_input"]["value"] == "1"


def test_request_copies_only_public_fields_and_never_reads_keychain(ctx, seat, monkeypatch):
    with ctx.locked():
        state = ctx.load_state()
        # Guard against future callers adding secret-bearing fields to metadata.
        state.data["keys"][seat["id"]]["credentials"] = {"api_key": SECRET}
        state.data["keys"][seat["id"]]["last_validation"]["response"] = SECRET
        state.accounts("codex")["work@x"]["auth"] = SECRET
        state.save()
    def forbidden(*args):
        pytest.fail("handoff must not access credentials")
    monkeypatch.setattr(ctx.keychain, "get", forbidden)
    model = Model("openrouter", "m", SECRET, "responses", source_url="https://example/" + SECRET)
    id = _request(ctx, seat, model=model)
    assert SECRET not in json.dumps(ctx.load_state().data["handoffs"])
    assert SECRET not in json.dumps(handoff.resolve(ctx, id))


@pytest.mark.parametrize("changes", [{"provider": "openai"}, {"id": "different"},
                                     {"wire_api": "messages"}])
def test_wrong_price_identity_is_rejected(ctx, seat, changes):
    model = replace(Model("openrouter", "m", "m", "responses"), **changes)
    with pytest.raises(ValueError, match="Price model"):
        _request(ctx, seat, model=model)
    assert handoff.pending(ctx) == []


def test_invalid_requests_do_not_write(ctx, seat):
    before = ctx.state_file.read_bytes()
    with pytest.raises(ValueError, match="another harness"):
        handoff.request(ctx, "claude", None, seat["id"])
    with pytest.raises(ValueError, match="Unknown key seat"):
        handoff.request(ctx, "codex", None, "missing")
    with pytest.raises(ValueError, match="positive"):
        _request(ctx, seat, timeout=timedelta(0))
    with pytest.raises(ValueError, match="boolean"):
        handoff.answer(ctx, "missing", "false")
    with pytest.raises(ValueError, match="identifiers"):
        _request(ctx, seat, session_id={"secret": SECRET})
    assert ctx.state_file.read_bytes() == before


def test_concurrent_answers_and_consumers_have_one_winner(ctx, seat):
    id = _request(ctx, seat)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda choice: (choice, handoff.answer(ctx, id, choice)),
                                [True, False] * 4))
        winners = [choice for choice, accepted in results if accepted]
        assert len(winners) == 1
        resolved = list(pool.map(lambda _: handoff.resolve(ctx, id), range(8)))
    records = [record for record in resolved if record is not None]
    assert len(records) == 1
    assert records[0]["status"] == ("approved" if winners[0] else "declined")


def _process_requests(root, seat_id, ready, done):
    """Each spawned worker owns its Context and opens the existing flock independently."""
    ctx = Context.for_test(root)
    ready.put(True)
    ids = [handoff.request(ctx, "codex", None, seat_id) for _ in range(3)]
    with ctx.locked():
        state = ctx.load_state()
        state.set_setting(f"worker_{os.getpid()}", True)
        state.save()
    done.put(ids)


def test_cross_process_requests_share_existing_flock(ctx, seat, tmp_path):
    mp = multiprocessing.get_context("spawn")
    ready, done = mp.Queue(), mp.Queue()
    workers = [mp.Process(target=_process_requests, args=(tmp_path, seat["id"], ready, done))
               for _ in range(3)]
    before = ctx.load_state().data["rev"]
    try:
        with ctx.locked():
            for worker in workers:
                worker.start()
            for _ in workers:
                assert ready.get(timeout=15)
            # Holding the normal engine lock must block handoff writers too.
            with pytest.raises(Empty):
                done.get(timeout=0.2)
            assert ctx.load_state().data.get("handoffs", {}) == {}
        ids = [id for _ in workers for id in done.get(timeout=15)]
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        state = ctx.load_state()
        assert set(state.data["handoffs"]) == set(ids)
        assert len(ids) == 9
        assert state.data["rev"] == before + 12
        assert all(state.settings()[f"worker_{worker.pid}"] for worker in workers)
        assert handoff.pending(ctx) == []  # all requesting processes have now exited
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        ready.close()
        done.close()
