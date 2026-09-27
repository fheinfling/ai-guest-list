"""Offline catalog fixtures pin provider units; injected transports also exercise cache ageing."""
from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from acctsw import pricing as P
from acctsw.providers import get_provider

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Catalog tests must use an injected transport")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def fake_get(mapping, calls):
    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return mapping.get(url, (404, ""))
    return get


def test_xai_cents_per_100m_and_zero_long_context_fallback():
    # docs.x.ai/developers/rest-api-reference/inference/models (language-models envelope).
    body = json.dumps({"models": [{"id": "grok", "prompt_text_token_price": 20000,
        "completion_text_token_price": 80000, "cached_prompt_text_token_price": 2000,
        "prompt_text_token_price_long_context": 40000,
        "completion_text_token_price_long_context": 160000,
        "cached_prompt_text_token_price_long_context": 0, "long_context_threshold": 128000}]})
    model, = P.parse_catalog(get_provider("xai"), body)
    assert model.rate("input") == Decimal("2")
    assert model.rate("output") == Decimal("8")
    assert model.rate("cached_input") == Decimal("0.2")
    assert model.rate("input", long_context=True) == Decimal("4")
    assert model.rate("output", long_context=True) == Decimal("16")
    assert model.rate("cached_input", long_context=True) == Decimal("0.2")
    assert model.reasoning_included_in_output is True
    assert model.context_window is None
    assert model.long_context_threshold == 128000
    chat, = P.parse_catalog(get_provider("xai"), body, wire_api="chat")
    assert chat.reasoning_included_in_output is False
    assert chat.prices_complete


def test_openrouter_per_token_strings_and_per_request_fee():
    body = json.dumps({"data": [{"id": "vendor/model", "pricing": {
        "prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003",
        "input_cache_write": "0.00000375", "request": "0.001"}}]})
    model, = P.parse_catalog(get_provider("openrouter"), body)
    assert model.rate("input") == Decimal(3)
    assert model.rate("output") == Decimal(15)
    assert model.rate("cached_input") == Decimal("0.3")
    assert model.rate("cache_write") == Decimal("3.75")
    assert model.rate("request") == Decimal("0.001")
    assert model.prices_complete


def test_together_bare_array_preserves_decimal_numbers():
    # docs.together.ai/reference/models: prices already in USD/Mtok.
    body = '[{"id":"model","pricing":{"input":0.1234567890123456789,"output":0.3,"cached_input":0.2}}]'
    model, = P.parse_catalog(get_provider("together"), body)
    assert model.rate("input") == Decimal("0.1234567890123456789")
    assert model.rate("output") == Decimal("0.3")
    assert model.rate("cached_input") == Decimal("0.2")
    assert model.reasoning_included_in_output is None
    assert not model.prices_complete
    with pytest.raises(ValueError):
        P.parse_catalog(get_provider("together"), '{"data":[]}')


def test_cerebras_pins_native_public_feed_not_huggingface(tmp_path):
    # inference-docs.cerebras.ai/api-reference/models/public-models, native format.
    p = get_provider("cerebras")
    assert p.models_endpoint == "https://api.cerebras.ai/public/v1/models"
    body = '{"data":[{"id":"gpt-oss-120b","pricing":{"prompt":"0.00000035","completion":"0.00000075"}}]}'
    calls = []
    result = P.fetch_catalog(p, "must-not-send", get=fake_get({p.models_endpoint: (200, body)}, calls),
                             cache_path=tmp_path / "prices.json", at=NOW)
    model, = result.models
    assert model.rate("input") == Decimal("0.35")
    assert model.rate("output") == Decimal("0.75")
    assert calls == [(p.models_endpoint, {}, 20)]
    wrong, = P.parse_catalog(p, '{"data":[{"id":"m","pricing":{"input":0.35,"output":0.75}}]}')
    assert wrong.rate("input") is None


def test_langdock_agent_timestamp_is_milliseconds_and_not_completion_schema():
    body = '{"data":[{"id":"m","name":"Model","created":1750000000123,"region":"eu"}]}'
    agent, = P.parse_langdock_agent_catalog(body)
    assert agent["created"] == Decimal("1750000000.123")
    completion, = P.parse_catalog(get_provider("langdock"), body)
    assert completion.created is None
    assert completion.rate("input") is None
    assert completion.reasoning_included_in_output is None


