"""Key seats keep secrets in Context's Keychain, and never probe another provider.

Every test uses the shared Context.for_test fixture and injected HTTP. Network and real process
entry points are blocked here so an accidentally missed injection fails before touching either.
"""
from __future__ import annotations

from dataclasses import replace
import json
import socket
import subprocess
from urllib import request

import pytest

from acctsw import TOOLS, keyseats as K, providers as P
from acctsw.keychain import InMemoryKeychain, KeychainError
from acctsw.state import State

SECRET = 'sk-test-private-"never-persist-9876'


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Test attempted network or a real subprocess")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(request, "build_opener", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def ok(url, headers, timeout):
    return 200, json.dumps({"echo": SECRET})


def add(ctx, provider="openai", **kwargs):
    return K.add(ctx, P.get_provider(provider), SECRET, label="Work", model="model-id",
                 get=ok, **kwargs)


def test_secret_roundtrip_and_metadata_only(ctx, monkeypatch, caplog):
    assert isinstance(ctx.keychain, InMemoryKeychain)
    seat = add(ctx)
    id = seat["id"]
    assert ctx.keychain.get(ctx.keychain_service, f"key:{id}") == SECRET
    assert K.get_secret(ctx, id, harness="codex") == SECRET
    assert seat["fingerprint"] == "9876"
    assert set(seat) == {"id", "label", "provider", "harness", "model", "fingerprint",
                         "created_at", "last_validation"}
    assert ctx.load_state().data["keys"][id] == seat
    assert set(TOOLS) == {"codex", "claude"}
    assert not ctx._homes_root.exists()

    def no_secret_read(*args):
        pytest.fail("Metadata access read Keychain")
    monkeypatch.setattr(ctx.keychain, "get", no_secret_read)
    assert K.list(ctx) == [seat]
    assert K.get(ctx, id) == seat
    for value in (ctx.state_file.read_text(), repr(K.list(ctx)), repr(K.get(ctx, id)), caplog.text):
        assert SECRET not in value
        assert json.dumps(SECRET)[1:-1] not in value
    seat["last_validation"]["error"] = "changed"
    assert K.get(ctx, id)["last_validation"]["error"] is None


@pytest.mark.parametrize("provider", ["openai", "openrouter", "langdock", "deepseek", "xai", "anthropic"])
def test_harness_pairing(ctx, provider):
    seat = add(ctx, provider)
    harness = "claude" if provider == "anthropic" else "codex"
    other = "codex" if harness == "claude" else "claude"
    assert seat["harness"] == harness
    assert K.list(ctx, harness=harness) == [seat]
    assert K.list(ctx, harness=other) == []
    with pytest.raises(ValueError, match="another harness"):
        K.get_secret(ctx, seat["id"], harness=other)
    assert K.get_secret(ctx, seat["id"], harness=harness) == SECRET


@pytest.mark.parametrize("provider", ["together", "mistral", "cerebras", "openai_compatible", "groq"])
def test_refuse_unverified_and_beta_before_validation_or_storage(ctx, provider):
    p = P.get_provider(provider, **({"base_url": "https://custom.test/v1"}
                                  if provider == "openai_compatible" else {}))
    def forbidden(*args):
        pytest.fail("Unsupported harness must be refused before testing a key")
    with pytest.raises(ValueError, match="Responses support is not verified"):
        K.add(ctx, p, SECRET, label="Work", model="m", get=forbidden)
    assert K.list(ctx) == []
    assert ctx.keychain._store == {}


def test_region_and_verified_custom_metadata(ctx):
    p = P.get_provider("langdock", region="eu")
    seat = K.add(ctx, p, SECRET, label="EU", model="m", get=ok)
    assert seat["region"] == "eu"
    # A future real capability test can supply a verified descriptor. A catalog 200 cannot.
    custom = replace(P.get_provider("openai_compatible", base_url="https://custom.test/v1"),
                     responses_support="verified")
    seat = K.add(ctx, custom, SECRET, label="Custom", model="m", get=ok)
    assert seat["base_url"] == custom.base_url


@pytest.mark.parametrize("provider", P.PROVIDERS)
def test_validation_is_exactly_one_authenticated_get(ctx, provider):
    p = P.get_provider(provider, **({"base_url": "https://custom.test/v1"}
                                  if provider == "openai_compatible" else {}))
    calls = []
    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return ok(url, headers, timeout)
    result = K.validate(ctx, p, SECRET, get=get)
    assert calls == [(p.validation_endpoint, p.headers(SECRET), 20)]
    assert result["operation_permitted"] is True
    assert result["inference_verified"] is False
    assert result["http_status"] == 200
    assert result["operation"] == {"openrouter": "key_info", "deepseek": "balance"}.get(provider, "models_list")
    assert not ctx.state_file.exists()


