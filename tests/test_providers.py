"""Provider capabilities and documented error pairs must not leak across compatible APIs."""
from __future__ import annotations

import copy
import json

import pytest

from acctsw import providers as P


def test_registry_capabilities_and_validation_are_independent():
    # langdock appears twice on purpose: its OpenAI-compatible and Anthropic-compatible routes
    # are different endpoints serving different model families, and they pair to different
    # harnesses. One key reaches both; the registry entry is what picks the road.
    assert set(P.PROVIDERS) == {"openai", "anthropic", "openrouter", "langdock",
                                "langdock_anthropic", "deepseek", "xai", "groq", "together",
                                "mistral", "cerebras", "openai_compatible"}
    assert P.get_provider("anthropic").responses_support == "unsupported"
    assert P.get_provider("anthropic").headers("secret") == {
        "x-api-key": "secret", "anthropic-version": "2023-06-01"}
    assert P.get_provider("openrouter").validation_endpoint.endswith("/key")
    assert P.get_provider("deepseek").validation_endpoint.endswith("/user/balance")
    assert P.get_provider("cerebras").validation_endpoint != P.get_provider("cerebras").models_endpoint
    assert P.get_provider("groq").responses_support == "beta"
    for id in ("together", "mistral", "cerebras"):
        assert P.get_provider(id).responses_support == "unverified"


@pytest.mark.parametrize("region", ["eu", "us", "global"])
def test_langdock_regions(region):
    p = P.get_provider("langdock", region=region)
    assert p.base_url == f"https://api.langdock.com/openai/{region}/v1"
    assert p.models_endpoint == p.validation_endpoint == p.base_url + "/models"
    assert p.headers("key") == {"Authorization": "Bearer key"}


def test_custom_provider_never_inherits_verified_responses():
    p = P.get_provider("openai_compatible", base_url="https://example.test/v1/")
    assert p.responses_support == "unverified"
    assert p.models_endpoint == "https://example.test/v1/models"
    for base in (None, "file:///tmp/models", "https://key@example.test", "https://example.test/?key=x"):
        with pytest.raises(ValueError):
            P.get_provider("openai_compatible", base_url=base)


@pytest.mark.parametrize("provider,status,error,expected", [
    ("openai", 401, {"code": "invalid_api_key"}, "invalid_key"),
    ("openai", 429, {"code": "insufficient_quota"}, "insufficient_quota"),
    ("openai", 429, {"code": "rate_limit_exceeded"}, "rate_limited"),
    # OpenAI puts exhausted money and throttling in the same 429; only `code` separates them, so
    # every documented spend code must read as quota rather than falling through to unknown.
    ("openai", 429, {"code": "credit_balance_exhausted"}, "insufficient_quota"),
    ("openai", 429, {"code": "organization_spend_limit_exceeded"}, "insufficient_quota"),
    ("openai", 429, {"code": "project_spend_limit_exceeded"}, "insufficient_quota"),
    ("openai", 429, {"code": "organization_usage_limit_exceeded"}, "insufficient_quota"),
    ("openai", 429, {"code": "slow_down"}, "rate_limited"),
    ("openai", 429, {"code": "not_a_documented_code"}, "unknown"),
    ("anthropic", 401, {"type": "authentication_error"}, "invalid_key"),
    ("anthropic", 402, {"type": "billing_error"}, "insufficient_quota"),
    ("anthropic", 400, {"type": "invalid_request_error"}, "unknown"),
    ("anthropic", 429, {"type": "rate_limit_error"}, "unknown"),
    ("openrouter", 401, {"code": 401}, "invalid_key"),
    ("openrouter", 402, {"code": 402}, "insufficient_quota"),
    ("openrouter", 429, {"code": 429}, "rate_limited"),
    ("openrouter", 402, {"code": 402, "metadata": {"limit_source": "openrouter_key_limit"}}, "insufficient_quota"),
    ("openrouter", 402, {"code": 402, "metadata": {"limit_source": "openrouter_credits"}}, "insufficient_quota"),
    ("openrouter", 402, {"code": 402, "metadata": {"limit_source": "openrouter_in_flight_budget"}}, "rate_limited"),
    ("openrouter", 402, {"code": 402, "metadata": {"limit_source": "future_value"}}, "unknown"),
    ("groq", 400, {"code": "blocked_api_access"}, "insufficient_quota"),
    # Langdock sends no structured error fields at all; the status carries the whole signal.
    ("langdock", 401, {}, "invalid_key"),
    ("langdock", 429, {}, "unknown"),   # a 429 could be throttling or money — stays unclaimed
    ("groq", 403, {"code": "blocked_api_access"}, "unknown"),
    ("openrouter", 429, {"code": "429"}, "unknown"),
    ("openrouter", 401, {"code": 402}, "unknown"),
    ("anthropic", 400, {"type": "billing_error"}, "unknown"),
])
def test_documented_error_pairs(provider, status, error, expected):
    # Contradictory prose must have no bearing on the structured classification.
    body = {"error": {**error, "message": "revoked invalid key quota exhausted rate limited"}, "future": [1]}
    original = copy.deepcopy(body)
    assert P.classify_error(provider, status, body) == expected
    assert P.classify_error(provider, status, json.dumps(body)) == expected
    assert body == original


