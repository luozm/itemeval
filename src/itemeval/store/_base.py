"""Keyed parquet upserts with schema-enforced round trips."""

import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from itemeval._errors import StoreError

# Non-additive run-identity change (recovery-run-identity): an old store carries a
# `run_id` column the current schema replaced with `experiment_id` + `attempt`.
# We do not read old code; the safe, result-preserving migration is delete + re-run
# (content keys are unchanged, so cached generations replay identical at ~$0). The
# guard fails loudly with the briefing instead of crashing opaquely on the missing
# column (DEVELOPMENT.md "Study-facing schema evolution", pre-1.0 clean-break gate).
STUDY_MIGRATION_MSG = (
    "{name} predates the run-identity change: it has a `run_id` column but no "
    "`experiment_id`. itemeval now identifies runs by `experiment_id` + `attempt`. "
    "Delete manifests/, logs/, and the parquet stores (solutions, gradings, ledger, "
    "log_index, materialized_rubrics) under this study, then re-run — cached "
    "generations replay at ~$0 and the content keys are unchanged, so results are "
    "identical."
)


def assert_identity_current(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Raise the migration briefing if `df` is an old-schema store (has `run_id`,
    lacks `experiment_id`); otherwise pass it through. Read-time guard, mirroring
    the wave backfill's locus."""
    if not df.empty and "run_id" in df.columns and "experiment_id" not in df.columns:
        raise StoreError(STUDY_MIGRATION_MSG.format(name=path.name))
    return df


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
    path: Path,
    rows: "list[dict] | pd.DataFrame",
    key: "list[str]",
    schema: pa.Schema,
    *,
    recency_col: "str | None" = None,
    error_col: "str | None" = None,
    content_col: "str | None" = None,
) -> int:
    """Concat new rows over existing, dedup on key keeping last, write atomically.

    With `recency_col` set (e.g. ``"attempt"``), the surviving row per key is the
    one with the **highest** recency value, not merely the last written. This is
    what keeps a re-applied *older* attempt from clobbering a newer one: the
    recoverable-harvest re-reads crashed `.eval` logs and re-upserts their rows
    as "new", so without a recency guard a stale attempt-N error row would
    overwrite an attempt-(N+1) recovered solution for the same content key (the
    key never includes `attempt`). A stable sort on `recency_col` lands the
    highest attempt last so `keep="last"` preserves it.

    `error_col` and `content_col` together impose a **quality** order that
    outranks recency, so the surviving row per key is the best-quality one rather
    than merely the newest: **valid (no error, non-blank content) > empty (no
    error, blank content) > error (non-null `error_col`)**, ties broken by
    `recency_col`. Recency alone is insufficient when one key has rows of
    different quality at the *same* attempt (two `.eval` logs of one attempt — one
    that produced the solution, one that timed out / 404'd / came back blank), or
    when a *later* retry errors or blanks after an earlier attempt succeeded (a
    single-provider backend flipping valid↔404 across retries): plain recency lets
    the worse row win and the harvest flips the cell's validity by re-application
    order. Ranking quality first makes the surviving state deterministic and
    monotonic — a recovered solution is never erased by a later infra failure or
    blank."""
    df_new = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if df_new.empty:
        return 0
    existing = read_parquet_or_empty(path, schema)
    df = df_new if existing.empty else pd.concat([existing, df_new], ignore_index=True)
    # Sort so the row to KEEP per key lands last (drop_duplicates keep="last"):
    # no-error outranks error, then non-blank content outranks blank, then highest
    # recency. sort_values is ascending, so False(worse) precedes True(better).
    sort_cols: list[str] = []
    tmp_cols: list[str] = []
    if error_col is not None and error_col in df.columns:
        df = df.assign(_ie_ok=df[error_col].isna())
        sort_cols.append("_ie_ok")
        tmp_cols.append("_ie_ok")
    if content_col is not None and content_col in df.columns:
        df = df.assign(
            _ie_content=df[content_col].notna()
            & (df[content_col].fillna("").astype(str).str.strip() != "")
        )
        sort_cols.append("_ie_content")
        tmp_cols.append("_ie_content")
    if recency_col is not None and recency_col in df.columns:
        sort_cols.append(recency_col)
    if sort_cols:
        df = df.sort_values(sort_cols, kind="mergesort")
    df = df.drop_duplicates(subset=key, keep="last")
    if tmp_cols:
        df = df.drop(columns=tmp_cols)
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