@pytest.mark.parametrize("provider,status,error,category", [
    ("openai", 401, {"code": "invalid_api_key"}, "invalid_key"),
    *[("openai", 429, {"code": code}, "insufficient_quota") for code in
      ("insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
       "project_spend_limit_exceeded", "organization_usage_limit_exceeded")],
    *[("openai", 429, {"code": code}, "rate_limited") for code in ("rate_limit_exceeded", "slow_down")],
    ("anthropic", 401, {"type": "authentication_error"}, "invalid_key"),
    ("anthropic", 402, {"type": "billing_error"}, "insufficient_quota"),
    ("anthropic", 429, {"type": "rate_limit_error"}, "unknown"),
    ("openrouter", 401, {"code": 401}, "invalid_key"),
    ("openrouter", 402, {"code": 402}, "insufficient_quota"),
    ("openrouter", 429, {"code": 429}, "rate_limited"),
    *[("openrouter", 402, {"code": 402, "metadata": {"limit_source": source}}, category)
      for source, category in (("openrouter_key_limit", "insufficient_quota"),
                               ("openrouter_credits", "insufficient_quota"),
                               ("openrouter_in_flight_budget", "rate_limited"), ("future", "unknown"))],
    ("groq", 400, {"code": "blocked_api_access"}, "insufficient_quota"),
    *[(provider, 401, {"code": "invalid_api_key"}, "unknown") for provider in
      ("langdock", "deepseek", "xai", "together", "mistral", "cerebras", "openai_compatible")],
])
def test_validation_uses_documented_taxonomy(ctx, provider, status, error, category):
    p = P.get_provider(provider, **({"base_url": "https://custom.test/v1"}
                                  if provider == "openai_compatible" else {}))
    result = K.validate(ctx, p, SECRET, get=lambda *a: (status, json.dumps(
        {"error": {**error, "message": SECRET + " revoked throttled"}})))
    assert result["error"] == category
    assert result["operation_permitted"] is False
    assert SECRET not in repr(result)


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError])
def test_transport_failure_is_unknown_and_does_not_echo_secret(ctx, error):
    def failed(*args):
        raise error(SECRET)
    result = K.validate(ctx, P.get_provider("openai"), SECRET, get=failed)
    assert result["error"] == "unknown" and result["http_status"] == 0
    assert SECRET not in repr(result)


@pytest.mark.parametrize("harness", [None, "key", ""])
def test_credential_access_requires_an_explicit_harness(ctx, harness):
    seat = add(ctx)
    with pytest.raises(ValueError, match="requires a codex or claude harness"):
        K.get_secret(ctx, seat["id"], harness=harness)


def test_failed_validation_is_retained_without_claiming_inference_failure(ctx):
    seat = K.add(ctx, P.get_provider("openai"), SECRET, label="Restricted", model="m",
                 get=lambda *a: (403, '{"error":{"message":"denied"}}'))
    assert K.get(ctx, seat["id"])["last_validation"] == seat["last_validation"]
    assert seat["last_validation"]["operation_permitted"] is False
    assert seat["last_validation"]["inference_verified"] is False


def test_forward_compatible_state_is_preserved(ctx):
    state = ctx.load_state()
    state.data["future"] = {"feature": True}
    state.save()
    seat = add(ctx)
    assert ctx.load_state().data["future"] == {"feature": True}
    assert K.get(ctx, seat["id"]) == seat
    assert K.remove(ctx, seat["id"])
    assert K.list(ctx) == []
    assert ctx.keychain.get(ctx.keychain_service, f'key:{seat["id"]}') is None


@pytest.mark.parametrize("raises", [False, True])
def test_remove_unpublishes_before_keychain_failure_and_can_retry(ctx, monkeypatch, raises):
    seat = add(ctx)
    original = ctx.keychain.delete
    def failed(*args):
        assert K.get(ctx, seat["id"]) is None
        if raises:
            raise KeychainError(SECRET)
        return False
    monkeypatch.setattr(ctx.keychain, "delete", failed)
    with pytest.raises(KeychainError, match="Keychain deletion failed") as error:
        K.remove(ctx, seat["id"])
    assert SECRET not in str(error.value)
    assert K.list(ctx) == []
    monkeypatch.setattr(ctx.keychain, "delete", original)
    assert K.remove(ctx, seat["id"]) is False  # Orphan cleanup succeeds without republishing.
    assert ctx.keychain._store == {}


def test_add_rolls_back_keychain_when_state_cannot_be_saved(ctx, monkeypatch):
    def failed(*args):
        raise OSError("read only")
    monkeypatch.setattr(State, "save", failed)
    with pytest.raises(OSError):
        add(ctx)
    assert ctx.keychain._store == {}
    assert K.list(ctx) == []


def test_remove_keeps_secret_when_metadata_save_fails(ctx, monkeypatch):
    seat = add(ctx)
    def failed(*args):
        raise OSError("read only")
    monkeypatch.setattr(State, "save", failed)
    with pytest.raises(OSError):
        K.remove(ctx, seat["id"])
    assert K.get_secret(ctx, seat["id"], harness="codex") == SECRET


def test_keychain_write_failure_does_not_publish_or_echo_secret(ctx, monkeypatch):
    def failed(*args):
        raise KeychainError(SECRET)
    monkeypatch.setattr(ctx.keychain, "set", failed)
    with pytest.raises(KeychainError) as error:
        add(ctx)
    assert SECRET not in str(error.value)
    assert K.list(ctx) == []


@pytest.mark.parametrize("secret,label,model", [("1234", "Work", "m"),
    (SECRET, SECRET, "m"), (SECRET, "Work", SECRET)])
def test_reject_secrets_in_metadata_and_full_key_fingerprints(ctx, secret, label, model):
    with pytest.raises(ValueError):
        K.add(ctx, P.get_provider("openai"), secret, label=label, model=model, get=ok)
    assert not ctx.state_file.exists()
    assert ctx.keychain._store == {}