@pytest.mark.parametrize("id", ["openai", "anthropic", "openrouter", "langdock", "deepseek",
                                "groq", "mistral", "cerebras", "openai_compatible"])
def test_catalogs_normalise_discovery_without_inventing_prices(id):
    p = get_provider(id, **({"base_url": "https://example.test/v1"} if id == "openai_compatible" else {}))
    model, = P.parse_catalog(p, '{"data":[{"id":"unpriced","display_name":"Unpriced"}]}')
    assert model.provider == id and model.display_name == "Unpriced"
    assert model.rates["input"].status == "unknown"
    assert not model.prices_complete


CONTEXT_FIELDS = [
    ("anthropic", None, "max_input_tokens"), ("deepseek", None, "context_window"),
    ("groq", None, "context_window"), ("mistral", None, "max_context_length"),
    ("openrouter", None, "context_length"), ("openrouter", "top_provider", "context_length"),
    ("together", None, "context_length"), ("cerebras", "limits", "max_context_length"),
]


@pytest.mark.parametrize("provider,parent,field", CONTEXT_FIELDS)
@pytest.mark.parametrize("value,expected", [
    (128000, 128000), (None, None), (True, None), (0, None), (-1, None),
    ("128000", None), (128000.5, None), ({}, None),
])
def test_context_window_uses_documented_capacity_fields(provider, parent, field, value, expected):
    fields = {field: value}
    row = {"id": "m", **({parent: fields} if parent else fields)}
    body = json.dumps([row] if provider == "together" else {"data": [row]})
    model, = P.parse_catalog(get_provider(provider), body)
    assert model.context_window == expected
    assert model.long_context_threshold is None


@pytest.mark.parametrize("provider", [
    "openai", "anthropic", "deepseek", "groq", "mistral", "openrouter", "together",
    "cerebras", "langdock", "xai",
])
def test_missing_context_window_stays_unknown(provider):
    rows = [{"id": "m"}]
    body = json.dumps(rows if provider == "together" else {
        "models" if provider == "xai" else "data": rows})
    model, = P.parse_catalog(get_provider(provider), body)
    assert model.context_window is None


@pytest.mark.parametrize("provider", ["openai", "langdock"])
def test_undocumented_context_fields_are_not_guessed(provider):
    row = {"id": "m", "context_window": 100, "max_input_tokens": 200,
           "max_context_length": 300, "context_length": 400,
           "top_provider": {"context_length": 500}, "limits": {"max_context_length": 600}}
    model, = P.parse_catalog(get_provider(provider), json.dumps({"data": [row]}))
    assert model.context_window is None


@pytest.mark.parametrize("top,expected", [(128000, 128000), (None, 64000), (False, 64000)])
def test_openrouter_prefers_catalog_context_then_top_provider(top, expected):
    row = {"id": "m", "context_length": top, "top_provider": {"context_length": 64000}}
    model, = P.parse_catalog(get_provider("openrouter"), json.dumps({"data": [row]}))
    assert model.context_window == expected


@pytest.mark.parametrize("provider,parent", [("openrouter", "top_provider"), ("cerebras", "limits")])
@pytest.mark.parametrize("value", [None, [], "unknown"])
def test_partial_nested_context_metadata_is_safe(provider, parent, value):
    body = json.dumps({"data": [{"id": "m", parent: value}]})
    model, = P.parse_catalog(get_provider(provider), body)
    assert model.context_window is None


def test_sort_models_uses_input_price_with_unknown_last_and_preserves_ties():
    rows = [{"id": "a-unknown"}, {"id": "b-expensive", "pricing": {"input": 10, "output": 0}},
            {"id": "z-cheap", "pricing": {"input": 2, "output": 100}},
            {"id": "c-free", "pricing": {"input": 0}},
            {"id": "d-cheap", "pricing": {"input": 2, "output": 1}},
            {"id": "e-unknown", "pricing": {"output": 1}}]
    models = P.parse_catalog(get_provider("together"), json.dumps(rows))
    original = list(models)
    assert P.has_live_prices(models)
    assert P.model_sort_key(models) == "input_usd_per_million_tokens"
    assert [m.id for m in P.sort_models(models)] == [
        "c-free", "z-cheap", "d-cheap", "b-expensive", "a-unknown", "e-unknown"]
    assert models == original


