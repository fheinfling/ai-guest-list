"""Key-seat bridge contracts, with no network or real credentials."""
import json
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest

from acctsw import bridge, handoff, keyseats, pricing
from acctsw.util import iso, now

SECRET = "sk-pasted-private-key-123456"


@pytest.fixture
def transport(ctx, monkeypatch):
    locked = False
    real_lock = ctx.locked
    calls = []

    @contextmanager
    def lock():
        nonlocal locked
        assert not locked, "nested flock would deadlock"
        with real_lock():
            locked = True
            try:
                yield
            finally:
                locked = False

    def get(url, headers, timeout):
        assert not locked, "network must be outside the flock"
        calls.append((url, headers))
        if url.endswith("/key"):
            return 200, '{"data": {}}'
        return 200, json.dumps({"data": [
            {"id": "a-expensive", "name": "expensive", "context_length": 200000,
             "pricing": {"prompt": "0.000004", "completion": "0.000012"}},
            {"id": "z-cheap", "name": "cheap", "context_length": 100000,
             "pricing": {"prompt": "0.000001", "completion": "0.000003"}},
        ]})

    monkeypatch.setattr(ctx, "locked", lock)
    monkeypatch.setattr(pricing, "_default_get", get)
    return calls


def add(ctx, **kwargs):
    return bridge.handle(ctx, {"action": "key_add", "provider": "openrouter", "secret": SECRET,
                               "label": "work", "model": "z-cheap", **kwargs})


def envelope(ctx, result):
    assert type(result["ok"]) is bool
    assert result["state"] == bridge.snapshot_state(ctx)
    assert SECRET not in json.dumps(result)


def test_key_crud_validation_and_revisions(ctx, transport, monkeypatch):
    before = bridge.snapshot_state(ctx)["rev"]
    result = add(ctx)
    envelope(ctx, result)
    assert result["ok"] is True
    id = result["added"]
    assert result["state"]["keys"] == [result["seat"]]
    assert result["state"]["rev"] == before + 1
    assert keyseats.get_secret(ctx, id, harness="codex") == SECRET
    assert transport[0][0].endswith("/key")  # Public models cannot validate an OpenRouter key.

    checked = bridge.handle(ctx, {"action": "key_validate", "id": id})
    envelope(ctx, checked)
    assert checked["ok"] and checked["validation"]["operation_permitted"]
    assert checked["state"]["rev"] == before + 2

    monkeypatch.setattr(ctx.keychain, "get", lambda *_: pytest.fail("snapshot read a secret"))
    assert bridge.snapshot_state(ctx)["keys"][0]["id"] == id
    removed = bridge.handle(ctx, {"action": "key_remove", "id": id})
    envelope(ctx, removed)
    assert removed["ok"] and removed["removed"] and removed["state"]["keys"] == []
    assert removed["state"]["rev"] == before + 3


@pytest.mark.parametrize("provider,sort_key,ids", [
    ("openrouter", "input_usd_per_million_tokens", ["z-cheap", "a-expensive"]),
    ("openai", "id", ["a-expensive", "z-cheap"]),
])
def test_models_live_prices_or_absent(ctx, transport, provider, sort_key, ids):
    result = bridge.handle(ctx, {"action": "models_list", "provider": provider, "secret": SECRET})
    envelope(ctx, result)
    assert result["ok"] and result["sort_key"] == sort_key
    assert [model["id"] for model in result["models"]] == ids
    assert result["source"] == "live"
    model = result["models"][0]
    assert model["display_name"]
    assert "context_window" in model
    if provider == "openrouter":
        assert model["context_window"] == 100000
        assert Decimal(model["price"]["rates"]["input"]["value"]) == 1
        assert Decimal(model["price"]["rates"]["output"]["value"]) == 3
        assert model["price"]["token_unit"] == "per_million_tokens"
        assert model["price"]["estimate"] is True
    else:
        assert all("price" not in model for model in result["models"])
    assert result["state"]["rev"] == 0  # Catalog discovery is not a state mutation.


@pytest.mark.parametrize("action", sorted(bridge.KEY_ACTIONS))
def test_missing_fields_always_return_envelope(ctx, action):
    result = bridge.handle(ctx, {"action": action, "secret": SECRET})
    envelope(ctx, result)
    assert result["ok"] is False


@pytest.mark.parametrize("action,module,name", [
    ("key_add", keyseats, "add"),
    ("key_remove", keyseats, "remove"),
    ("key_validate", keyseats, "validate"),
    ("models_list", pricing, "fetch_catalog"),
    ("answer_key_switch", handoff, "answer"),
])
@pytest.mark.parametrize("exception", [ValueError, RuntimeError, OSError, KeyError])
def test_backend_errors_never_raise_or_echo(ctx, transport, monkeypatch, action, module, name, exception):
    id = add(ctx)["added"]

    def fail(*_args, **_kwargs):
        raise exception(f"backend echoed {SECRET}")

    monkeypatch.setattr(module, name, fail)
    result = bridge.handle(ctx, {"action": action, "provider": "openrouter", "secret": SECRET,
                                "label": "work", "model": "z-cheap", "id": id, "approved": True})
    envelope(ctx, result)
    assert result["ok"] is False


def test_provider_cannot_echo_key_in_catalog_or_cache(ctx, monkeypatch):
    monkeypatch.setattr(pricing, "_default_get", lambda *_: (200, json.dumps({"data": [
        {"id": SECRET, "name": f"echo {SECRET}"},
    ]})))
    result = bridge.handle(ctx, {"action": "models_list", "provider": "openai", "secret": SECRET})
    envelope(ctx, result)
    assert result["ok"]
    assert SECRET not in (ctx.data_dir / "pricing.json").read_text()


