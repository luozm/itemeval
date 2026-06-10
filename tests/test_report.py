import pandas as pd
import pytest

from itemeval.budget._pricing import ModelPrice, PricingTable
from itemeval.budget._report import cost_report

PRICING = PricingTable(
    updated_at="t",
    source="file",
    models={"openai/gpt-5": ModelPrice(input_usd_per_mtok=10.0, output_usd_per_mtok=20.0)},
)


def _row(**over) -> dict:
    base = {
        "run_id": "r",
        "stage": "generate",
        "condition_id": "c",
        "model": "openai/gpt-5",
        "calls": 1,
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "total_tokens": 2_000_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "usd": 0.0,
        "priced": True,
        "batch": False,
        "created_at": "t",
    }
    base.update(over)
    return base


def test_cache_savings_only():
    # 1M input + 1M cache_read + 1M output, no batch.
    rep = cost_report(pd.DataFrame([_row(cache_read_tokens=1_000_000)]), PRICING)
    # after_cache = 1M*10 + 1M*0.1*10 (cache read) + 1M*20 = 10 + 1 + 20 = 31
    # baseline    = (1M+1M)*10 + 1M*20 = 20 + 20 = 40  (cache read billed at full input)
    assert rep.actual_usd == pytest.approx(31.0)
    assert rep.baseline_usd == pytest.approx(40.0)
    assert rep.cache_savings_usd == pytest.approx(9.0)
    assert rep.batch_savings_usd == pytest.approx(0.0)
    assert rep.total_savings_usd == pytest.approx(9.0)
    assert rep.savings_pct == pytest.approx(22.5)
    assert [p.provider for p in rep.by_provider] == ["openai"]
    assert rep.by_provider[0].savings_usd == pytest.approx(9.0)


def test_batch_savings_compose_with_cache():
    rep = cost_report(pd.DataFrame([_row(cache_read_tokens=1_000_000, batch=True)]), PRICING)
    # after_cache = 31 (as above); batch halves it -> actual 15.5
    assert rep.actual_usd == pytest.approx(15.5)
    assert rep.baseline_usd == pytest.approx(40.0)
    assert rep.cache_savings_usd == pytest.approx(9.0)
    assert rep.batch_savings_usd == pytest.approx(15.5)
    # additive: cache + batch == total
    assert rep.total_savings_usd == pytest.approx(24.5)


def test_unpriced_excluded_and_listed():
    rep = cost_report(pd.DataFrame([_row(model="mystery/model", priced=False)]), PRICING)
    assert rep.unpriced_models == ["mystery/model"]
    assert rep.actual_usd == 0.0 and rep.baseline_usd == 0.0
    assert rep.by_provider == []


def test_cache_hit_row_with_null_tokens_contributes_zero():
    # Local response-cache hit: priced, but no usage recorded -> null tokens.
    rep = cost_report(
        pd.DataFrame([_row(input_tokens=None, output_tokens=None, total_tokens=None)]),
        PRICING,
    )
    assert rep.total_savings_usd == 0.0
    assert rep.actual_usd == 0.0


def test_empty_ledger():
    rep = cost_report(pd.DataFrame(columns=_row().keys()), PRICING)
    assert rep.total_savings_usd == 0.0
    assert rep.savings_pct == 0.0
    assert rep.by_provider == []
