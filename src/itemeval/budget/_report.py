"""Post-run cost report: per-provider spend + savings vs plain-API list price.

Savings figures re-price the ledger's stored token counts at the *current*
pricing table, so the decomposition is internally exact:

    baseline   = every input token at the full input rate, no batch discount
    after_cache= cached tokens priced at their (discounted) cache rates
    actual     = after_cache, halved when the row was billed as a batch call

    cache_savings = baseline    - after_cache
    batch_savings = after_cache - actual
    total_savings = baseline    - actual   (== cache_savings + batch_savings)

Local-response-cache / resume reuse is deliberately NOT represented: a cache
hit carries no usage object, so its ledger row holds null tokens and contributes
zero to both `actual` and `baseline`. The reported savings therefore cover the
prompt-cache discount and the batch discount only.
"""

import pandas as pd
from pydantic import BaseModel, ConfigDict

from itemeval.budget._pricing import PricingTable, cost_usd, lookup_price, provider_of


class ProviderSpend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    calls: int
    usd: float  # actual (cache + batch applied)
    baseline_usd: float  # plain-API list price
    savings_usd: float


class CostReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_usd: float
    baseline_usd: float
    cache_savings_usd: float
    batch_savings_usd: float
    total_savings_usd: float
    savings_pct: float  # total_savings / baseline, 0 when baseline is 0
    by_provider: list[ProviderSpend]
    unpriced_models: list[str]  # priced=False rows; excluded from the figures


def _int(value) -> int:
    return 0 if value is None or pd.isna(value) else int(value)


def cost_report(ledger: "pd.DataFrame", pricing: PricingTable) -> CostReport:
    """Build a savings + per-provider spend report from a cost ledger.

    See the module docstring for the savings model and its scope.
    """
    actual = baseline = cache_sav = batch_sav = 0.0
    unpriced: set[str] = set()
    by_provider: dict[str, dict] = {}

    for row in ledger.itertuples():
        model = row.model
        price = lookup_price(pricing, model)
        if price is None:
            if _int(row.calls) > 0:
                unpriced.add(model)
            continue
        in_tok = _int(row.input_tokens)
        out_tok = _int(row.output_tokens)
        cache_read = _int(row.cache_read_tokens)
        cache_write = _int(row.cache_write_tokens)

        after_cache = cost_usd(price, in_tok, out_tok, cache_read, cache_write)
        base = cost_usd(price, in_tok + cache_read + cache_write, out_tok, 0, 0)
        act = after_cache * 0.5 if bool(row.batch) else after_cache

        actual += act
        baseline += base
        cache_sav += base - after_cache
        batch_sav += after_cache - act

        agg = by_provider.setdefault(
            provider_of(model), {"calls": 0, "usd": 0.0, "baseline_usd": 0.0}
        )
        agg["calls"] += _int(row.calls)
        agg["usd"] += act
        agg["baseline_usd"] += base

    total_sav = baseline - actual
    providers = [
        ProviderSpend(
            provider=name,
            calls=agg["calls"],
            usd=agg["usd"],
            baseline_usd=agg["baseline_usd"],
            savings_usd=agg["baseline_usd"] - agg["usd"],
        )
        for name, agg in sorted(by_provider.items())
    ]
    return CostReport(
        actual_usd=actual,
        baseline_usd=baseline,
        cache_savings_usd=cache_sav,
        batch_savings_usd=batch_sav,
        total_savings_usd=total_sav,
        savings_pct=(100.0 * total_sav / baseline) if baseline > 0 else 0.0,
        by_provider=providers,
        unpriced_models=sorted(unpriced),
    )