@pytest.mark.parametrize("field", ["label", "model", "base_url"])
def test_secret_in_metadata_is_rejected_before_storage(ctx, transport, field):
    kwargs = {field: f"https://example.test/{SECRET}"}
    if field == "base_url":
        kwargs.update(provider="openai_compatible", allow_unverified=True)
    result = add(ctx, **kwargs)
    envelope(ctx, result)
    assert not result["ok"] and not result["state"]["keys"]
    assert not transport


def test_validation_precedes_storage_and_retains_scoped_denial(ctx, monkeypatch):
    def get(*_):
        assert ctx.load_state().data["keys"] == {}
        return 401, json.dumps({"error": {"code": 401, "message": SECRET}})

    monkeypatch.setattr(pricing, "_default_get", get)
    result = add(ctx)
    envelope(ctx, result)
    assert result["ok"]  # The committed engine retains validation, not a claim of inference access.
    assert result["seat"]["last_validation"]["error"] == "invalid_key"


def test_validate_racing_removal_does_not_resurrect_seat(ctx, transport, monkeypatch):
    id = add(ctx)["added"]

    def get(*_):
        keyseats.remove(ctx, id)
        return 200, "{}"

    monkeypatch.setattr(pricing, "_default_get", get)
    result = bridge.handle(ctx, {"action": "key_validate", "id": id})
    envelope(ctx, result)
    assert not result["ok"] and result["state"]["keys"] == []


def test_validate_does_not_clobber_concurrent_settings(ctx, transport, monkeypatch):
    id = add(ctx)["added"]

    def get(*_):
        assert bridge.handle(ctx, {"action": "toggle", "key": "key_fallback", "value": True})["ok"]
        return 200, "{}"

    monkeypatch.setattr(pricing, "_default_get", get)
    result = bridge.handle(ctx, {"action": "key_validate", "id": id})
    envelope(ctx, result)
    assert result["ok"] and result["state"]["settings"]["key_fallback"] is True


@pytest.mark.parametrize("approved", [True, False])
def test_answer_pending_confirmation(ctx, transport, approved):
    seat_id = add(ctx)["added"]
    id = handoff.request(ctx, "codex", None, seat_id, session_id="session-1")
    snapshot = bridge.snapshot_state(ctx)
    assert snapshot["pending_key_switches"][0]["id"] == id
    assert SECRET not in json.dumps(snapshot)
    result = bridge.handle(ctx, {"action": "answer_key_switch", "id": id, "approved": approved})
    envelope(ctx, result)
    assert result["ok"] and result["answered"]
    assert result["state"]["pending_key_switches"] == []
    assert result["state"]["rev"] == snapshot["rev"] + 1
    repeated = bridge.handle(ctx, {"action": "answer_key_switch", "id": id, "approved": not approved})
    envelope(ctx, repeated)
    assert not repeated["ok"]
    assert handoff.resolve(ctx, id)["status"] == ("approved" if approved else "declined")


def test_expired_confirmation_is_hidden_and_cannot_be_approved(ctx, transport):
    id = handoff.request(ctx, "codex", None, add(ctx)["added"],
                         at=now() - timedelta(minutes=3))
    snapshot = bridge.snapshot_state(ctx)
    assert snapshot["pending_key_switches"] == []
    result = bridge.handle(ctx, {"action": "answer_key_switch", "id": id, "approved": True})
    envelope(ctx, result)
    assert result["ok"] is False


@pytest.mark.parametrize("key", ["confirm_key_switch", "key_fallback"])
def test_new_toggles(ctx, key):
    for value in (True, False):
        rev = bridge.snapshot_state(ctx)["rev"]
        result = bridge.handle(ctx, {"action": "toggle", "key": key, "value": value})
        envelope(ctx, result)
        assert result["ok"] and result["state"]["settings"][key] is value
        assert result["state"]["rev"] == rev + 1


def test_unverified_acknowledgement_is_explicit_and_gates_network(ctx, transport):
    kwargs = {"provider": "openai_compatible", "base_url": "https://example.test/v1"}
    for value in (False, "false", 1):
        result = add(ctx, **kwargs, allow_unverified=value)
        envelope(ctx, result)
        assert not result["ok"] and not transport
    result = add(ctx, **kwargs, allow_unverified=True)
    envelope(ctx, result)
    assert result["ok"] and result["seat"]["responses_verified"] is False
    assert transport[0][0] == "https://example.test/v1/models"


def test_region_and_harness_are_preserved(ctx, transport):
    result = add(ctx, provider="langdock", region="us")
    assert result["ok"] and result["seat"]["region"] == "us"
    assert transport[0][0] == "https://api.langdock.com/openai/us/v1/models"
    result = add(ctx, provider="anthropic")
    assert result["ok"] and result["seat"]["harness"] == "claude"


def test_catalog_failure_returns_error_envelope(ctx, monkeypatch):
    monkeypatch.setattr(pricing, "_default_get", lambda *_: (503, SECRET))
    result = bridge.handle(ctx, {"action": "models_list", "provider": "openai", "secret": SECRET})
    envelope(ctx, result)
    assert not result["ok"] and result["models"] == [] and result["sort_key"] == "id"


@pytest.mark.parametrize("action", sorted(bridge.KEY_ACTIONS))
def test_broken_snapshot_cannot_raise_or_leak(ctx, monkeypatch, action):
    def fail(*_):
        raise OSError(SECRET)

    monkeypatch.setattr(bridge, "snapshot_state", fail)
    result = bridge.handle(ctx, {"action": action, "secret": SECRET})
    assert result["ok"] is False and result["state"] is None
    assert SECRET not in json.dumps(result)