def test_unpriced_catalog_orders_by_id_even_with_context_metadata():
    body = '{"data":[{"id":"z","context_window":100},{"id":"a","context_window":200}]}'
    models = P.parse_catalog(get_provider("deepseek"), body)
    assert not P.has_live_prices(models)
    assert P.model_sort_key(models) == "id"
    assert [m.id for m in P.sort_models(models)] == ["a", "z"]


def test_empty_catalog_has_id_order():
    assert not P.has_live_prices([])
    assert P.model_sort_key([]) == "id"
    assert P.sort_models([]) == []


def test_partial_rates_aliases_and_zero_are_safe_for_sorting():
    zero = P.Model("together", "zero", "Zero", "chat", rates={"input": P.Rate("known", "0")})
    alias = replace(zero, id="alias", rates={"input": P.Rate("same_as", same_as="output"),
                                           "output": P.Rate("known", "2")})
    cycle = replace(zero, id="cycle", rates={"input": P.Rate("same_as", same_as="output"),
                                           "output": P.Rate("same_as", same_as="input")})
    unknown = replace(zero, id="unknown", rates={"output": P.Rate("known", "1")})
    inapplicable = replace(zero, id="inapplicable", rates={"input": P.Rate("not_applicable")})
    assert P.has_live_prices([zero])
    assert not P.has_live_prices([cycle, unknown, inapplicable])
    assert P.sort_models([cycle, alias, unknown, zero, inapplicable]) == [
        zero, alias, cycle, unknown, inapplicable]


def test_non_usd_prices_do_not_imply_a_dollar_order():
    usd = P.Model("together", "usd", "USD", "chat", rates={"input": P.Rate("known", "2")})
    eur = replace(usd, id="eur", currency="EUR", rates={"input": P.Rate("known", "1")})
    assert not P.has_live_prices([eur])
    assert P.model_sort_key([eur]) == "id"
    assert P.sort_models([eur, usd]) == [usd, eur]


def test_cache_ttl_staleness_refresh_and_decimal_roundtrip(tmp_path):
    p = get_provider("together")
    path = tmp_path / "pricing.json"
    body = '[{"id":"m","context_length":32768,"pricing":{"input":0.1234567890123456789,"output":2}}]'
    calls = []
    get = fake_get({p.models_endpoint: (200, body)}, calls)
    first = P.fetch_catalog(p, "secret", get=get, cache_path=path, at=NOW)
    assert first.source == "live"
    assert first.models[0].verified_at == NOW.isoformat()
    assert "secret" not in path.read_text()
    cached = P.fetch_catalog(p, "secret", get=get, cache_path=path, at=NOW + timedelta(hours=5))
    assert cached.source == "cache" and not cached.cache_expired and not cached.potentially_stale
    assert cached.models[0].rate("input") == Decimal("0.1234567890123456789")
    assert cached.models[0].source == "live"
    assert cached.models[0].context_window == 32768
    assert cached.models[0].verified_at == NOW.isoformat()
    assert P.model_sort_key(cached.models) == "input_usd_per_million_tokens"
    assert len(calls) == 1
    P.fetch_catalog(p, "secret", get=get, cache_path=path, at=NOW + timedelta(hours=6))
    assert len(calls) == 2
    failed = fake_get({}, calls)
    stale = P.fetch_catalog(p, "secret", get=failed, cache_path=path, at=NOW + timedelta(hours=31))
    assert stale.source == "cache" and stale.cache_expired and stale.potentially_stale
    assert stale.error == "http_404"


def test_cache_is_scoped_to_custom_endpoint_and_key(tmp_path):
    calls = []
    path = tmp_path / "pricing.json"
    p = get_provider("openai_compatible", base_url="https://one.test/v1")
    q = get_provider("openai_compatible", base_url="https://two.test/v1")
    get = fake_get({x.models_endpoint: (200, '{"data":[{"id":"m"}]}') for x in (p, q)}, calls)
    for provider, key in ((p, "one-key"), (p, "two-key"), (q, "one-key")):
        P.fetch_catalog(provider, key, get=get, cache_path=path, at=NOW)
    assert len(calls) == 3