@pytest.mark.parametrize("provider", P.PROVIDERS)
def test_prose_and_bare_status_are_never_classified(provider):
    # Langdock is the one provider that classifies a 401 from its status, because it sends no
    # structured fields at all (see the dedicated test below). It still never reads the message,
    # which is what this test is really protecting, so its 401 rows are exercised there instead.
    statuses = (429,) if provider == "langdock" else (401, 429)
    for body in ("not json", "[]", {}, {"error": "invalid key"}, {"error": {"message": "revoked"}}):
        for status in statuses:
            assert P.classify_error(provider, status, body) == "unknown"


@pytest.mark.parametrize("provider", ["deepseek", "xai", "together", "mistral", "cerebras", "openai_compatible"])
def test_undocumented_wire_pairs_are_unknown(provider):
    for status, code in ((401, "invalid_api_key"), (429, "insufficient_quota"), (401, "token_revoked")):
        assert P.classify_error(provider, status, {"error": {"code": code}}) == "unknown"


def test_429_does_not_imply_retry():
    for provider in ("openai", "anthropic", "langdock", "openai_compatible"):
        assert P.PROVIDERS[provider].money_may_be_gone_on_429
    assert not P.is_retryable("openai", 429, {"error": {"code": "insufficient_quota"}})
    assert not P.is_retryable("anthropic", 429, {"error": {"type": "rate_limit_error"}})
    assert P.is_retryable("openai", 429, {"error": {"code": "rate_limit_exceeded"}})


def test_langdock_401_is_status_evidence_and_never_message_text():
    """Langdock sends no structured error fields, so the status carries the whole signal.

    Observed live 2026-09-26: {"message": "The provided API key is invalid."} — no error object,
    no type, no code. A 401 from an authenticated models list means the credential was refused,
    which is what invalid_key claims. What must NOT happen is reading that sentence: the verdict
    is identical whatever the message says, including when it says the opposite.
    """
    for body in ('{"message": "The provided API key is invalid."}',
                 '{"message": "everything is fine, actually"}',
                 '{"message": "revoked"}', "{}"):
        assert P.classify_error("langdock", 401, body) == "invalid_key", body
    # 429 stays unclaimed: for this provider it could be throttling or exhausted money.
    for body in ('{"message": "slow down"}', '{"message": "out of credits"}', "{}"):
        assert P.classify_error("langdock", 429, body) == "unknown", body
        assert not P.is_retryable("langdock", 429, body)
    # An unreadable body is weaker evidence than a readable one; the parse guard wins.
    assert P.classify_error("langdock", 401, "not json at all") == "unknown"
    # And a bare-message 401 must NOT start classifying for providers that do send fields.
    for other in ("openai", "anthropic", "deepseek", "openai_compatible"):
        assert P.classify_error(other, 401, '{"message": "invalid"}') == "unknown", other
