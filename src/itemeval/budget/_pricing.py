"""Pricing table: packaged seed + optional OpenRouter refresh + user overrides."""

import json
import os
import urllib.request
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from itemeval._errors import BudgetError
from itemeval._util import atomic_write_bytes, utc_now_iso

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
BATCH_PROVIDERS = {"openai", "anthropic", "google", "grok", "together"}


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float | None = None  # None -> 0.1 * input
    cache_write_usd_per_mtok: float | None = None  # None -> 1.25 * input


class PricingTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at: str
    source: str  # "seed" | "openrouter" | "merged" | "file"
    models: dict[str, ModelPrice]


def _parse_pricing(raw: bytes, origin: str) -> PricingTable:
    try:
        return PricingTable.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise BudgetError(f"invalid pricing table {origin}: {e}") from e


def seed_pricing() -> PricingTable:
    raw = files("itemeval.budget").joinpath("pricing_seed.json").read_bytes()
    return _parse_pricing(raw, "pricing_seed.json")


def user_pricing_path() -> Path:
    env = os.environ.get("ITEMEVAL_PRICING_PATH")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "itemeval" / "pricing.json"


def load_pricing(explicit_path: "str | None", base_dir: Path) -> PricingTable:
    """Precedence: explicit config path -> user cache file -> packaged seed."""
    if explicit_path is not None:
        p = (base_dir / explicit_path).resolve()
        if not p.is_file():
            raise BudgetError(f"budget.pricing_path not found: {p}")
        return _parse_pricing(p.read_bytes(), str(p))
    user = user_pricing_path()
    if user.is_file():
        return _parse_pricing(user.read_bytes(), str(user))
    return seed_pricing()


def refresh_pricing(timeout: float = 30.0) -> PricingTable:
    """Merge OpenRouter's live pricing API over the seed; persist to the user cache."""
    try:
        with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
        entries = data["data"]
    except Exception as e:
        raise BudgetError(f"OpenRouter pricing refresh failed: {e}") from e

    table = seed_pricing()
    for entry in entries:
        model_id = entry.get("id")
        pricing = entry.get("pricing") or {}
        try:
            inp = float(pricing["prompt"]) * 1e6
            out = float(pricing["completion"]) * 1e6
        except (KeyError, TypeError, ValueError):
            continue
        price = ModelPrice(input_usd_per_mtok=inp, output_usd_per_mtok=out)
        table.models[f"openrouter/{model_id}"] = price
        if model_id not in table.models:  # seed wins for native ids
            table.models[model_id] = price
    merged = PricingTable(updated_at=utc_now_iso(), source="merged", models=table.models)
    atomic_write_bytes(
        user_pricing_path(),
        (merged.model_dump_json(indent=2) + "\n").encode("utf-8"),
    )
    return merged


def lookup_price(table: PricingTable, model: str) -> "ModelPrice | None":
    if model in table.models:
        return table.models[model]
    if model.startswith("mockllm/") and "mockllm/*" in table.models:
        return table.models["mockllm/*"]
    if f"openrouter/{model}" in table.models:
        return table.models[f"openrouter/{model}"]
    if model.startswith("openrouter/") and model[len("openrouter/") :] in table.models:
        return table.models[model[len("openrouter/") :]]
    return None


def cost_usd(
    price: ModelPrice,
    input_tokens: "int | None",
    output_tokens: "int | None",
    cache_read: "int | None" = 0,
    cache_write: "int | None" = 0,
) -> float:
    crp = price.cache_read_usd_per_mtok
    cwp = price.cache_write_usd_per_mtok
    if crp is None:
        crp = 0.1 * price.input_usd_per_mtok
    if cwp is None:
        cwp = 1.25 * price.input_usd_per_mtok
    return (
        (input_tokens or 0) * price.input_usd_per_mtok
        + (output_tokens or 0) * price.output_usd_per_mtok
        + (cache_read or 0) * crp
        + (cache_write or 0) * cwp
    ) / 1e6


def provider_of(model: str) -> str:
    return model.split("/")[0]
