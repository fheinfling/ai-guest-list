"""Provider-only model discovery with live prices or explicit unknowns.

All token rates are normalised to native currency per million tokens; request fees retain their
per-request unit. A catalog entry does not establish harness compatibility. Cached catalogs retain
their live provenance and fetch time; no offline price table supplies missing rates.
Network and time are injectable, and authenticated GETs never follow redirects.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from .providers import Provider, WireAPI

HttpGet = Callable[[str, dict, float], tuple[int, str]]
MILLION = Decimal(1_000_000)


def format_rate(value: Decimal | str | None) -> str | None:
    """Render a rate the way a person reads a price, not the way a catalog stores one.

    Providers publish full precision — "3.000000", "0.00000025" — and printing that raw makes a
    decision surface look like machine output. Values of a dollar or more carry at most two
    decimals, since a third never changes a judgement at that scale. Smaller ones keep three
    significant figures instead of a fixed place count, because per-million-token rates run down to
    fractions of a cent and a fixed two decimals would collapse most of a catalog to "$0.00".
    Trailing zeros go in both cases. None stays None: an unknown price is never a formatted zero.

    Mirrors formatPrice in app/web/render.mjs; the two must agree, and tests compare them against
    the same real catalog.
    """
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    if amount == 0:
        return "0"
    # Half-UP, not Python's default half-even: a price ending exactly on a half should read the way
    # money is normally rounded, and the web formatter (toPrecision) already rounds that way. The
    # two surfaces must never print different numbers for the same model — 0.3125 is $0.313 in both.
    if amount >= 1:
        quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        # Three significant figures: quantize at the decade two below the leading digit.
        quantized = amount.quantize(Decimal(1).scaleb(amount.adjusted() - 2),
                                    rounding=ROUND_HALF_UP)
    text = format(quantized.normalize(), "f")
    return text


CACHE_TTL = timedelta(hours=6)
CACHED_STALE_AFTER = timedelta(hours=24)
RATE_NAMES = ("input", "output", "cached_input", "cache_write", "cache_write_5m",
              "cache_write_1h", "reasoning", "request")


def decimal(value: Any) -> Decimal:
    """Reject floats: once binary rounding happened, financial precision is already lost."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Expected a decimal string, integer or Decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as e:
        raise ValueError("Invalid decimal") from e
    if not result.is_finite() or result < 0:
        raise ValueError("Expected a finite nonnegative amount")
    return result


@dataclass(frozen=True)
class Rate:
    status: Literal["known", "unknown", "same_as", "not_applicable"] = "unknown"
    value: Decimal | None = None
    same_as: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("known", "unknown", "same_as", "not_applicable"):
            raise ValueError("Invalid rate status")
        if self.status == "known":
            object.__setattr__(self, "value", decimal(self.value))
        elif self.value is not None:
            raise ValueError("Only known rates have a value")
        if self.status == "same_as" and self.same_as not in RATE_NAMES:
            raise ValueError("same_as must name another rate")


@dataclass(frozen=True)
class Model:
    provider: str
    id: str
    display_name: str
    wire_api: WireAPI
    currency: str = "USD"
    rates: dict[str, Rate] = field(default_factory=dict)
    reasoning_included_in_output: bool | None = None
    verified_at: str | None = None  # When this live catalog was fetched, if known.
    source: Literal["live"] = "live"
    source_url: str = ""
    created: Decimal | None = None  # Unix seconds, including fractional milliseconds.
    context_window: int | None = None  # Capacity, independent of a pricing tier threshold.
    long_context_threshold: int | None = None
    long_context_rates: dict[str, Rate] = field(default_factory=dict)

    def rate(self, name: str, *, long_context: bool = False,
             _seen: frozenset[str] = frozenset()) -> Decimal | None:
        if name in _seen:
            return None
        rates = {**self.rates, **self.long_context_rates} if long_context else self.rates
        rate = rates.get(name, Rate())
        if rate.status == "same_as":
            return self.rate(rate.same_as or "", long_context=long_context, _seen=_seen | {name})
        return rate.value if rate.status == "known" else None

    @property
    def prices_complete(self) -> bool:
        """Whether every price dimension is known, not a prerequisite for running or metering.

        An individual response may use only known dimensions or report its monetary cost.
        """
        if self.reasoning_included_in_output is None or not self.currency:
            return False
        if self.long_context_rates and self.long_context_threshold is None:
            return False
        for long in (False, True) if self.long_context_rates else (False,):
            rates = {**self.rates, **self.long_context_rates} if long else self.rates
            for name in RATE_NAMES:
                rate = rates.get(name, Rate())
                if rate.status != "not_applicable" and self.rate(name, long_context=long) is None:
                    return False
            if any(self.rate(name, long_context=long) is None for name in ("input", "output")):
                return False
        return True


