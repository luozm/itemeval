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

    parquet = pd.read_parquet(prep.paths.export_dir / "gradings_long.parquet")
    assert list(parquet.columns) == list(EXPORT_SCHEMA.names)
    assert len(parquet.columns) == 45
    assert len(parquet) == 8

    # One row per grading event, never aggregated; full provenance joined in.
    assert parquet["score"].notna().all()
    assert parquet["solution"].notna().all()
    assert parquet["model"].nunique() == 2
    assert (parquet["replication"] == parquet["replication"].astype(int)).all()
    assert parquet["gen_usd"].notna().all() and parquet["grade_usd"].notna().all()
    assert parquet["gen_log_file"].notna().all()

    csv = pd.read_csv(prep.paths.export_dir / "gradings_long.csv")
    assert list(csv.columns) == list(parquet.columns)
    assert len(csv) == len(parquet)

    ledger_csv = pd.read_csv(prep.paths.export_dir / "ledger.csv")
    assert set(ledger_csv["stage"]) == {"generate", "grade"}


def test_export_detects_ledger_mismatch(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    # Corrupt the ledger with an extra phantom row.
    upsert_ledger(
        prep.paths,
        [
            {
                "run_id": "phantom",
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
