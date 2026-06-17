"""Pricing table: packaged seed + optional OpenRouter refresh + user overrides."""

import json
import os
import urllib.request
from datetime import datetime, timezone
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
    # None -> 1.25 * input for Anthropic-style explicit caching (write
    # surcharge); 0 for providers with free automatic cache writes (OpenAI,
    # Gemini implicit, DeepSeek, ...). See cache_write_default().
    cache_write_usd_per_mtok: float | None = None
    # Roster metadata captured on refresh, used to build / filter / stratify a
    # `solvers.sample` pricing-table universe. None for entries without
    # OpenRouter metadata (the packaged seed, pinned user tables).
    text_model: bool | None = None  # runnable text->text chat model (text in/out + params)
    reasoning: bool | None = None  # exposes a reasoning parameter
    multimodal: bool | None = None  # accepts more than text as input
    context_length: int | None = None  # max context window (tokens)
    # OpenRouter release timestamp, Unix seconds (the top-level `created` field;
    # verified unit 2026-06-17). Powers `released_after` filtering and the
    # `recency` stratify dimension; None for the packaged seed / pinned tables.
    created: int | None = None


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

        def _opt(key: str) -> "float | None":
            try:
                return float(pricing[key]) * 1e6  # noqa: B023 (loop var read eagerly)
            except (KeyError, TypeError, ValueError):
                return None

        # Roster metadata. Runnable text model: takes text, emits text, and
        # exposes generation parameters — the last clause drops OpenRouter's
        # meta/router entries (empty supported_parameters), not standard chat.
        arch = entry.get("architecture") or {}
        params = entry.get("supported_parameters") or []
        in_mods = arch.get("input_modalities") or []
        text_model = (
            "text" in in_mods and "text" in (arch.get("output_modalities") or []) and bool(params)
        )

        price = ModelPrice(
            input_usd_per_mtok=inp,
            output_usd_per_mtok=out,
            cache_read_usd_per_mtok=_opt("input_cache_read"),
            cache_write_usd_per_mtok=_opt("input_cache_write"),
            text_model=text_model,
            reasoning="reasoning" in params,
            multimodal=len(in_mods) > 1,
            context_length=entry.get("context_length"),
            created=entry.get("created"),
        )
        table.models[f"openrouter/{model_id}"] = price
        if model_id not in table.models:  # seed wins for native ids
            table.models[model_id] = price
    merged = PricingTable(updated_at=utc_now_iso(), source="merged", models=table.models)
    atomic_write_bytes(
        user_pricing_path(),
        (merged.model_dump_json(indent=2) + "\n").encode("utf-8"),
    )
    return merged


class PricingProvenance(BaseModel):
    """Where the prices behind a projection/report came from, and how fresh."""

    model_config = ConfigDict(extra="forbid")

    source: str  # "seed" | "openrouter" | "merged" | "file"
    updated_at: str
    age_days: float | None  # None when updated_at is unparseable
    refreshed: bool  # a live OpenRouter refresh ran during this load


def _table_age_days(table: PricingTable) -> "float | None":
    """Age of the table in days from its `updated_at`; None if unparseable."""
    try:
        stamped = datetime.strptime(table.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - stamped).total_seconds() / 86400.0


def maybe_refresh_pricing(
    table: PricingTable, max_age_days: "float | None", *, timeout: float = 30.0
) -> PricingTable:
    """Best-effort staleness refresh used for `budget.pricing_max_age_days`.

    Returns a freshly merged OpenRouter table when `table` is at least
    `max_age_days` old and the API is reachable; otherwise returns `table`
    unchanged. Network/parse failures are swallowed (the caller keeps the stale
    table) so a no-network run never breaks. `max_age_days=None` disables it.
    """
    if max_age_days is None:
        return table
    age = _table_age_days(table)
    if age is not None and age < max_age_days:
        return table
    try:
        return refresh_pricing(timeout=timeout)
    except BudgetError:
        return table


def describe_pricing(table: PricingTable, *, refreshed: bool = False) -> PricingProvenance:
    """Provenance for `table` (source, age, whether it was just refreshed)."""
    return PricingProvenance(
        source=table.source,
        updated_at=table.updated_at,
        age_days=_table_age_days(table),
        refreshed=refreshed,
    )


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


def anthropic_style_caching(model: "str | None") -> bool:
    """Whether `model` bills cache writes Anthropic-style (1.25x surcharge).

    Anthropic models — called natively or through OpenRouter — charge a write
    surcharge for explicit cache_control caching. Token-prefix providers
    (OpenAI, Gemini implicit, DeepSeek, Grok, ...) cache automatically with
    free writes. Unknown (None) models keep the conservative surcharge.
    """
    if model is None:
        return True
    segments = model.split("/")
    return "anthropic" in segments[:2]


def cache_write_default(price: ModelPrice, model: "str | None") -> float:
    if price.cache_write_usd_per_mtok is not None:
        return price.cache_write_usd_per_mtok
    return 1.25 * price.input_usd_per_mtok if anthropic_style_caching(model) else 0.0


def cost_usd(
    price: ModelPrice,
    input_tokens: "int | None",
    output_tokens: "int | None",
    cache_read: "int | None" = 0,
    cache_write: "int | None" = 0,
    model: "str | None" = None,
) -> float:
    crp = price.cache_read_usd_per_mtok
    if crp is None:
        crp = 0.1 * price.input_usd_per_mtok
    cwp = cache_write_default(price, model)
    return (
        (input_tokens or 0) * price.input_usd_per_mtok
        + (output_tokens or 0) * price.output_usd_per_mtok
        + (cache_read or 0) * crp
        + (cache_write or 0) * cwp
    ) / 1e6


def provider_of(model: str) -> str:
    return model.split("/")[0]


def batch_providers_used(models: list[str]) -> list[str]:
    """Providers among `models` whose batch API a batch run goes through."""
    return sorted({provider_of(m) for m in models} & BATCH_PROVIDERS)