@pytest.mark.parametrize("provider,id", [
    ("anthropic", "claude-sonnet-4-6"), ("openai", "gpt-5.6"), ("deepseek", "deepseek-chat"),
])
def test_discovery_never_supplies_static_prices(provider, id):
    model, = P.parse_catalog(get_provider(provider), json.dumps({"data": [{"id": id}]}))
    assert model.source == "live"
    assert model.verified_at is None
    assert model.rate("input") is None and model.rate("output") is None
    assert not P.has_live_prices([model])


@pytest.mark.parametrize("bad", ['{', '[]', '{"broken":true}'])
def test_corrupt_cache_falls_back_without_network(tmp_path, bad):
    path = tmp_path / "pricing.json"
    path.write_text(bad)
    result = P.fetch_catalog(get_provider("openai"), get=lambda *_: (500, ""), cache_path=path, at=NOW)
    assert result.source == "unavailable" and result.models == []
    assert result.fetched_at is None and result.cache_expired
    assert result.error == "http_500" and not result.potentially_stale


@pytest.mark.parametrize("status,body,error", [
    (503, "", "http_503"), (200, "{", "catalog_unavailable"),
    (200, '{"data":[]}', "catalog_unavailable"),
])
def test_failed_discovery_without_cache_has_no_models(tmp_path, status, body, error):
    result = P.fetch_catalog(get_provider("anthropic"), get=lambda *_: (status, body),
                             cache_path=tmp_path / "prices.json", at=NOW)
    assert result.models == [] and result.source == "unavailable" and result.error == error


@pytest.mark.parametrize("source", ["vendored", None])
def test_cache_without_live_provenance_cannot_restore_static_prices(tmp_path, source):
    p = get_provider("anthropic")
    path = tmp_path / "pricing.json"
    body = '{"data":[{"id":"claude-sonnet-4-6"}]}'
    P.fetch_catalog(p, get=lambda *_: (200, body), cache_path=path, at=NOW)
    cache = json.loads(path.read_text())
    model = next(iter(cache.values()))["models"][0]
    model["rates"]["input"] = {"status": "known", "value": "3"}
    if source is None:
        model.pop("source")
    else:
        model["source"] = source
    path.write_text(json.dumps(cache))
    result = P.fetch_catalog(p, get=lambda *_: (503, ""), cache_path=path, at=NOW)
    assert result.models == [] and result.source == "unavailable"


def test_transport_blocks_redirect_before_any_second_request(monkeypatch):
    # Exercise urllib's redirect machinery without opening a socket, including preserved headers.
    requests = []

    # Subclassing HTTPSHandler removes urllib's default HTTPS handler entirely. A BaseHandler
    # at the same priority can lose to the real transport, depending on handler ordering.
    class Redirect(urllib.request.HTTPSHandler):
        def https_open(self, req):
            requests.append(req)
            headers = {"Location": "https://other.test/stolen"}
            return self.parent.error("http", req, io.BytesIO(b"redirect"), 302, "Found", headers)

    real_builder = urllib.request.build_opener
    monkeypatch.setattr(P.urllib.request, "build_opener", lambda *handlers: real_builder(
        *handlers, urllib.request.ProxyHandler({}), Redirect()))
    status, body = P._default_get("https://provider.test/models", {"Authorization": "Bearer key"}, 2)
    assert status == 302 and body == "redirect"
    assert len(requests) == 1 and requests[0].get_header("Authorization") == "Bearer key"


def test_invalid_prices_never_become_zero():
    for bad in (None, "NaN", "Infinity", "-1", True, 0.1):
        with pytest.raises(ValueError):
            P.Rate("known", bad)


@pytest.mark.parametrize("name,key,standard", [
    ("input", "prompt_text_token_price", 20000),
    ("output", "completion_text_token_price", 80000),
    ("cached_input", "cached_prompt_text_token_price", 2000),
])
def test_each_xai_zero_long_context_rate_inherits_standard(name, key, standard):
    body = json.dumps({"models": [{"id": "grok", key: standard,
        key + "_long_context": 0, "long_context_threshold": 128000}]})
    model, = P.parse_catalog(get_provider("xai"), body)
    assert model.rate(name, long_context=True) == Decimal(standard) / 10000
