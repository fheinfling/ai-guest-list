"""Billing providers are not interchangeable just because their JSON resembles OpenAI's.

Discovery, free key validation and Responses support are separate capabilities. Error mapping
deliberately leaves undocumented wire pairs unknown; neither prose nor a bare 429 proves that
retrying will help. The registry describes endpoints only and never probes a key across providers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

WireAPI = Literal["responses", "chat", "messages"]
ResponsesSupport = Literal["verified", "beta", "unverified", "unsupported"]


@dataclass(frozen=True)
class Provider:
    id: str
    display_name: str
    base_url: str
    wire_api: WireAPI
    responses_support: ResponsesSupport
    usage_adapter: str
    models_endpoint: str
    validation_endpoint: str
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    region: str | None = None
    # True also covers providers where the absence of financial 429s is unverified.
    money_may_be_gone_on_429: bool = True
    catalog_public: bool = False

    def headers(self, key: str) -> dict[str, str]:
        headers = {self.auth_header: self.auth_prefix + key}
        if self.id == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers


def _provider(id: str, name: str, base: str, wire: WireAPI,
              support: ResponsesSupport, **kwargs: Any) -> Provider:
    return Provider(id, name, base, wire, support, id,
                    kwargs.pop("models_endpoint", base + "/models"),
                    kwargs.pop("validation_endpoint", base + "/models"), **kwargs)


PROVIDERS = {
    p.id: p for p in (
        _provider("openai", "OpenAI", "https://api.openai.com/v1", "responses", "verified"),
        _provider("anthropic", "Anthropic", "https://api.anthropic.com/v1", "messages",
                  "unsupported", auth_header="x-api-key", auth_prefix=""),
        _provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "responses",
                  "verified", validation_endpoint="https://openrouter.ai/api/v1/key",
                  catalog_public=True),
        _provider("langdock", "Langdock EU", "https://api.langdock.com/openai/eu/v1",
                  "responses", "verified", region="eu"),
        _provider("deepseek", "DeepSeek", "https://api.deepseek.com", "responses", "verified",
                  validation_endpoint="https://api.deepseek.com/user/balance"),
        _provider("xai", "xAI", "https://api.x.ai/v1", "responses", "verified",
                  models_endpoint="https://api.x.ai/v1/language-models"),
        _provider("groq", "Groq", "https://api.groq.com/openai/v1", "responses", "beta",
                  money_may_be_gone_on_429=False),
        _provider("together", "Together", "https://api.together.ai/v1", "chat", "unverified"),
        _provider("mistral", "Mistral", "https://api.mistral.ai/v1", "chat", "unverified"),
        # Pin the native public feed: prompt/completion are STRINGS in USD/token. Never append
        # format=huggingface or format=openrouter without changing the adapter too.
        _provider("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "chat", "unverified",
                  models_endpoint="https://api.cerebras.ai/public/v1/models", catalog_public=True),
        _provider("openai_compatible", "OpenAI-compatible", "", "responses", "unverified",
                  models_endpoint="", validation_endpoint=""),
    )
}


def get_provider(id: str, *, region: str | None = None,
                 base_url: str | None = None) -> Provider:
    p = PROVIDERS[id]
    if id == "langdock":
        region = region or "eu"
        if region not in ("eu", "us", "global"):
            raise ValueError("Langdock region must be eu, us or global")
        base = f"https://api.langdock.com/openai/{region}/v1"
        return replace(p, base_url=base, region=region, display_name=f"Langdock {region.upper()}",
                       models_endpoint=base + "/models", validation_endpoint=base + "/models")
    if id == "openai_compatible":
        parts = urlsplit(base_url or "")
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError("A custom HTTP(S) base_url without credentials/query is required")
        base = (base_url or "").rstrip("/")
        return replace(p, base_url=base, models_endpoint=base + "/models",
                       validation_endpoint=base + "/models")
    if base_url is not None or region is not None:
        raise ValueError("Only custom providers accept base_url; only Langdock accepts region")
    return p


def classify_error(provider: str | Provider, status: int, body: str | dict) -> str:
    """Classify documented wire pairs, without modifying the caller's original error fields.

    Sources: platform.claude.com/docs/en/api/errors, openrouter.ai/docs/api_reference/limits,
    console.groq.com/docs/spend-limits. No documented discriminator here distinguishes revoked
    keys from other invalid keys: ``revoked`` is reserved, never inferred from message text.
    """
    id = provider.id if isinstance(provider, Provider) else provider
    try:
        data = json.loads(body, parse_float=Decimal) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return "unknown"
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "unknown"
    code, kind = error.get("code"), error.get("type")
    if id == "openai":
        # The 429 bucket carries BOTH throttling and exhausted money, separated only by `code`.
        # Collapsing the spend codes into "unknown" would tell a user with an empty balance
        # nothing about why their key stopped working, so each documented code is mapped.
        # `slow_down` is the documented ramp signal and is genuinely transient.
        return {(401, "invalid_api_key"): "invalid_key",
                (429, "insufficient_quota"): "insufficient_quota",
                (429, "credit_balance_exhausted"): "insufficient_quota",
                (429, "organization_spend_limit_exceeded"): "insufficient_quota",
                (429, "project_spend_limit_exceeded"): "insufficient_quota",
                (429, "organization_usage_limit_exceeded"): "insufficient_quota",
                (429, "rate_limit_exceeded"): "rate_limited",
                (429, "slow_down"): "rate_limited"}.get(
                    (status, code), "unknown") if isinstance(code, str) else "unknown"
    if id == "anthropic":
        # rate_limit_error also describes monthly spend caps. Without headers there is no
        # documented discriminator separating those from throttling, so that pair stays unknown.
        return {(401, "authentication_error"): "invalid_key",
                (402, "billing_error"): "insufficient_quota"}.get(
                    (status, kind), "unknown") if isinstance(kind, str) else "unknown"
    if id == "openrouter" and type(code) is int and code == status:
        metadata = error.get("metadata", {})
        if not isinstance(metadata, dict):
            return "unknown"
        source = metadata.get("limit_source")
        if status == 401:
            return "invalid_key"
        if status == 402:
            if source == "openrouter_in_flight_budget":
                return "rate_limited"
            if source in (None, "openrouter_credits", "openrouter_key_limit"):
                return "insufficient_quota"
        if status == 429 and source is None:
            return "rate_limited"
        # New limit_source values are not silently collapsed to a generic retry policy.
    if id == "groq" and status == 400 and code == "blocked_api_access":
        return "insufficient_quota"
    # In particular, compatible servers do not inherit OpenAI's error vocabulary.
    return "unknown"


def is_retryable(provider: str | Provider, status: int, body: str | dict) -> bool:
    """Only a positively identified transient limit is retryable by this taxonomy."""
    return classify_error(provider, status, body) == "rate_limited"