def _input_price(model: Model) -> Decimal | None:
    # The picker names USD/Mtok explicitly; unknown currencies cannot share that ordering.
    return model.rate("input") if model.source == "live" and model.currency == "USD" else None


def has_live_prices(models: list[Model]) -> bool:
    """Whether any live USD input rate makes cost ordering meaningful, including known zero."""
    return any(_input_price(model) is not None for model in models)


def model_sort_key(models: list[Model]) -> Literal["input_usd_per_million_tokens", "id"]:
    """Expose the ordering for the picker header; partial catalogs need not be fully priced."""
    return "input_usd_per_million_tokens" if has_live_prices(models) else "id"


def sort_models(models: list[Model]) -> list[Model]:
    """Order by live input USD/Mtok, otherwise id; preserve discovery order for equal keys."""
    if model_sort_key(models) == "id":
        return sorted(models, key=lambda model: model.id)

    def key(model: Model) -> Decimal:
        price = _input_price(model)
        return price if price is not None else Decimal("Infinity")

    return sorted(models, key=key)


def _model(data: dict) -> Model:
    data = dict(data)
    if data.get("source") != "live":
        raise ValueError("Cached model must originate from a live catalog")
    for key in ("rates", "long_context_rates"):
        data[key] = {name: Rate(**value) for name, value in data.get(key, {}).items()}
    if data.get("created") is not None:
        data["created"] = decimal(data["created"])
    if data.get("reasoning_included_in_output") is not None and type(
            data["reasoning_included_in_output"]) is not bool:
        raise ValueError("Reasoning inclusion must be true, false or null")
    return Model(**data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        finally:
            e.close()
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, ""


def _rate(value: Any, scale: Decimal = Decimal(1)) -> Rate:
    try:
        return Rate("known", decimal(value) * scale)
    except ValueError:
        return Rate()


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _context_window(provider: str, row: dict) -> int | None:
    """Read only documented capacity fields, never pricing thresholds or guessed schemas."""
    field = {"anthropic": "max_input_tokens", "deepseek": "context_window",
             "groq": "context_window", "mistral": "max_context_length",
             "openrouter": "context_length", "together": "context_length"}.get(provider)
    candidate = row.get(field) if field else None
    if type(candidate) is int and candidate > 0:
        return candidate
    nested = {"openrouter": ("top_provider", "context_length"),
              "cerebras": ("limits", "max_context_length")}.get(provider)
    if nested:
        parent, field = nested
        value = row.get(parent)
        candidate = value.get(field) if isinstance(value, dict) else None
        if type(candidate) is int and candidate > 0:
            return candidate
    return None


def parse_catalog(provider: Provider, text: str, *, wire_api: WireAPI | None = None,
                  verified_at: str | None = None) -> list[Model]:
    """Adapt only the selected billing provider's catalog, retaining unknown rates as unknown.

    Langdock's completion catalog schema is unverified: consume only id/name from its data list,
    never copy timestamps or prices from the distinct agent catalog into an inference model.
    """
    data = json.loads(text, parse_float=Decimal)
    id, wire = provider.id, wire_api or provider.wire_api
    if id == "together":
        rows = data  # docs.together.ai/reference/models: bare array, USD/Mtok.
    elif isinstance(data, dict):
        rows = data.get("models" if id == "xai" else "data")
    else:
        rows = None
    if not isinstance(rows, list):
        # UNVERIFIED against Langdock's docs: assume an Anthropic-style data list.
        # A different schema means no discoverable models, not a parsing exception.
        if id == "langdock_anthropic":
            return []
        raise ValueError("Unexpected catalog envelope")
    models = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        rates = {name: Rate() for name in RATE_NAMES}
        rates.update({name: Rate("not_applicable") for name in
                      ("cache_write_5m", "cache_write_1h", "request")})
        inclusion = (False if wire == "chat" else True) if id == "xai" else (
            True if id in ("openai", "anthropic", "openrouter", "deepseek", "cerebras") else None)
        rates["reasoning"] = Rate("same_as", same_as="output") if inclusion is not None else Rate()
        prices = row.get("pricing", {})
        prices = prices if isinstance(prices, dict) else {}
        long_rates: dict[str, Rate] = {}
        threshold = None
        if id in ("openrouter", "cerebras"):
            for name, key in (("input", "prompt"), ("output", "completion")):
                rates[name] = _rate(prices.get(key), MILLION)
            if id == "openrouter":
                for name, key in (("cached_input", "input_cache_read"),
                                  ("cache_write", "input_cache_write")):
                    rates[name] = _rate(prices.get(key), MILLION)
                rates["request"] = _rate(prices.get("request"))
        elif id == "together":
            for name in ("input", "output", "cached_input"):
                rates[name] = _rate(prices.get(name))
        elif id == "xai":
            # docs.x.ai/developers/rest-api-reference/inference/models
            for name, key in (("input", "prompt_text_token_price"),
                              ("output", "completion_text_token_price"),
                              ("cached_input", "cached_prompt_text_token_price")):
                rates[name] = _rate(row.get(key), Decimal("0.0001"))
                if key + "_long_context" in row:
                    long = row[key + "_long_context"]
                    long_rates[name] = rates[name] if long == 0 else _rate(long, Decimal("0.0001"))
            candidate = row.get("long_context_threshold")
            threshold = candidate if type(candidate) is int and candidate >= 0 else None
            if threshold == 0:
                long_rates = {}
            rates["cache_write"] = Rate("not_applicable")
        created = None
        if id not in ("langdock", "langdock_anthropic") and row.get("created") is not None:
            try:
                created = decimal(row["created"])
            except ValueError:
                pass
        model = Model(id, row["id"], row.get("display_name") or row.get("name") or row["id"],
                      wire, rates=rates, reasoning_included_in_output=inclusion,
                      verified_at=verified_at, source="live", source_url=provider.models_endpoint,
                      created=created, context_window=_context_window(id, row),
                      long_context_threshold=threshold, long_context_rates=long_rates)
        models.append(model)
    return models


def parse_langdock_agent_catalog(text: str) -> list[dict[str, Any]]:
    """Agent discovery is separate from completion discovery; created is milliseconds here."""
    data = json.loads(text, parse_float=Decimal)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected Langdock agent catalog envelope")
    return [{**row, "created": decimal(row["created"]) / 1000}
            for row in rows if isinstance(row, dict) and "created" in row]


@dataclass(frozen=True)
class Catalog:
    models: list[Model]
    source: str
    fetched_at: datetime | None
    checked_at: datetime
    error: str | None = None

    @property
    def cache_expired(self) -> bool:
        return self.fetched_at is None or self.checked_at - self.fetched_at >= CACHE_TTL

    @property
    def potentially_stale(self) -> bool:
        return (self.source == "cache" and self.fetched_at is not None
                and self.checked_at - self.fetched_at > CACHED_STALE_AFTER)


def _sanitize_catalog(value: Any, key: str) -> Any:
    """Redact decoded credentials, including future model fields and nested mapping keys.

    Match key-seat validation's minimum length: empty/public keys and tiny placeholders must
    not rewrite ordinary catalog text. Keep models and their non-secret metadata intact.
    """
    if not key or len(key) <= 4:
        return value
    if isinstance(value, str):
        clean = value.replace(key, "[redacted]")
        # The marker (possibly joined to adjacent text) must not recreate an unusual key.
        # This fallback strictly shrinks the string because keys here exceed four characters.
        while key in clean:
            clean = clean.replace(key, "***")
        return clean
    if is_dataclass(value):
        return replace(value, **{f.name: _sanitize_catalog(getattr(value, f.name), key)
                                 for f in fields(value)})
    if isinstance(value, dict):
        return {_sanitize_catalog(k, key): _sanitize_catalog(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_catalog(v, key) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_catalog(v, key) for v in value)
    return value


def _write_catalog_cache(path: Path, cache: dict) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(cache, f, default=str)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def fetch_catalog(provider: Provider, key: str = "", *, get: HttpGet = _default_get,
                  cache_path: Path | None = None, at: datetime | None = None,
                  timeout: float = 20, wire_api: WireAPI | None = None) -> Catalog:
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Use a timezone-aware clock")
    path = cache_path if cache_path is not None else Path.home() / ".account-switcher/pricing.json"
    wire = wire_api or provider.wire_api
    # Account permissions and custom hosts must not share catalog entries. Never persist the key.
    identity = json.dumps([provider.id, provider.models_endpoint, wire,
                           hashlib.sha256(key.encode()).hexdigest()])
    cache_key = hashlib.sha256(identity.encode()).hexdigest()
    cache: dict = {}
    cached = None
    contaminated = False
    try:
        cache = json.loads(path.read_text(), parse_float=Decimal)
        if not isinstance(cache, dict):
            cache = {}
        entry = cache.get(cache_key)
        if isinstance(entry, dict):
            clean_entry = _sanitize_catalog(entry, key)
            contaminated = clean_entry != entry
            cache[cache_key] = entry = clean_entry
            fetched = _date(entry["fetched_at"])
            if fetched <= at:
                cached = Catalog([_model(m) for m in entry["models"]], "cache", fetched, at)
    except (OSError, ValueError, KeyError, TypeError):
        cache = {}
    if contaminated:
        # Repair even fresh entries and stale fallbacks before any return or failed refresh.
        try:
            _write_catalog_cache(path, cache)
        except OSError:
            # A disposable cache must not retain a known credential if replacement failed.
            # If even removal is denied, surface that failure instead of silently keeping it.
            path.unlink(missing_ok=True)
    if cached is not None and not cached.cache_expired:
        return cached
    try:
        status, body = get(provider.models_endpoint,
                           {} if provider.catalog_public else provider.headers(key), timeout)
        if status != 200:
            raise ValueError(f"http_{status}")
        models = parse_catalog(provider, body, wire_api=wire, verified_at=at.isoformat())
        models = _sanitize_catalog(models, key)
        if not models:
            if provider.id == "langdock_anthropic":
                return Catalog([], "unavailable", None, at, "no_models")
            raise ValueError("empty_catalog")
    except (OSError, ValueError, TypeError) as e:
        error = str(e) if isinstance(e, ValueError) and str(e).startswith("http_") else "catalog_unavailable"
        return replace(cached, error=error) if cached else Catalog(
            [], "unavailable", None, at, error)
    cache[cache_key] = {"fetched_at": at.isoformat(), "models": [asdict(m) for m in models]}
    try:
        _write_catalog_cache(path, cache)
    except OSError:
        pass  # A read-only cache must not discard a successfully fetched catalog.
    return Catalog(models, "live", at, at)
