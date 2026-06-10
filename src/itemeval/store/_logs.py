"""Raw .eval log index."""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

LOG_INDEX_KEY = ["log_file"]

LOG_INDEX_SCHEMA = pa.schema(
    [
        pa.field("log_file", pa.string(), nullable=False),
        pa.field("run_id", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("condition_id", pa.string()),
        pa.field("task_name", pa.string()),
        pa.field("model", pa.string()),
        pa.field("status", pa.string()),
        pa.field("started_at", pa.string()),
        pa.field("completed_at", pa.string()),
        pa.field("total_samples", pa.int64()),
        pa.field("completed_samples", pa.int64()),
        pa.field("input_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
        pa.field("total_tokens", pa.int64()),
        pa.field("usd", pa.float64()),
        pa.field("created_at", pa.string()),
    ]
)


def upsert_log_index(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.log_index, rows, LOG_INDEX_KEY, LOG_INDEX_SCHEMA)


def read_log_index(paths: StudyPaths) -> pd.DataFrame:
    return read_parquet_or_empty(paths.log_index, LOG_INDEX_SCHEMA)
