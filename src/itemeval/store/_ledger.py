"""Cost ledger: one row per (run x stage x condition x model)."""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

LEDGER_KEY = ["run_id", "stage", "condition_id", "model"]

LEDGER_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("stage", pa.string(), nullable=False),
        pa.field("condition_id", pa.string(), nullable=False),
        pa.field("model", pa.string(), nullable=False),
        pa.field("provider", pa.string()),  # inspect provider prefix; which dashboard billed
        pa.field("calls", pa.int64(), nullable=False),
        pa.field("input_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
        pa.field("total_tokens", pa.int64()),
        pa.field("cache_read_tokens", pa.int64()),
        pa.field("cache_write_tokens", pa.int64()),
        pa.field("usd", pa.float64()),
        pa.field("priced", pa.bool_(), nullable=False),
        pa.field("batch", pa.bool_(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
    ]
)


def upsert_ledger(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.ledger, rows, LEDGER_KEY, LEDGER_SCHEMA)


def read_ledger(paths: StudyPaths) -> pd.DataFrame:
    return read_parquet_or_empty(paths.ledger, LEDGER_SCHEMA)
