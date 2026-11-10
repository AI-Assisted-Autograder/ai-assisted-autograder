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
    basis: str = Field(min_length=1)


class RequestAccounting(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model_name: str
    usage: TokenUsage
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    pricing: ModelPrice | None = None


def default_model_prices() -> list[ModelPrice]:
    """Google Cloud Standard/Global published rates, including promotional credits."""
    common = {
        "engine": "pitt",
        "model": "@pitt-ai-connect-google-vertex/gemini-4-argon",
        "response_model": "gemini-4-argon",
        "basis": "Google Cloud Standard / Global; public rates including applicable credits",
    }
    return [
        ModelPrice(**common, effective_from=date(2026, 10, 13),
                   input_per_million=Decimal("2"), cached_input_per_million=Decimal("0.10"),
                   output_per_million=Decimal("10")),
        ModelPrice(**common, effective_from=date(2026, 12, 1),
                   input_per_million=Decimal("4"), cached_input_per_million=Decimal("0.20"),
                   output_per_million=Decimal("20")),
    ]


def select_price(prices: list[ModelPrice], engine: str, model: str, day: date) -> ModelPrice | None:
    eligible = [price for price in prices
                if price.engine == engine and price.model == model and price.effective_from <= day]
    return max(eligible, key=lambda price: price.effective_from, default=None)


def estimate_cost(usage: TokenUsage, model_name: str, pricing: ModelPrice | None) -> RequestAccounting:
    amount = None
    if (pricing is not None and pricing.response_model == model_name
            and usage.input_tokens is not None and usage.output_tokens is not None
            and usage.cached_input_tokens is not None
            and usage.cached_input_tokens <= usage.input_tokens):
        amount = (
            (usage.input_tokens - usage.cached_input_tokens) * pricing.input_per_million
            + usage.cached_input_tokens * pricing.cached_input_per_million
            + usage.output_tokens * pricing.output_per_million
        ) / Decimal(1_000_000)
    return RequestAccounting(model_name=model_name, usage=usage,
                             estimated_cost_usd=amount, pricing=pricing)
