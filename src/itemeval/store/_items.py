"""Canonical items snapshot store."""

import pandas as pd
import pyarrow as pa

from itemeval._util import canonical_json, utc_now_iso
from itemeval.adapters._base import LoadedDataset
from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

ITEM_KEY = ["item_id", "dataset_id"]

ITEMS_SCHEMA = pa.schema(
    [
        pa.field("item_id", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("dataset_revision", pa.string(), nullable=False),
        pa.field("input", pa.string(), nullable=False),
        pa.field("target", pa.string(), nullable=False),
        pa.field("grading_scheme", pa.string()),
        pa.field("metadata_json", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
    ]
)


def upsert_items(paths: StudyPaths, datasets: "list[LoadedDataset]") -> int:
    now = utc_now_iso()
    rows = [
        {
            "item_id": item.id,
            "dataset_id": ds.dataset_id,
            "dataset_revision": ds.revision,
            "input": item.input,
            "target": item.target,
            "grading_scheme": item.grading_scheme,
            "metadata_json": canonical_json(item.metadata),
            "created_at": now,
        }
        for ds in datasets
        for item in ds.items
    ]
    return upsert_parquet(paths.items, rows, ITEM_KEY, ITEMS_SCHEMA)


def read_items(paths: StudyPaths) -> pd.DataFrame:
    return read_parquet_or_empty(paths.items, ITEMS_SCHEMA)
