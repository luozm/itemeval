"""Solutions store: one row per (generate condition x item x epoch)."""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

SOLUTION_KEY = ["condition_id", "item_id", "epoch"]

SOLUTIONS_SCHEMA = pa.schema(
    [
        pa.field("study", pa.string(), nullable=False),
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("condition_id", pa.string(), nullable=False),
        pa.field("condition_slug", pa.string(), nullable=False),
        pa.field("item_id", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("dataset_revision", pa.string(), nullable=False),
        pa.field("epoch", pa.int32(), nullable=False),
        pa.field("model", pa.string(), nullable=False),
        pa.field("prompt_name", pa.string(), nullable=False),
        pa.field("prompt_hash", pa.string(), nullable=False),
        pa.field("model_config_name", pa.string(), nullable=False),
        pa.field("temperature_requested", pa.float64()),
        pa.field("temperature_effective", pa.float64()),
        pa.field("top_p_requested", pa.float64()),
        pa.field("top_p_effective", pa.float64()),
        pa.field("max_tokens_requested", pa.int64()),
        pa.field("max_tokens_effective", pa.int64()),
        pa.field("seed_requested", pa.int64()),
        pa.field("reasoning_effort", pa.string()),
        pa.field("reasoning_effort_effective", pa.string()),
        pa.field("reasoning_tokens_requested", pa.int64()),
        pa.field("solution", pa.string()),
        pa.field("stop_reason", pa.string()),
        pa.field("error", pa.string()),
        pa.field("input_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
        pa.field("total_tokens", pa.int64()),
        pa.field("cache_read_tokens", pa.int64()),
        pa.field("cache_write_tokens", pa.int64()),
        pa.field("reasoning_tokens", pa.int64()),
        pa.field("usd", pa.float64()),
        pa.field("latency_s", pa.float64()),
        pa.field("log_file", pa.string(), nullable=False),
        pa.field("sample_uuid", pa.string()),
        pa.field("created_at", pa.string(), nullable=False),
    ]
)


def read_solutions(paths: StudyPaths) -> pd.DataFrame:
    return read_parquet_or_empty(paths.solutions, SOLUTIONS_SCHEMA)


def upsert_solutions(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.solutions, rows, SOLUTION_KEY, SOLUTIONS_SCHEMA)


def items_to_run(
    df: pd.DataFrame, condition_id: str, item_ids: "list[str]", replications: int
) -> "list[str]":
    """Items (input order preserved) missing any completed epoch 1..replications."""
    if df.empty:
        return list(item_ids)
    cond = df[(df["condition_id"] == condition_id) & (df["error"].isna())]
    done_epochs = cond.groupby("item_id")["epoch"].apply(set).to_dict()
    needed = set(range(1, replications + 1))
    return [iid for iid in item_ids if not needed.issubset(done_epochs.get(iid, set()))]
