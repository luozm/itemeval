"""Gradings store: one row per grading event."""

import pandas as pd
import pyarrow as pa

from itemeval.store._base import assert_identity_current, read_parquet_or_empty, upsert_parquet
from itemeval.store._layout import StudyPaths
from itemeval.store._solutions import empty_solution_mask

GRADING_KEY = ["grade_condition_id", "gen_condition_id", "item_id", "epoch"]

GRADINGS_SCHEMA = pa.schema(
    [
        pa.field("study", pa.string(), nullable=False),
        pa.field("experiment_id", pa.string(), nullable=False),  # run identity (see _solutions)
        pa.field("attempt", pa.int32(), nullable=False),
        pa.field("grade_condition_id", pa.string(), nullable=False),
        pa.field("grade_condition_slug", pa.string(), nullable=False),
        pa.field("gen_condition_id", pa.string(), nullable=False),
        pa.field("item_id", pa.string(), nullable=False),
        pa.field("epoch", pa.int32(), nullable=False),
        pa.field("grade_kind", pa.string(), nullable=False),
        pa.field("grader_name", pa.string()),
        pa.field("grader_model", pa.string()),
        pa.field("rubric_name", pa.string()),
        pa.field("rubric_hash", pa.string()),
        pa.field("scorer_name", pa.string()),
        pa.field("score", pa.float64()),
        pa.field("score_raw", pa.string()),
        pa.field("parse_ok", pa.bool_(), nullable=False),
        pa.field("parse_error", pa.string()),
        pa.field("reasoning", pa.string()),
        pa.field("judge_completion", pa.string()),
        # Raw judge-call provenance (provider-finish-capture): the OpenRouter
        # backend that served the judge call and its raw finish_reason before
        # inspect's stop_reason flatten. Null for verifiable/skip rows (no model
        # call), mock models, and cache replays. Same columns as solutions.
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
        pa.field("log_file", pa.string()),
        pa.field("created_at", pa.string(), nullable=False),
        # Wave provenance, inherited from the graded solution row (default 0/null).
        pa.field("wave", pa.int32()),  # nullable: null reads as wave 0
        pa.field("wave_label", pa.string()),
    ]
)


def read_gradings(paths: StudyPaths) -> pd.DataFrame:
    from itemeval.store._solutions import _backfill_provenance, _backfill_wave

    df = assert_identity_current(
        read_parquet_or_empty(paths.gradings, GRADINGS_SCHEMA), paths.gradings
    )
    return _backfill_provenance(_backfill_wave(df))


def upsert_gradings(paths: StudyPaths, rows: "list[dict]") -> int:
    return upsert_parquet(paths.gradings, rows, GRADING_KEY, GRADINGS_SCHEMA)


def pending_solutions(
    solutions_df: pd.DataFrame,
    gradings_df: pd.DataFrame,
    grade_condition_id: str,
    force: bool,
    *,
    include_empty: bool = False,
) -> pd.DataFrame:
    """Gradable solutions rows not yet finally graded under this grade condition.

    A solutions row is pending iff no gradings row exists for
    (grade_condition_id, gen condition, item, epoch) with error null. Rows with
    parse_ok=False are final (not re-run); rows with error set are pending again.

    Empty no-error completions are excluded unless `include_empty=True` (the
    `grade` empty-solution policy, which grades the empty answer as-is).
    """
    base = solutions_df[solutions_df["error"].isna()]
    gradable = base if include_empty else base[~empty_solution_mask(base)]
    if force or gradings_df.empty:
        return gradable
    done = gradings_df[
        (gradings_df["grade_condition_id"] == grade_condition_id) & (gradings_df["error"].isna())
    ]
    if done.empty:
        return gradable
    done_keys = set(zip(done["gen_condition_id"], done["item_id"], done["epoch"].astype(int)))
    mask = [
        (row.condition_id, row.item_id, int(row.epoch)) not in done_keys
        for row in gradable.itertuples()
    ]
    return gradable[mask]
