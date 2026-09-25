"""This cost meter measures spend for display and never enforces spending limits.

Provider adapters produce disjoint billable counts; a shared input+output+reasoning formula would
double-charge several APIs. Money stays Decimal in its native currency. SQLite stores decimal
strings and response IDs atomically so replay after resume cannot increment the ledger twice.
No launcher, application state or credentials are involved in this engine module.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .pricing import MILLION, Model, decimal


class PriceUnavailable(ValueError):
    """A number cannot be displayed without guessing; this never means refuse to run."""


@dataclass(frozen=True)
class Unavailable:
    """An explicit display result, distinct from a known zero or a partial total."""

    reason: str


@dataclass(frozen=True)
class Tokens:
    counts: dict[str, Decimal]
    prompt_tokens: Decimal
    reasoning_tokens: Decimal


@dataclass(frozen=True)
class Charge:
    amount: Decimal
    currency: str
    source: str  # provider_reported | estimated


def _count(data: dict, key: str, *, required: bool = False) -> Decimal:
    if key not in data and required:
        raise PriceUnavailable(f"Missing {key}")
    try:
        value = decimal(data.get(key, 0))
    except ValueError as e:
        raise PriceUnavailable(f"Invalid {key}") from e
    if value != value.to_integral_value():
        raise PriceUnavailable(f"Non-integral {key}")
    return value


def _details(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise PriceUnavailable(f"Invalid {key}")
    return value


def _inclusive(usage: dict, model: Model) -> Tokens:
    """For APIs whose prompt total includes cached reads/writes, replace those token rates."""
    chat = model.wire_api == "chat"
    input_key, output_key = ("prompt_tokens", "completion_tokens") if chat else ("input_tokens", "output_tokens")
    prompt = _count(usage, input_key, required=True)
    output = _count(usage, output_key, required=True)
    details = _details(usage, input_key + "_details")
    read = _count(details, "cached_tokens")
    write = _count(details, "cache_write_tokens")
    ordinary = prompt - read - write
    if ordinary < 0:
        raise PriceUnavailable("Cache tokens exceed inclusive prompt total")
    reasoning = _count(_details(usage, output_key + "_details"), "reasoning_tokens")
    if model.reasoning_included_in_output and reasoning > output:
        raise PriceUnavailable("Reasoning exceeds inclusive output total")
    counts = {"input": ordinary, "cached_input": read, "cache_write": write, "output": output}
    if model.reasoning_included_in_output is False:
        counts["reasoning"] = reasoning
    return Tokens(counts, prompt, reasoning)


def _openai(usage: dict, model: Model) -> Tokens:
    return _inclusive(usage, model)


def _anthropic(usage: dict, model: Model) -> Tokens:
    prompt = _count(usage, "input_tokens", required=True)
    output = _count(usage, "output_tokens", required=True)
    read = _count(usage, "cache_read_input_tokens")
    counts = {"input": prompt, "output": output, "cached_input": read}
    creation = _details(usage, "cache_creation")
    aggregate = _count(usage, "cache_creation_input_tokens")
    if "ephemeral_5m_input_tokens" in creation or "ephemeral_1h_input_tokens" in creation:
        five = _count(creation, "ephemeral_5m_input_tokens")
        hour = _count(creation, "ephemeral_1h_input_tokens")
        if "cache_creation_input_tokens" in usage and five + hour != aggregate:
            raise PriceUnavailable("Cache creation decomposition does not match aggregate")
        counts.update(cache_write_5m=five, cache_write_1h=hour)
        writes = five + hour
    else:
        # Without TTL details, only an explicitly supplied aggregate price is safe.
        counts["cache_write"] = writes = aggregate
    thinking = _count(_details(usage, "output_tokens_details"), "thinking_tokens")
    if thinking > output:
        raise PriceUnavailable("Thinking exceeds inclusive output total")
    return Tokens(counts, prompt + read + writes, thinking)


def _openrouter(usage: dict, model: Model) -> Tokens:
    return _inclusive(usage, model)


def _deepseek(usage: dict, model: Model) -> Tokens:
    if model.wire_api == "responses":
        return _inclusive(usage, model)
    tokens = _inclusive(usage, model)
    hit = _count(usage, "prompt_cache_hit_tokens")
    miss = tokens.prompt_tokens - hit
    if miss < 0 or ("prompt_cache_miss_tokens" in usage
                    and _count(usage, "prompt_cache_miss_tokens") != miss):
        raise PriceUnavailable("DeepSeek cache counts do not match prompt total")
    return Tokens({**tokens.counts, "input": miss, "cached_input": hit},
                  tokens.prompt_tokens, tokens.reasoning_tokens)


def _xai(usage: dict, model: Model) -> Tokens:
    # Chat's documented 32 prompt + 9 completion + 94 reasoning = 135 total differs from
    # Responses, where reasoning is already in output_tokens. The API is part of model identity.
    expected = model.wire_api != "chat"
    if model.reasoning_included_in_output is not expected:
        raise PriceUnavailable("xAI reasoning inclusion does not match the wire API")
    return _inclusive(usage, model)


def _cerebras(usage: dict, model: Model) -> Tokens:
    return _inclusive(usage, model)


def _configured_compatible(usage: dict, model: Model) -> Tokens:
    # Discovery leaves reasoning inclusion null for these providers. Only an explicit verified
    # model record can opt into their compatible counters; the adapter never infers inclusion.
    if model.wire_api == "messages":
        raise PriceUnavailable("Unverified Messages accounting")
    return _inclusive(usage, model)


USAGE_ADAPTERS: dict[str, Callable[[dict, Model], Tokens]] = {
    "openai": _openai, "anthropic": _anthropic, "openrouter": _openrouter,
    "deepseek": _deepseek, "xai": _xai, "cerebras": _cerebras,
    "langdock": _configured_compatible, "groq": _configured_compatible,
    "together": _configured_compatible, "mistral": _configured_compatible,
    "openai_compatible": _configured_compatible,
}


def adapt_usage(model: Model, usage: dict) -> Tokens:
    if model.reasoning_included_in_output is None:
        raise PriceUnavailable("Unknown reasoning inclusion")
    if model.provider in ("openai", "anthropic", "openrouter", "deepseek", "cerebras") and (
            model.reasoning_included_in_output is not True):
        raise PriceUnavailable("Reasoning inclusion contradicts provider accounting")
    adapter = USAGE_ADAPTERS.get(model.provider)
    if adapter is None:
        raise PriceUnavailable("Unknown usage adapter")
    return adapter(usage, model)


def estimate(model: Model, usage: dict | str) -> Charge:
    """Price one final response's usage, never cumulative thread counters.

    Provider-reported USD takes precedence even if catalog prices are unavailable. Missing
    information raises PriceUnavailable; display callers can use try_estimate instead.
    """
    if isinstance(usage, str):
        try:
            usage = json.loads(usage, parse_float=Decimal)
        except ValueError as e:
            raise PriceUnavailable("Invalid usage JSON") from e
    if not isinstance(usage, dict):
        raise PriceUnavailable("Missing usage")
    money_key = {"openrouter": "cost", "xai": "cost_in_usd_ticks"}.get(model.provider)
    if money_key and usage.get(money_key) is not None:
        try:
            amount = decimal(usage[money_key])
        except ValueError as e:
            raise PriceUnavailable("Invalid provider-reported cost") from e
        if money_key == "cost_in_usd_ticks":
            amount /= Decimal(10**10)
        return Charge(amount, "USD", "provider_reported")
    tokens = adapt_usage(model, usage)
    if not model.currency:
        raise PriceUnavailable("Unknown currency")
    if model.long_context_rates and model.long_context_threshold is None:
        raise PriceUnavailable("Unknown long-context threshold")
    long = bool(model.long_context_threshold and tokens.prompt_tokens >= model.long_context_threshold)
    total = Decimal(0)
    for name, count in tokens.counts.items():
        if count == 0:
            continue
        rate = model.rate(name, long_context=long)
        if rate is None:
            raise PriceUnavailable(f"Price unavailable: {name}")
        total += count * rate / MILLION
    request = model.rates.get("request")
    if request is None or request.status != "not_applicable":
        fee = model.rate("request", long_context=long)
        if fee is None:
            raise PriceUnavailable("Price unavailable: request")
        total += fee
    return Charge(total, model.currency, "estimated")


def try_estimate(model: Model, usage: dict | str) -> Charge | Unavailable:
    """Return an estimate or its unavailability reason without interrupting the caller."""
    try:
        return estimate(model, usage)
    except PriceUnavailable as e:
        return Unavailable(str(e))


@dataclass(frozen=True)
class Entry:
    response_id: str
    session_id: str
    month: str
    charge: Charge | None
    error: str | None = None


class Ledger:
    """One native-currency cost meter, optionally persisted to a caller-owned SQLite path.

    Session totals span resumed runs and month boundaries; month totals span all sessions. UTC
    calendar months are the default, with an injectable timezone for local calendar totals.
    Unknown responses are stored as NULL, never zero; affected totals return Unavailable.
    """

    def __init__(self, *, currency: str = "USD", path: Path | None = None,
                 calendar_timezone: tzinfo = timezone.utc):
        self.currency = currency
        self.calendar_timezone = calendar_timezone
        self._db = sqlite3.connect(str(path) if path is not None else ":memory:")
        self._db.execute("""CREATE TABLE IF NOT EXISTS responses (
            response_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, month TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, wire_api TEXT NOT NULL,
            currency TEXT NOT NULL, amount TEXT, source TEXT, error TEXT)""")
        currencies = self._db.execute("SELECT DISTINCT currency FROM responses").fetchall()
        if any(row[0] != currency for row in currencies):
            self.close()
            raise ValueError("Ledger currency mismatch; no FX conversion is performed")

    def close(self) -> None:
        self._db.close()

    def _month(self, at: datetime) -> str:
        if at.tzinfo is None:
            raise ValueError("Use a timezone-aware timestamp")
        return at.astimezone(self.calendar_timezone).strftime("%Y-%m")

    def record(self, model: Model, response_id: str, session_id: str, usage: dict | str,
               *, at: datetime | None = None) -> Entry:
        if not response_id or not session_id:
            raise ValueError("response_id and session_id are required for deduplication")
        if model.currency != self.currency:
            raise ValueError("Ledger currency mismatch; no FX conversion is performed")
        month = self._month(at or datetime.now(timezone.utc))
        result = try_estimate(model, usage)
        charge = result if isinstance(result, Charge) else None
        error = result.reason if isinstance(result, Unavailable) else None
        if charge is not None and charge.currency != self.currency:
            raise ValueError("Reported currency differs from ledger currency")
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO responses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (response_id, session_id, month, model.provider, model.id, model.wire_api,
                              self.currency, str(charge.amount) if charge else None,
                              charge.source if charge else None, error))
            row = self._db.execute("SELECT * FROM responses WHERE response_id = ?", (response_id,)).fetchone()
            if (row[1], row[3], row[4], row[5]) != (session_id, model.provider, model.id, model.wire_api):
                raise ValueError("response_id was already recorded for a different response identity")
        settled = Charge(Decimal(row[7]), row[6], row[8]) if row[7] is not None else None
        return Entry(row[0], row[1], row[2], settled, row[9])

    def _total(self, field: str, value: str) -> Decimal | Unavailable:
        # SQLite SUM coerces TEXT to binary floats; sum parsed Decimals in Python instead.
        rows = self._db.execute(f"SELECT amount FROM responses WHERE {field} = ?", (value,)).fetchall()
        if any(row[0] is None for row in rows):
            return Unavailable("Spend includes an unpriced response")
        return sum((Decimal(row[0]) for row in rows), Decimal(0))

    def session_total(self, session_id: str) -> Decimal | Unavailable:
        return self._total("session_id", session_id)

    def monthly_total(self, at: datetime | None = None) -> Decimal | Unavailable:
        return self._total("month", self._month(at or datetime.now(timezone.utc)))
