import pandas as pd
import pytest

from itemeval._errors import StoreError
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade
from itemeval.store._export import EXPORT_SCHEMA, export_study
from itemeval.store._ledger import LEDGER_SCHEMA, upsert_ledger


def test_export_requires_gradings(study):
    cfg, _ = study
    with pytest.raises(StoreError, match="nothing to export"):
        export_study(cfg)


def test_export_schema_and_mirrors(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    result = export_study(cfg)

    assert result.rows == 8
    assert result.internally_reconciled
    assert result.generation_usd > 0 and result.grading_usd > 0

    # Cost report: recomputed actual matches the stored ledger spend; mockllm has
    # no batch/cache discount so there are no savings, but the provider is tracked.
    cost = result.cost
    assert cost.actual_usd == pytest.approx(result.generation_usd + result.grading_usd)
    assert cost.total_savings_usd == pytest.approx(0.0)
    assert {p.provider for p in cost.by_provider} == {"mockllm"}
    assert sum(p.calls for p in cost.by_provider) > 0
    # Pricing provenance accompanies the report (export never auto-refreshes).
    assert result.pricing.source == "seed" and result.pricing.refreshed is False

    parquet = pd.read_parquet(prep.paths.export_dir / "gradings_long.parquet")
    assert list(parquet.columns) == list(EXPORT_SCHEMA.names)
    assert (
        len(parquet.columns) == 54
    )  # 50 + served_provider/native_finish_reason per stage (provider-finish-capture)
    assert len(parquet) == 8

    # provider-finish-capture: the four raw-provenance columns exist; a mock run
    # has no serving provider / native finish_reason, so all are null.
    for c in (
        "gen_served_provider",
        "gen_native_finish_reason",
        "grade_served_provider",
        "grade_native_finish_reason",
    ):
        assert c in parquet.columns and parquet[c].isna().all()

    # One row per grading event, never aggregated; full provenance joined in.
    assert parquet["score"].notna().all()
    assert parquet["solution"].notna().all()
    # truncation-signal: a clean mock run truncates nothing (all stop_reason=stop).
    assert str(parquet["truncated"].dtype) in ("bool", "boolean")
    assert not parquet["truncated"].any()
    assert parquet["model"].nunique() == 2
    assert (parquet["replication"] == parquet["replication"].astype(int)).all()
    assert parquet["gen_usd"].notna().all() and parquet["grade_usd"].notna().all()
    assert parquet["gen_log_file"].notna().all()

    csv = pd.read_csv(prep.paths.export_dir / "gradings_long.csv")
    assert list(csv.columns) == list(parquet.columns)
    assert len(csv) == len(parquet)

    ledger_csv = pd.read_csv(prep.paths.export_dir / "ledger.csv")
    assert set(ledger_csv["stage"]) == {"generate", "grade"}


def test_export_carries_served_provider_and_finish(study):
    """provider-finish-capture: a served_provider / native_finish_reason value
    stored on a solution (judge) row reaches the export's gen_* (grade_*) column."""
    from itemeval.store._gradings import read_gradings, upsert_gradings
    from itemeval.store._solutions import read_solutions, upsert_solutions

    cfg, prep = study
    run_generate(prep)
    run_grade(prep)

    sol = read_solutions(prep.paths).iloc[0].to_dict()
    sol["served_provider"], sol["native_finish_reason"] = "Fireworks", "stop"
    upsert_solutions(prep.paths, [sol])
    grad = read_gradings(prep.paths).iloc[0].to_dict()
    grad["served_provider"], grad["native_finish_reason"] = "Anthropic", "stop"
    upsert_gradings(prep.paths, [grad])

    export_study(cfg)
    parquet = pd.read_parquet(prep.paths.export_dir / "gradings_long.parquet")
    assert (parquet["gen_served_provider"] == "Fireworks").any()
    assert (parquet["gen_native_finish_reason"] == "stop").any()
    assert (parquet["grade_served_provider"] == "Anthropic").any()


def test_export_detects_ledger_mismatch(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    # Corrupt the ledger with an extra phantom row.
    upsert_ledger(
        prep.paths,
        [
            {
                "experiment_id": "phantom",
                "attempt": 1,
                "stage": "generate",
                "condition_id": "x",
                "model": "m",
                "calls": 1,
                "usd": 99.0,
                "priced": True,
                "batch": False,
                "created_at": "t",
            }
        ],
    )
    assert LEDGER_SCHEMA is not None
    result = export_study(cfg)
    assert not result.internally_reconciled
