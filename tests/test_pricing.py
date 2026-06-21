import io
import json
import urllib.request

import pytest

from itemeval._errors import BudgetError
from itemeval._util import utc_now_iso
from itemeval.budget._pricing import (
    ModelPrice,
    PricingTable,
    cost_usd,
    describe_pricing,
    is_schema_stale,
    load_pricing,
    lookup_price,
    maybe_refresh_pricing,
    provider_of,
    refresh_pricing,
    seed_pricing,
)


def _table(updated_at: str) -> PricingTable:
    return PricingTable(updated_at=updated_at, source="seed", models={})


def test_seed_loads_and_prices_mockllm():
    table = seed_pricing()
    assert table.source == "seed"
    price = lookup_price(table, "mockllm/anything")
    assert price is not None and price.input_usd_per_mtok == 3.0


def test_lookup_precedence():
    table = seed_pricing()
    assert lookup_price(table, "openai/gpt-5-mini").input_usd_per_mtok == 0.25
    # openrouter cross-keys, both directions
    assert lookup_price(table, "deepseek/deepseek-v3.2") is not None
    assert lookup_price(table, "openrouter/deepseek/deepseek-v3.2") is not None
    assert lookup_price(table, "unknown/model") is None


def _price(inp=1.0, out=2.0) -> ModelPrice:
    return ModelPrice(input_usd_per_mtok=inp, output_usd_per_mtok=out)


def test_native_id_prices_off_openrouter_dotted_slug():
    """A natively-called Anthropic id (hyphenated version) prices off the
    OpenRouter slug (dotted version) the refresh seeds — the convention gap."""
    table = PricingTable(
        updated_at=utc_now_iso(),
        source="file",
        models={"anthropic/claude-opus-4.8": _price(15.0, 75.0)},
    )
    # native hyphenated id (what the Anthropic API requires) resolves
    hit = lookup_price(table, "anthropic/claude-opus-4-8")
    assert hit is not None and hit.output_usd_per_mtok == 75.0
    # and the reverse: a dotted id resolves off a hyphenated table key
    table2 = PricingTable(
        updated_at=utc_now_iso(),
        source="file",
        models={"anthropic/claude-haiku-4-5": _price(1.0, 5.0)},
    )
    assert lookup_price(table2, "anthropic/claude-haiku-4.5").output_usd_per_mtok == 5.0
    # multi-segment version (claude-3-5-sonnet <-> claude-3.5-sonnet)
    table3 = PricingTable(
        updated_at=utc_now_iso(),
        source="file",
        models={"anthropic/claude-3.5-sonnet": _price()},
    )
    assert lookup_price(table3, "anthropic/claude-3-5-sonnet") is not None


def test_version_variants_leave_non_version_separators_alone():
    from itemeval.budget._pricing import _id_variants

    # only digit<sep>digit toggles; letter-boundary separators are untouched
    assert set(_id_variants("anthropic/claude-opus-4-8")) == {
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4.8",
    }
    assert _id_variants("anthropic/claude-3-haiku") == ["anthropic/claude-3-haiku"]
    assert _id_variants("openai/gpt-4o") == ["openai/gpt-4o"]


def test_normalization_does_not_misprice_absent_models():
    """A toggled variant that matches no key must not invent a price."""
    table = PricingTable(
        updated_at=utc_now_iso(),
        source="file",
        models={"anthropic/claude-opus-4.8": _price()},
    )
    # a different, genuinely-absent version returns None (no false match)
    assert lookup_price(table, "anthropic/claude-opus-4-9") is None
    assert lookup_price(table, "anthropic/claude-sonnet-4-5") is None


def test_native_price_resolution_matrix():
    """Documents the guarantee scope: native ids whose model token matches the
    OpenRouter slug (after version-separator normalization) price; a differing
    provider namespace does not, and stays None for the unpriced-models hint."""
    table = PricingTable(
        updated_at=utc_now_iso(),
        source="file",
        models={
            "openai/gpt-5.5": _price(),
            "google/gemini-3.1-pro-preview": _price(),
            "anthropic/claude-opus-4.8": _price(),  # dotted slug as seeded
            "x-ai/grok-4.20": _price(),  # OpenRouter's provider name
        },
    )
    # OpenAI / Google: native id == OpenRouter model token → priced as-is
    assert lookup_price(table, "openai/gpt-5.5") is not None
    assert lookup_price(table, "google/gemini-3.1-pro-preview") is not None
    # Anthropic: native hyphenated id bridges to the dotted slug → priced
    assert lookup_price(table, "anthropic/claude-opus-4-8") is not None
    # xAI: native provider namespace ("xai"/"grok") differs from OpenRouter's
    # "x-ai" — NOT bridged by version normalization, surfaces as unpriced
    assert lookup_price(table, "xai/grok-4.20") is None
    assert lookup_price(table, "grok/grok-4.20") is None


