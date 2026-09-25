"""The plan's documented usage contracts require different provider arithmetic.

Rates are controlled fixtures, not assertions about today's retail prices. In particular,
cache-write replacement and xAI's separate Chat reasoning charge must survive refactoring.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from acctsw import spend as S
from acctsw.pricing import Model, Rate, parse_catalog
from acctsw.providers import WireAPI, get_provider

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def model(provider: str = "openai", wire: WireAPI = "responses", *,
          inclusion: bool | None = True) -> Model:
    return Model(provider, "fixture", "Fixture", wire, reasoning_included_in_output=inclusion,
                 rates={"input": Rate("known", "2"), "output": Rate("known", "8"),
                        "cached_input": Rate("known", "0.2"),
                        "cache_write": Rate("known", "2.5"),
                        "reasoning": Rate("same_as", same_as="output"),
                        "request": Rate("not_applicable")})


@pytest.fixture
def anthropic_model():
    return Model("anthropic", "fixture", "Fixture", "messages", reasoning_included_in_output=True,
                 rates={"input": Rate("known", "3"), "output": Rate("known", "15"),
                        "cached_input": Rate("known", "0.3"), "cache_write": Rate(),
                        "cache_write_5m": Rate("known", "3.75"),
                        "cache_write_1h": Rate("known", "6"),
                        "reasoning": Rate("same_as", same_as="output"),
                        "request": Rate("not_applicable")})


@pytest.fixture
def ledger():
    value = S.Ledger()
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("wire", ["responses", "chat"])
def test_openai_inclusive_cache_writes_replace_input_and_reasoning_is_not_added(wire):
    # OpenAI input includes cache reads/writes; the write rate is 1.25 times ordinary input.
    input_key, output_key = (("input_tokens", "output_tokens") if wire == "responses"
                             else ("prompt_tokens", "completion_tokens"))
    usage = {input_key: 1000, output_key: 200,
             input_key + "_details": {"cached_tokens": 300, "cache_write_tokens": 100},
             output_key + "_details": {"reasoning_tokens": 150}}
    m = model(wire=wire)
    tokens = S.adapt_usage(m, usage)
    assert tokens.counts == {"input": 600, "cached_input": 300, "cache_write": 100, "output": 200}
    assert tokens.prompt_tokens == 1000 and tokens.reasoning_tokens == 150
    assert S.estimate(m, usage) == S.Charge(Decimal("0.00311"), "USD", "estimated")


def test_anthropic_exclusive_input_charges_ttl_decomposition_only(anthropic_model):
    # platform.claude.com/docs/en/build-with-claude/prompt-caching: input excludes all cache.
    m = anthropic_model
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 500,
             "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 300},
             "output_tokens_details": {"thinking_tokens": 40}}
    tokens = S.adapt_usage(m, usage)
    assert tokens.counts == {"input": 100, "output": 50, "cached_input": 1000,
                             "cache_write_5m": 200, "cache_write_1h": 300}
    assert tokens.prompt_tokens == 1600 and tokens.reasoning_tokens == 40
    assert S.estimate(m, usage).amount == Decimal("0.0039")


def test_anthropic_aggregate_requires_an_explicit_rate_without_guessing_ttl(anthropic_model):
    m = anthropic_model
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 500}
    assert S.try_estimate(m, usage) == S.Unavailable("Price unavailable: cache_write")
    m = replace(m, rates={**m.rates, "cache_write": Rate("known", "3.75")})
    assert S.adapt_usage(m, usage).counts["cache_write"] == 500
    assert S.estimate(m, usage).amount == Decimal("0.002925")


def test_anthropic_rejects_inconsistent_cache_decomposition():
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 500,
             "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 301}}
    assert S.try_estimate(model("anthropic", "messages"), usage) == S.Unavailable(
        "Cache creation decomposition does not match aggregate")


def test_xai_chat_excludes_reasoning_while_responses_includes_it():
    # docs.x.ai/developers/models: documented Chat example 32 + 9 + 94 = 135.
    chat_usage = {"prompt_tokens": 32, "completion_tokens": 9, "total_tokens": 135,
                  "completion_tokens_details": {"reasoning_tokens": 94}}
    responses_usage = {"input_tokens": 32, "output_tokens": 103, "total_tokens": 135,
                       "output_tokens_details": {"reasoning_tokens": 94}}
    # Use the catalog's per-API inclusion flags so this also checks discovery -> accounting.
    body = ('{"models":[{"id":"grok","prompt_text_token_price":20000,'
            '"completion_text_token_price":80000,"cached_prompt_text_token_price":2000}]}')
    chat, = parse_catalog(get_provider("xai"), body, wire_api="chat")
    responses, = parse_catalog(get_provider("xai"), body, wire_api="responses")
    assert S.adapt_usage(chat, chat_usage).counts["reasoning"] == 94
    assert "reasoning" not in S.adapt_usage(responses, responses_usage).counts
    assert S.estimate(chat, chat_usage).amount == Decimal("0.000888")
    assert S.estimate(responses, responses_usage).amount == Decimal("0.000888")


@pytest.mark.parametrize("provider", ["langdock", "groq", "together", "mistral", "openai_compatible"])
def test_compatible_provider_needs_verified_reasoning_inclusion(provider):
    m = model(provider, "chat", inclusion=None)
    usage = {"prompt_tokens": 32, "completion_tokens": 9,
             "completion_tokens_details": {"reasoning_tokens": 94}}
    assert S.try_estimate(m, usage) == S.Unavailable("Unknown reasoning inclusion")
    with pytest.raises(S.PriceUnavailable, match="Unknown reasoning inclusion"):
        S.estimate(m, usage)
    assert S.estimate(replace(m, reasoning_included_in_output=False), usage).amount == Decimal("0.000888")


@pytest.mark.parametrize("provider,wire", [
    ("openrouter", "responses"), ("openrouter", "chat"),
    ("deepseek", "responses"), ("cerebras", "chat"),
])
def test_other_inclusive_providers_do_not_double_count_reasoning(provider, wire):
    input_key, output_key = (("input_tokens", "output_tokens") if wire == "responses"
                             else ("prompt_tokens", "completion_tokens"))
    usage = {input_key: 100, output_key: 20, input_key + "_details": {"cached_tokens": 40},
             output_key + "_details": {"reasoning_tokens": 15}}
    assert S.estimate(model(provider, wire), usage).amount == Decimal("0.000288")


def test_deepseek_chat_uses_separate_cache_hit_and_miss_counters():
    usage = {"prompt_tokens": 100, "completion_tokens": 20,
             "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60,
             "completion_tokens_details": {"reasoning_tokens": 15}}
    m = model("deepseek", "chat")
    assert S.estimate(m, usage).amount == Decimal("0.000288")
    assert isinstance(S.try_estimate(m, {**usage, "prompt_cache_miss_tokens": 61}), S.Unavailable)


@pytest.mark.parametrize("provider,usage,amount", [
    ("openrouter", '{"cost":0.1234567890123456789}', "0.1234567890123456789"),
    ("xai", '{"cost_in_usd_ticks":123456789}', "0.0123456789"),
    ("openrouter", '{"cost":0}', "0"),
    ("xai", '{"cost_in_usd_ticks":0}', "0"),
])
def test_reported_money_needs_neither_prices_nor_token_conventions(provider, usage, amount):
    m = replace(model(provider, inclusion=None), rates={})
    assert S.try_estimate(m, usage) == S.Charge(Decimal(amount), "USD", "provider_reported")


@pytest.mark.parametrize("provider,key,value,amount", [
    ("openrouter", "cost", "0.123", "0.123"),
    ("xai", "cost_in_usd_ticks", 123456789, "0.0123456789"),
])
def test_reported_money_wins_over_a_different_token_estimate(provider, key, value, amount):
    usage = {"input_tokens": 100, "output_tokens": 20, key: value}
    assert S.estimate(model(provider), usage) == S.Charge(Decimal(amount), "USD", "provider_reported")
    assert S.estimate(model(provider), {**usage, key: None}).amount == Decimal("0.00036")
    assert isinstance(S.try_estimate(model(provider), {**usage, key: "NaN"}), S.Unavailable)


def test_request_fee_is_added_once_without_token_scaling():
    m = replace(model("openrouter"), rates={**model().rates, "request": Rate("known", "0.001")})
    assert S.estimate(m, {"input_tokens": 100, "output_tokens": 20}).amount == Decimal("0.00136")
    assert S.estimate(m, {"input_tokens": 0, "output_tokens": 0}).amount == Decimal("0.001")


def test_long_context_uses_full_prompt_and_requires_known_threshold():
    m = replace(model(), long_context_threshold=100,
                long_context_rates={"input": Rate("known", "4"), "output": Rate("known", "16")})
    usage = {"input_tokens": 101, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 40}}
    assert S.estimate(m, usage).amount == Decimal("0.000572")
    assert S.try_estimate(replace(m, long_context_threshold=None), usage) == S.Unavailable(
        "Unknown long-context threshold")


@pytest.mark.parametrize("usage", [
    "{", "[]", {}, {"input_tokens": -1, "output_tokens": 2},
    {"input_tokens": "1.5", "output_tokens": 2},
    {"input_tokens": 1, "output_tokens": 2, "input_tokens_details": None},
    {"input_tokens": 1, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 2}},
    {"input_tokens": 1, "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 3}},
])
def test_invalid_usage_is_unavailable_never_zero(usage):
    assert isinstance(S.try_estimate(model(), usage), S.Unavailable)


def test_unknown_used_rate_is_unavailable_but_unused_dimensions_do_not_gate_meter():
    m = replace(model(), rates={"input": Rate("known", "2"), "output": Rate("known", "8"),
                                "request": Rate("not_applicable")})
    assert not m.prices_complete
    usage = {"input_tokens": 100, "output_tokens": 20}
    assert S.estimate(m, usage).amount == Decimal("0.00036")
    assert S.try_estimate(m, {**usage, "input_tokens_details": {"cached_tokens": 10}}) == S.Unavailable(
        "Price unavailable: cached_input")


def test_ledger_deduplicates_response_id_across_reopen_and_month_boundary(tmp_path):
    path = tmp_path / "spend.sqlite"
    m = model()
    usage = {"input_tokens": 100, "output_tokens": 20}
    first = S.Ledger(path=path)
    try:
        entry = first.record(m, "response-1", "session", usage, at=NOW)
        assert first.record(m, "response-1", "session", usage, at=NOW) == entry
    finally:
        first.close()
    resumed = S.Ledger(path=path)
    try:
        # A replay keeps the original month, even if a changed payload arrives after resume.
        later = NOW + timedelta(days=10)
        assert resumed.record(m, "response-1", "session", {"input_tokens": 999}, at=later) == entry
        assert resumed.session_total("session") == Decimal("0.00036")
        assert resumed.monthly_total(NOW) == Decimal("0.00036")
        assert resumed.monthly_total(later) == 0
        with pytest.raises(ValueError, match="different response identity"):
            resumed.record(m, "response-1", "different-session", usage, at=NOW)
    finally:
        resumed.close()


def test_ledger_sessions_span_months_and_months_span_sessions(ledger):
    m = model("openrouter")
    later = NOW + timedelta(days=10)
    ledger.record(m, "a", "one", {"cost": "0.1"}, at=NOW)
    ledger.record(m, "b", "two", {"cost": "0.2"}, at=NOW)
    ledger.record(m, "c", "one", {"cost": "0.3"}, at=later)
    assert ledger.session_total("one") == Decimal("0.4")
    assert ledger.session_total("two") == Decimal("0.2")
    assert ledger.session_total("empty") == 0
    assert ledger.monthly_total(NOW) == Decimal("0.3")
    assert ledger.monthly_total(later) == Decimal("0.3")


def test_ledger_unknown_response_makes_affected_totals_unavailable(ledger):
    ledger.record(model("openrouter"), "priced", "one", {"cost": "0.1"}, at=NOW)
    entry = ledger.record(model(inclusion=None), "unknown", "two",
                          {"input_tokens": 100, "output_tokens": 20}, at=NOW)
    assert entry.charge is None and entry.error == "Unknown reasoning inclusion"
    assert ledger.session_total("one") == Decimal("0.1")
    assert isinstance(ledger.session_total("two"), S.Unavailable)
    assert isinstance(ledger.monthly_total(NOW), S.Unavailable)
    assert ledger.monthly_total(NOW + timedelta(days=40)) == 0


def test_local_calendar_month_and_native_currency(tmp_path):
    ledger = S.Ledger(currency="EUR", calendar_timezone=timezone(timedelta(hours=2)))
    at = datetime(2026, 9, 30, 23, tzinfo=timezone.utc)
    m = replace(model(), currency="EUR")
    try:
        entry = ledger.record(m, "one", "session", {"input_tokens": 100, "output_tokens": 20}, at=at)
        assert entry.month == "2026-10" and entry.charge.currency == "EUR"
        assert ledger.monthly_total(NOW) == 0
        assert ledger.monthly_total(at) == Decimal("0.00036")
        with pytest.raises(ValueError, match="currency mismatch"):
            ledger.record(model(), "two", "session", {}, at=at)
        with pytest.raises(ValueError, match="Reported currency"):
            ledger.record(replace(m, provider="openrouter"), "three", "session", {"cost": "1"}, at=at)
    finally:
        ledger.close()
