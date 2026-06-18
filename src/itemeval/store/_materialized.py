"""Per-item materialized-rubric artifact store (content-addressed, frozen).

A materializing rubric (`rubrics:` with `materialize:`) produces one rubric per
item from the item's reference, frozen here and reused verbatim by every grader
call for that item — across graders, solutions, replications, and resumed runs.
Keyed by `(materialize_id, item_id)` where `materialize_id` hashes the build
template + materializer model, so a changed build template or model re-derives.
"""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

MATERIALIZED_KEY = ["materialize_id", "item_id"]

MATERIALIZED_SCHEMA = pa.schema(
    [
        pa.field("materialize_id", pa.string(), nullable=False),
        pa.field("rubric_name", pa.string(), nullable=False),
        pa.field("item_id", pa.string(), nullable=False),
        pa.field("materializer_model", pa.string(), nullable=False),
        pa.field("build_template_hash", pa.string(), nullable=False),
        pa.field("rubric_text", pa.string()),  # null/empty when the materializer produced none
        pa.field("rubric_hash", pa.string()),
        pa.field("usd", pa.float64()),
        pa.field("input_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
        pa.field("error", pa.string()),  # non-null = the call failed; the row stays pending
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
    ]
)


def read_materialized(paths: StudyPaths) -> pd.DataFrame:
    return read_parquet_or_empty(paths.materialized_rubrics, MATERIALIZED_SCHEMA)


def upsert_materialized(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.materialized_rubrics, rows, MATERIALIZED_KEY, MATERIALIZED_SCHEMA)


def stored_texts(existing: pd.DataFrame, materialize_id: str) -> "dict[str, str]":
    """{item_id -> frozen rubric text} for successfully-materialized rows (no
    error) under this materialize_id. A null/NaN rubric_text reads as "" (the
    materializer ran but produced no text — counted as empty, still reused)."""
    if existing.empty:
        return {}
    sub = existing[(existing["materialize_id"] == materialize_id) & existing["error"].isna()]
    out: "dict[str, str]" = {}
    for r in sub.itertuples():
        txt = r.rubric_text
        out[str(r.item_id)] = "" if txt is None or txt != txt else str(txt)
    return out