def test_cost_usd_with_cache_fallbacks():
    price = ModelPrice(input_usd_per_mtok=10.0, output_usd_per_mtok=20.0)
    # cache read defaults to 0.1x input, write to 1.25x input
    usd = cost_usd(price, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert usd == pytest.approx(10.0 + 20.0 + 1.0 + 12.5)
    assert cost_usd(price, None, None) == 0.0


def test_provider_of():
    assert provider_of("openai/gpt-5") == "openai"
    assert provider_of("openrouter/deepseek/x") == "openrouter"


def test_load_pricing_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "p.json"
    explicit.write_text(
        json.dumps(
            {
                "updated_at": "t",
                "source": "file",
                "models": {"m/x": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}},
            }
        )
    )
    table = load_pricing("p.json", tmp_path)
    assert table.source == "file"
    with pytest.raises(BudgetError, match="not found"):
        load_pricing("missing.json", tmp_path)
    assert load_pricing(None, tmp_path).source == "seed"


def test_refresh_pricing_merges_openrouter(monkeypatch, tmp_path):
    monkeypatch.setenv("ITEMEVAL_PRICING_PATH", str(tmp_path / "user.json"))
    payload = {
        "data": [
            {
                "id": "deepseek/deepseek-v3.2",
                "pricing": {"prompt": "0.0000005", "completion": "0.0000010"},
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": ["max_tokens", "temperature", "reasoning"],
                "context_length": 131072,
                "created": 1727308800,  # OpenRouter release timestamp (Unix seconds)
            },
            {
                "id": "openai/gpt-image",  # emits text AND image -> passes text_model gate
                "pricing": {"prompt": "0.0000005", "completion": "0.0000010"},
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["image", "text"],
                },
                "supported_parameters": ["max_tokens", "temperature"],
            },
            {
                "id": "meta/router",  # no generation params -> not a runnable text model
                "pricing": {"prompt": "0.0000005", "completion": "0.0000010"},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "supported_parameters": [],
            },
            {"id": "broken/entry", "pricing": {}},
        ]
    }

    def fake_urlopen(url, timeout):
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    table = refresh_pricing()
    assert table.source == "merged"
    price = table.models["openrouter/deepseek/deepseek-v3.2"]
    assert price.input_usd_per_mtok == pytest.approx(0.5)
    assert price.text_model is True  # text in/out + generation params
    assert price.reasoning is True and price.multimodal is True  # has reasoning + image input
    assert price.context_length == 131072
    assert price.created == 1727308800  # release timestamp captured for recency
    assert price.output_modalities == ["text"]  # emitted modalities persisted
    gen = table.models["openrouter/openai/gpt-image"]
    assert gen.text_model is True  # text in output -> passes the gate
    assert gen.output_modalities == ["image", "text"]  # but emits non-text too
    assert table.models["openrouter/meta/router"].created is None  # absent -> None
    assert table.models["openrouter/meta/router"].text_model is False  # empty params
    assert (tmp_path / "user.json").is_file()
    # The persisted user table is now picked up by load_pricing.
    assert load_pricing(None, tmp_path).source == "merged"


def test_refresh_pricing_network_failure(monkeypatch):
    def boom(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(BudgetError, match="refresh failed"):
        refresh_pricing()


def test_maybe_refresh_disabled_and_fresh(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("refresh must not be attempted")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    fresh = _table(utc_now_iso())
    # max_age_days=None disables auto-refresh; a fresh table is left untouched.
    assert maybe_refresh_pricing(fresh, None) is fresh
    assert maybe_refresh_pricing(fresh, 7) is fresh


def test_maybe_refresh_stale_refreshes(monkeypatch, tmp_path):
    monkeypatch.setenv("ITEMEVAL_PRICING_PATH", str(tmp_path / "user.json"))
    payload = {
        "data": [{"id": "x/y", "pricing": {"prompt": "0.0000005", "completion": "0.000001"}}]
    }
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout: io.BytesIO(json.dumps(payload).encode())
    )
    refreshed = maybe_refresh_pricing(_table("2000-01-01T00:00:00Z"), 7)
    assert refreshed.source == "merged"


def test_maybe_refresh_stale_offline_keeps_table(monkeypatch):
    def boom(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    stale = _table("2000-01-01T00:00:00Z")
    # Best-effort: a failed refresh falls back to the stale table, never raises.
    assert maybe_refresh_pricing(stale, 7) is stale


def test_is_schema_stale():
    # No entry carries roster metadata -> stale, regardless of a recent stamp.
    stale = PricingTable(
        updated_at=utc_now_iso(),
        source="merged",
        models={"openrouter/x/y": ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)},
    )
    assert is_schema_stale(stale)
    assert is_schema_stale(_table(utc_now_iso()))  # empty table has no metadata either
    # One entry carrying text_model is enough (a merged table flags its roster).
    fresh = PricingTable(
        updated_at=utc_now_iso(),
        source="merged",
        models={
            "openrouter/x/y": ModelPrice(
                input_usd_per_mtok=1.0, output_usd_per_mtok=2.0, text_model=True
            )
        },
    )
    assert not is_schema_stale(fresh)


def test_describe_pricing():
    fresh = describe_pricing(_table(utc_now_iso()), refreshed=True)
    assert fresh.source == "seed" and fresh.refreshed is True
    assert fresh.age_days is not None and fresh.age_days < 1
    # Unparseable timestamp -> age is None, not a crash.
    unknown = describe_pricing(_table("t"))
    assert unknown.age_days is None and unknown.refreshed is False
