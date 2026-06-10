"""Keyed parquet upserts with schema-enforced round trips."""

import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from itemeval._errors import StoreError


def read_parquet_or_empty(path: Path, schema: pa.Schema) -> pd.DataFrame:
    if path.is_file():
        return pd.read_parquet(path)
    return pd.DataFrame({name: pd.Series(dtype="object") for name in schema.names})


def _coerce_to_schema(df: pd.DataFrame, schema: pa.Schema) -> pd.DataFrame:
    """Pandas-side dtype normalization so pyarrow casts cleanly (NaN -> null ints)."""
    df = df.copy()
    for field in schema:
        col = field.name
        if col not in df.columns:
            df[col] = None
        if pa.types.is_integer(field.type):
            df[col] = pd.to_numeric(df[col], errors="raise").astype("Int64")
        elif pa.types.is_floating(field.type):
            df[col] = pd.to_numeric(df[col], errors="raise").astype("Float64")
        elif pa.types.is_boolean(field.type):
            df[col] = df[col].astype("boolean")
    return df[list(schema.names)]


def upsert_parquet(
    path: Path, rows: "list[dict] | pd.DataFrame", key: "list[str]", schema: pa.Schema
) -> int:
    """Concat new rows over existing, dedup on key keeping last, write atomically."""
    df_new = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if df_new.empty:
        return 0
    existing = read_parquet_or_empty(path, schema)
    df = df_new if existing.empty else pd.concat([existing, df_new], ignore_index=True)
    df = df.drop_duplicates(subset=key, keep="last")
    df = df.sort_values(key, kind="mergesort", ignore_index=True)
    try:
        df = _coerce_to_schema(df, schema)
        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError) as e:
        raise StoreError(f"schema cast failed writing {path.name}: {e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
    return len(df_new)


def rel_to_study(paths, p: "str | Path") -> str:
    return os.path.relpath(str(p), str(paths.study_dir))
