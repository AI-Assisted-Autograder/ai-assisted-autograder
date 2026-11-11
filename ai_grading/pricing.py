"""Backend model prices and immutable estimates from reported token usage."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import TokenUsage


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    engine: Literal["vertex", "pitt", "gemini_api"]
    model: str = Field(min_length=1)
    response_model: str = Field(min_length=1)
    effective_from: date
    input_per_million: Decimal = Field(ge=0)
    cached_input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    cache_write_per_million: Decimal | None = Field(default=None, ge=0)
    basis: str = Field(min_length=1)


class RequestAccounting(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model_name: str
    usage: TokenUsage
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    pricing: ModelPrice | None = None


def default_model_prices() -> list[ModelPrice]:
    """Pitt's listed input/output rates supplied on 2026-11-10; public cache estimates."""
    common = {"engine": "pitt", "effective_from": date(2026, 11, 10)}
    google_basis = ("Pitt listed input/output (2026-11-10); Google Cloud public cache-read rates; "
                    "no assumed future price changes")
    anthropic_basis = ("Pitt listed input/output (2026-11-10); Anthropic public cache-read rates; "
                       "no regional surcharge")
    openai_basis = ("Pitt listed input/output (2026-11-10); OpenAI Standard public cache-read "
                    "and default 30-minute cache-write rates; short-context estimate")
    return [
        ModelPrice(**common, model="@pitt-ai-connect-google-vertex/gemini-4-argon",
                   response_model="gemini-4-argon", basis=google_basis,
                   input_per_million=Decimal("2"), cached_input_per_million=Decimal("0.10"),
                   output_per_million=Decimal("10")),
        ModelPrice(**common, model="@pitt-ai-connect-google-vertex/gemini-3.8-flash",
                   response_model="gemini-3.8-flash", basis=google_basis,
                   input_per_million=Decimal("0.75"), cached_input_per_million=Decimal("0.075"),
                   output_per_million=Decimal("3.75")),
        ModelPrice(**common, model="@pitt-ai-connect/us.anthropic.claude-sonnet-5-5",
                   response_model="us.anthropic.claude-sonnet-5-5", basis=anthropic_basis,
                   input_per_million=Decimal("2"), cached_input_per_million=Decimal("0.10"),
                   output_per_million=Decimal("10")),
        ModelPrice(**common, model="@pitt-ai-connect/us.anthropic.claude-opus-5-5",
                   response_model="us.anthropic.claude-opus-5-5", basis=anthropic_basis,
                   input_per_million=Decimal("4"), cached_input_per_million=Decimal("0.20"),
                   output_per_million=Decimal("20")),
        ModelPrice(**common, model="@pitt-ai-connect-azure-foundry/gpt-6.1-sol",
                   response_model="gpt-6.1-sol-2026-09-29", basis=openai_basis,
                   input_per_million=Decimal("2"), cached_input_per_million=Decimal("0.10"),
                   output_per_million=Decimal("10"), cache_write_per_million=Decimal("2.50")),
        ModelPrice(**common, model="@pitt-ai-connect-azure-foundry/gpt-6-luna",
                   response_model="gpt-6-luna-2026-09-22", basis=openai_basis,
                   input_per_million=Decimal("0.10"), cached_input_per_million=Decimal("0.01"),
                   output_per_million=Decimal("0.50"), cache_write_per_million=Decimal("0.125")),
    ]


def select_price(prices: list[ModelPrice], engine: str, model: str, day: date) -> ModelPrice | None:
    eligible = [price for price in prices
                if price.engine == engine and price.model == model and price.effective_from <= day]
    return max(eligible, key=lambda price: price.effective_from, default=None)


def estimate_cost(usage: TokenUsage, model_name: str, pricing: ModelPrice | None) -> RequestAccounting:
    amount = None
    cache_write_tokens = usage.cache_write_tokens or 0
    if (pricing is not None and pricing.response_model == model_name
            and usage.input_tokens is not None and usage.output_tokens is not None
            and usage.cached_input_tokens is not None
            and usage.cached_input_tokens + cache_write_tokens <= usage.input_tokens
            and (pricing.cache_write_per_million is None or usage.cache_write_tokens is not None)
            and (cache_write_tokens == 0 or pricing.cache_write_per_million is not None)):
        amount = (
            (usage.input_tokens - usage.cached_input_tokens - cache_write_tokens) * pricing.input_per_million
            + usage.cached_input_tokens * pricing.cached_input_per_million
            + cache_write_tokens * (pricing.cache_write_per_million or Decimal("0"))
            + usage.output_tokens * pricing.output_per_million
        ) / Decimal(1_000_000)
    return RequestAccounting(model_name=model_name, usage=usage,
                             estimated_cost_usd=amount, pricing=pricing)
