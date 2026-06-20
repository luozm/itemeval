"""Solutions store: one row per (generate condition x item x epoch)."""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import assert_identity_current, read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths

SOLUTION_KEY = ["condition_id", "item_id", "epoch"]

SOLUTIONS_SCHEMA = pa.schema(
    [
        pa.field("study", pa.string(), nullable=False),
        # Run identity (recovery-run-identity): experiment_id groups attempts of one
        # experiment, attempt distinguishes recovery passes. Not part of any content
        # key — a recovered cell overwrites the failed row at the same (cond,item,epoch).
        pa.field("experiment_id", pa.string(), nullable=False),
        pa.field("attempt", pa.int32(), nullable=False),
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
        # Raw call provenance (provider-finish-capture): the OpenRouter backend
        # that actually served this call, and its raw finish_reason *before*
        # inspect flattens it into stop_reason ('error' and unmapped reasons
        # collapse to 'unknown'). Null for mock models, cache replays, and
        # providers that don't return the fields.
        pa.field("served_provider", pa.string()),
        pa.field("native_finish_reason", pa.string()),
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
        # Wave provenance (re-observation over time): wave w with replications R
        # occupies epochs w*R+1 .. (w+1)*R. Derived columns, default 0/null —
        # studies that never use --wave see one constant column and nothing else.
        pa.field("wave", pa.int32()),  # nullable: null reads as wave 0
        pa.field("wave_label", pa.string()),
    ]
)


def _backfill_wave(df: pd.DataFrame) -> pd.DataFrame:
    """Old stores predate the wave columns; default them on read (no rewrite)."""
    if "wave" not in df.columns:
        df = df.assign(wave=0, wave_label=None)
    elif not df.empty and df["wave"].isna().any():
        df = df.assign(wave=df["wave"].fillna(0))
    return df


# Raw-call-provenance columns (provider-finish-capture) added after some stores
# were written. Both the solutions and gradings stores carry them, so the
# read-time backfill is shared (gradings imports this). Mirrors _backfill_wave's
# locus: default them to None on read so an older store still loads — the
# additive-by-construction invariant (DEVELOPMENT.md "Study-facing schema
# evolution"), never a raw comparison of a growing schema.
PROVENANCE_COLS = ("served_provider", "native_finish_reason")


def _backfill_provenance(df: pd.DataFrame) -> pd.DataFrame:
    """Old stores predate the served_provider / native_finish_reason columns;
    default them on read (no rewrite)."""
    missing = {c: None for c in PROVENANCE_COLS if c not in df.columns}
    return df.assign(**missing) if missing else df


def read_solutions(paths: StudyPaths) -> pd.DataFrame:
    df = assert_identity_current(
        read_parquet_or_empty(paths.solutions, SOLUTIONS_SCHEMA), paths.solutions
    )
    return _backfill_provenance(_backfill_wave(df))


def upsert_solutions(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.solutions, rows, SOLUTION_KEY, SOLUTIONS_SCHEMA)


def empty_solution_mask(df: pd.DataFrame) -> pd.Series:
    """No-error rows whose completion is null/blank (e.g. reasoning truncation).

    These completed without an API error but produced no gradable text. They are
    a distinct channel from error rows (re-attempted) and parse failures (final).
    """
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    blank = df["solution"].isna() | (df["solution"].fillna("").astype(str).str.strip() == "")
    return df["error"].isna() & blank


def oversized_solution_mask(df: pd.DataFrame, max_chars: int) -> pd.Series:
    """No-error, non-empty rows whose visible solution text exceeds `max_chars`.

    These are over-long ("degenerate"/loop) outputs — a weak model repeating
    itself for 100k+ chars is not a valid proof, so paying a judge to grade it
    is waste. The grade flow excludes them from the judge batch and scores them 0
    (the `max_solution_chars` grader policy). Disjoint from `empty_solution_mask`
    by construction (a blank completion has length 0), so empty handling applies
    first and a solution is never counted as both.
    """
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    lengths = df["solution"].fillna("").astype(str).str.len()
    return df["error"].isna() & (lengths > max_chars) & ~empty_solution_mask(df)


# inspect StopReason values that mean "output cut for length" (truncation-signal):
# the requested max_tokens budget, or the model's own context limit. content_filter
# is a refusal (not a length cut) and "unknown" conflates unmapped reasons (A2) —
# neither is truncation. Re-verify against inspect's StopReason literal on a bump.
TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "model_length"})


def truncated_mask(df: pd.DataFrame) -> pd.Series:
    """No-error rows that stopped on a length cap with a NON-empty completion —
    a truncated-but-gradable answer that today counts as `completed` and is scored
    as finished, so a budget cut reads as a content failure (truncation-signal).

    The disjoint complement of `empty_solution_mask`: an *empty* length-cap stop is
    already the empty/`incomplete` channel, so it is excluded here. This is an
    informational sub-count of completed rows — it never reclassifies them.
    """
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    is_length_cap = df["stop_reason"].isin(TRUNCATION_STOP_REASONS)
    return df["error"].isna() & is_length_cap & ~empty_solution_mask(df)


def epochs_to_run(
    df: pd.DataFrame,
    condition_id: str,
    item_ids: "list[str]",
    epoch_range: "tuple[int, int]",
    *,
    require_solution: bool = False,
) -> "dict[str, set[int]]":
    """Per item: epochs in epoch_range (inclusive) not yet completed.

    `require_solution=True` (the `rerun` empty-solution policy) counts only
    non-empty completions as done, so empty no-error rows are re-attempted.
    """
    lo, hi = epoch_range
    needed = set(range(lo, hi + 1))
    if df.empty:
        return {iid: set(needed) for iid in item_ids}
    cond = df[(df["condition_id"] == condition_id) & (df["error"].isna())]
    if require_solution and not cond.empty:
        cond = cond[~empty_solution_mask(cond)]
    done = cond.groupby("item_id")["epoch"].apply(lambda s: set(s.astype(int))).to_dict()
    return {iid: needed - done.get(iid, set()) for iid in item_ids}


def items_to_run(
    df: pd.DataFrame,
    condition_id: str,
    item_ids: "list[str]",
    replications: int,
    *,
    require_solution: bool = False,
) -> "list[str]":
    """Items (input order preserved) missing any completed epoch 1..replications."""
    missing = epochs_to_run(
        df, condition_id, item_ids, (1, replications), require_solution=require_solution
    )
    return [iid for iid in item_ids if missing[iid]]


def resolve_wave(df: pd.DataFrame, wave_label: str, replications: int) -> "tuple[int, int]":
    """(wave_number, epoch_offset) for a labeled re-observation wave.

    A label already present in the store resumes its block (mid-wave crash
    recovery); otherwise the next free epoch block after the store's max epoch
    is allocated — new waves are new keys, never replacements.
    """
    if not df.empty and "wave_label" in df.columns:
        existing = df[df["wave_label"] == wave_label]
        if not existing.empty:
            wave = int(existing["wave"].iloc[0])
            return wave, wave * replications
    max_epoch = 0 if df.empty else int(df["epoch"].astype(int).max())
    wave = -(-max_epoch // replications)  # ceil to the next free block
    return wave, wave * replications
