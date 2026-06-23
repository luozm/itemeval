"""Store core: upserts, dedup, int round-trips, items_to_run, pending_solutions."""

import pandas as pd
import pyarrow as pa
import pytest

from itemeval._errors import StoreError
from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._gradings import GRADING_KEY, GRADINGS_SCHEMA, pending_solutions
from itemeval.store._layout import StudyPaths
from itemeval.store._solutions import (
    SOLUTION_KEY,
    SOLUTIONS_SCHEMA,
    empty_solution_mask,
    items_to_run,
    truncated_mask,
)

TEST_SCHEMA = pa.schema(
    [
        pa.field("k", pa.string(), nullable=False),
        pa.field("epoch", pa.int32(), nullable=False),
        pa.field("count", pa.int64()),
        pa.field("flag", pa.bool_()),
        pa.field("note", pa.string()),
    ]
)
KEY = ["k", "epoch"]


def test_upsert_dedups_keeping_last(tmp_path):
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path, [{"k": "a", "epoch": 1, "count": 1, "flag": True, "note": "old"}], KEY, TEST_SCHEMA
    )
    upsert_parquet(
        path,
        [
            {"k": "a", "epoch": 1, "count": 2, "flag": False, "note": "new"},
            {"k": "b", "epoch": 1, "count": 3, "flag": True, "note": None},
        ],
        KEY,
        TEST_SCHEMA,
    )
    df = pd.read_parquet(path)
    assert len(df) == 2
    row_a = df[df["k"] == "a"].iloc[0]
    assert row_a["count"] == 2 and row_a["note"] == "new"


REC_SCHEMA = pa.schema(
    [
        pa.field("k", pa.string(), nullable=False),
        pa.field("epoch", pa.int32(), nullable=False),
        pa.field("attempt", pa.int32(), nullable=False),
        pa.field("note", pa.string()),
    ]
)

ERR_SCHEMA = pa.schema(
    [
        pa.field("k", pa.string(), nullable=False),
        pa.field("epoch", pa.int32(), nullable=False),
        pa.field("attempt", pa.int32(), nullable=False),
        pa.field("error", pa.string()),
        pa.field("note", pa.string()),
    ]
)


def test_error_pref_nonerror_wins_same_attempt(tmp_path):
    """Two rows at the SAME attempt — one valid, one error — must resolve to the
    valid one regardless of write order (the harvest-oscillation case)."""
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "error": None, "note": "ok"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "error": "Timeout", "note": "err"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1 and df.iloc[0]["note"] == "ok" and pd.isna(df.iloc[0]["error"])


def test_error_pref_later_error_does_not_clobber_earlier_success(tmp_path):
    """A *higher-attempt* error must not erase an earlier recovered solution
    (single-provider backend flips valid→404 on retry)."""
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "error": None, "note": "valid"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 7, "error": "404", "note": "later-err"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1 and df.iloc[0]["note"] == "valid"


def test_quality_pref_valid_beats_empty_same_attempt(tmp_path):
    """valid (non-blank) > empty (blank) at the same attempt, either write order —
    the case the error-only rule missed (empty is also error-null)."""
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "error": None, "note": "a real proof"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
        content_col="note",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "error": None, "note": ""}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
        content_col="note",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1 and df.iloc[0]["note"] == "a real proof"


def test_error_pref_higher_attempt_success_still_wins(tmp_path):
    """Among non-error rows, the highest attempt still wins (recency intact)."""
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 5, "error": None, "note": "old-ok"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 7, "error": None, "note": "new-ok"}],
        KEY,
        ERR_SCHEMA,
        recency_col="attempt",
        error_col="error",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1 and df.iloc[0]["note"] == "new-ok"


def test_recency_upsert_keeps_highest_attempt_regardless_of_write_order(tmp_path):
    """A re-applied *older* attempt must never clobber a newer one (the
    recoverable-harvest re-upserts crashed `.eval` rows as 'new'). With
    recency_col='attempt' the highest attempt per key always survives, even when
    written last."""
    path = tmp_path / "t.parquet"
    # attempt 7 lands a valid row first, then a stale attempt-6 error re-applies.
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 7, "note": "valid"}],
        KEY,
        REC_SCHEMA,
        recency_col="attempt",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 6, "note": "stale-error"}],
        KEY,
        REC_SCHEMA,
        recency_col="attempt",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1
    assert df.iloc[0]["attempt"] == 7 and df.iloc[0]["note"] == "valid"


def test_recency_upsert_same_attempt_lets_new_win(tmp_path):
    """Within equal recency the genuine rewrite (written later) still wins."""
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 3, "note": "old"}],
        KEY,
        REC_SCHEMA,
        recency_col="attempt",
    )
    upsert_parquet(
        path,
        [{"k": "a", "epoch": 1, "attempt": 3, "note": "new"}],
        KEY,
        REC_SCHEMA,
        recency_col="attempt",
    )
    df = pd.read_parquet(path)
    assert len(df) == 1 and df.iloc[0]["note"] == "new"


def test_upsert_int_roundtrip_with_nulls(tmp_path):
    path = tmp_path / "t.parquet"
    upsert_parquet(
        path, [{"k": "a", "epoch": 1, "count": None, "flag": True, "note": None}], KEY, TEST_SCHEMA
    )
    table = pa.parquet.read_table(path)
    assert table.schema.field("count").type == pa.int64()
    assert table.schema.field("epoch").type == pa.int32()
    df = pd.read_parquet(path)
    assert pd.isna(df["count"].iloc[0])


def test_upsert_missing_columns_filled_as_null(tmp_path):
    path = tmp_path / "t.parquet"
    upsert_parquet(path, [{"k": "a", "epoch": 1}], KEY, TEST_SCHEMA)
    df = pd.read_parquet(path)
    assert pd.isna(df["count"].iloc[0]) and pd.isna(df["note"].iloc[0])


def test_upsert_bad_cast_raises_store_error(tmp_path):
    path = tmp_path / "t.parquet"
    with pytest.raises(StoreError, match="schema cast failed"):
        upsert_parquet(path, [{"k": "a", "epoch": 1, "count": "not-a-number"}], KEY, TEST_SCHEMA)
    assert not path.exists()  # atomic: nothing written on failure


def test_upsert_empty_rows_noop(tmp_path):
    path = tmp_path / "t.parquet"
    assert upsert_parquet(path, [], KEY, TEST_SCHEMA) == 0
    assert not path.exists()


def test_read_parquet_or_empty(tmp_path):
    df = read_parquet_or_empty(tmp_path / "missing.parquet", TEST_SCHEMA)
    assert df.empty and list(df.columns) == list(TEST_SCHEMA.names)


def test_study_paths(tmp_path):
    paths = StudyPaths(tmp_path / "study")
    paths.ensure()
    assert paths.manifests_dir.is_dir() and paths.export_dir.is_dir()
    assert paths.logs_stage_dir("generate") == tmp_path / "study/logs/generate"


def _sol_row(
    cond: str,
    item: str,
    epoch: int,
    error=None,
    solution="s",
    stop_reason="stop",
    native_finish_reason=None,
):
    return {
        "study": "t",
        "experiment_id": "r",
        "attempt": 1,
        "condition_id": cond,
        "condition_slug": cond,
        "item_id": item,
        "dataset_id": "d",
        "dataset_revision": "v",
        "epoch": epoch,
        "model": "m",
        "prompt_name": "p",
        "prompt_hash": "h",
        "model_config_name": "mc",
        "solution": solution,
        "stop_reason": stop_reason,
        "native_finish_reason": native_finish_reason,
        "error": error,
        "log_file": "lf",
        "created_at": "t0",
    }


def _sol_df(rows):
    df = pd.DataFrame(rows)
    for col in SOLUTIONS_SCHEMA.names:
        if col not in df.columns:
            df[col] = None
    return df


def test_items_to_run_resume_logic():
    df = _sol_df(
        [
            _sol_row("c1", "1", 1),
            _sol_row("c1", "1", 2),  # item 1 complete
            _sol_row("c1", "2", 1),  # item 2 missing epoch 2
            _sol_row("c1", "3", 1),
            _sol_row("c1", "3", 2, error="boom"),  # errored epoch
        ]
    )
    assert items_to_run(df, "c1", ["1", "2", "3", "4"], 2) == ["2", "3", "4"]
    assert items_to_run(df, "other", ["1"], 2) == ["1"]
    empty = _sol_df([]).iloc[0:0]
    assert items_to_run(empty, "c1", ["1"], 2) == ["1"]


def test_empty_solution_mask():
    df = _sol_df(
        [
            _sol_row("c1", "1", 1, solution="text"),  # gradable
            _sol_row("c1", "1", 2, solution=None),  # empty (null)
            _sol_row("c1", "2", 1, solution="   "),  # empty (whitespace)
            _sol_row("c1", "2", 2, solution="", error="boom"),  # error channel, not empty-empty
        ]
    )
    assert empty_solution_mask(df).tolist() == [False, True, True, False]
    assert empty_solution_mask(_sol_df([]).iloc[0:0]).tolist() == []


def test_truncated_mask():
    df = _sol_df(
        [
            _sol_row("c1", "1", 1, solution="cut", stop_reason="max_tokens"),  # truncated
            _sol_row("c1", "1", 2, solution="cut", stop_reason="model_length"),  # truncated
            _sol_row("c1", "2", 1, solution=None, stop_reason="max_tokens"),  # empty, NOT truncated
            _sol_row("c1", "2", 2, solution="full", stop_reason="stop"),  # clean
            _sol_row(
                "c1", "3", 1, solution="cut", stop_reason="content_filter"
            ),  # refusal, not len
            _sol_row(
                "c1", "3", 2, solution="cut", stop_reason="max_tokens", error="boom"
            ),  # errored
        ]
    )
    assert truncated_mask(df).tolist() == [True, True, False, False, False, False]
    # disjoint from empty: the empty max_tokens row is in empty, not truncated
    assert empty_solution_mask(df).tolist() == [False, False, True, False, False, False]
    assert truncated_mask(_sol_df([]).iloc[0:0]).tolist() == []


def test_soft_invalid_mask():
    from itemeval.store._solutions import soft_invalid_mask

    df = _sol_df(
        [
            # provider marked the call failed (200 + finish=error)
            _sol_row("c1", "1", 1, solution="", native_finish_reason="error"),
            # inspect flattened an unmapped reason to "unknown"
            _sol_row("c1", "1", 2, solution="x", stop_reason="unknown"),
            _sol_row("c1", "2", 1, solution="ok", stop_reason="stop"),  # clean
            # an API error is the error channel (already retried), NOT a soft failure
            _sol_row("c1", "2", 2, solution=None, native_finish_reason="error", error="boom"),
        ]
    )
    assert soft_invalid_mask(df).tolist() == [True, True, False, False]
    assert soft_invalid_mask(_sol_df([]).iloc[0:0]).tolist() == []


class _FakeCall:
    def __init__(self, response):
        self.response = response


class _FakeEvent:
    def __init__(self, response=None):
        if response is not None:
            self.call = _FakeCall(response)


class _FakeSample:
    def __init__(self, events):
        self.events = events


def test_served_provider_finish_extracts_from_events():
    from itemeval.generate._run import served_provider_finish

    # A model event carrying both fields → both extracted.
    s = _FakeSample(
        [_FakeEvent({"provider": "GMICloud", "choices": [{"native_finish_reason": "error"}]})]
    )
    assert served_provider_finish(s) == ("GMICloud", "error")

    # Last non-empty value wins across events (final call on a retry sample).
    s2 = _FakeSample(
        [
            _FakeEvent({"provider": "GMICloud", "choices": [{"native_finish_reason": "error"}]}),
            _FakeEvent({"provider": "Fireworks", "choices": [{"native_finish_reason": "stop"}]}),
        ]
    )
    assert served_provider_finish(s2) == ("Fireworks", "stop")

    # Non-model events (no .call) and missing fields are skipped safely.
    s3 = _FakeSample([_FakeEvent(), _FakeEvent({"choices": [{}]})])
    assert served_provider_finish(s3) == (None, None)
    assert served_provider_finish(_FakeSample([])) == (None, None)
    assert served_provider_finish(_FakeSample(None)) == (None, None)


def test_read_backfills_provenance_columns_on_old_store(tmp_path):
    """provider-finish-capture: a store written before served_provider /
    native_finish_reason existed still reads back, with the columns defaulted to
    null (the additive-by-construction invariant — DEVELOPMENT.md schema gate)."""
    from itemeval.store._gradings import read_gradings
    from itemeval.store._solutions import PROVENANCE_COLS, read_solutions, upsert_solutions

    paths = StudyPaths(tmp_path / "study")
    paths.ensure()

    # Write an "old" solutions parquet whose schema lacks the provenance columns.
    old_sol_schema = pa.schema([f for f in SOLUTIONS_SCHEMA if f.name not in PROVENANCE_COLS])
    old_sol = pd.DataFrame([_sol_row("c1", "1", 1)])
    old_sol = old_sol[[n for n in old_sol_schema.names if n in old_sol.columns]]
    for col in old_sol_schema.names:
        if col not in old_sol.columns:
            old_sol[col] = None
    upsert_parquet(
        paths.solutions, old_sol[list(old_sol_schema.names)], SOLUTION_KEY, old_sol_schema
    )
    assert all(c not in pd.read_parquet(paths.solutions).columns for c in PROVENANCE_COLS)

    sol = read_solutions(paths)
    for c in PROVENANCE_COLS:
        assert c in sol.columns and sol[c].isna().all()
    # A fresh upsert then carries the columns for real.
    upsert_solutions(
        paths,
        [
            {
                **_sol_row("c1", "1", 1),
                "served_provider": "Fireworks",
                "native_finish_reason": "stop",
            }
        ],
    )
    sol2 = read_solutions(paths)
    assert sol2["served_provider"].iloc[0] == "Fireworks"

    # Same guard for gradings.
    old_grad_schema = pa.schema([f for f in GRADINGS_SCHEMA if f.name not in PROVENANCE_COLS])
    grad_row = {n: None for n in old_grad_schema.names}
    grad_row.update(
        {
            "study": "t",
            "experiment_id": "r",
            "attempt": 1,
            "grade_condition_id": "g1",
            "grade_condition_slug": "g1",
            "gen_condition_id": "c1",
            "item_id": "1",
            "epoch": 1,
            "grade_kind": "judge",
            "parse_ok": True,
            "created_at": "t0",
        }
    )
    upsert_parquet(paths.gradings, [grad_row], GRADING_KEY, old_grad_schema)
    assert all(c not in pd.read_parquet(paths.gradings).columns for c in PROVENANCE_COLS)
    grad = read_gradings(paths)
    for c in PROVENANCE_COLS:
        assert c in grad.columns and grad[c].isna().all()


def test_pending_solutions_solution_fingerprint():
    """grade-solution-fingerprint: a done grade whose stored solution_hash no longer
    matches the current solution is stale -> pending again (auto re-grade); a matching
    hash stays done; a null hash (old store) is treated as unknown -> matching."""
    from itemeval.store._gradings import solution_fingerprint

    solutions = _sol_df(
        [
            _sol_row("c1", "1", 1, solution="orig"),  # graded against "orig", unchanged
            _sol_row("c1", "2", 1, solution="changed"),  # solution overwritten since grading
            _sol_row("c1", "3", 1, solution="x"),  # graded by an old (null-hash) store
        ]
    )
    gradings = _grading_df(
        [
            _grading_row("g1", "c1", "1", 1, solution_hash=solution_fingerprint("orig")),  # match
            _grading_row("g1", "c1", "2", 1, solution_hash=solution_fingerprint("orig")),  # stale
            _grading_row("g1", "c1", "3", 1, solution_hash=None),  # unknown -> matches
        ]
    )
    pending = pending_solutions(solutions, gradings, "g1", force=False)
    keys = {(r.condition_id, r.item_id, int(r.epoch)) for r in pending.itertuples()}
    assert keys == {("c1", "2", 1)}  # only the changed solution re-grades


def test_read_backfills_solution_hash_on_old_store(tmp_path):
    """grade-solution-fingerprint: a gradings store written before solution_hash
    existed still reads back (column defaulted null), and a null hash is treated as
    matching by pending_solutions — an old store never forces a global re-grade
    (the additive-by-construction invariant, DEVELOPMENT.md schema gate)."""
    from itemeval.store._gradings import read_gradings

    paths = StudyPaths(tmp_path / "study")
    paths.ensure()

    old_schema = pa.schema([f for f in GRADINGS_SCHEMA if f.name != "solution_hash"])
    row = {n: None for n in old_schema.names}
    row.update(
        {
            "study": "t",
            "experiment_id": "r",
            "attempt": 1,
            "grade_condition_id": "g1",
            "grade_condition_slug": "g1",
            "gen_condition_id": "c1",
            "item_id": "1",
            "epoch": 1,
            "grade_kind": "judge",
            "parse_ok": True,
            "created_at": "t0",
        }
    )
    upsert_parquet(paths.gradings, [row], GRADING_KEY, old_schema)
    assert "solution_hash" not in pd.read_parquet(paths.gradings).columns

    grad = read_gradings(paths)
    assert "solution_hash" in grad.columns and grad["solution_hash"].isna().all()
    # The null-hash done row counts as matching -> the cell is not re-graded.
    solutions = _sol_df([_sol_row("c1", "1", 1, solution="anything")])
    assert pending_solutions(solutions, grad, "g1", force=False).empty


def test_items_to_run_require_solution_reruns_empties():
    df = _sol_df(
        [
            _sol_row("c1", "1", 1, solution="ok"),
            _sol_row("c1", "1", 2, solution=None),  # empty, no error
        ]
    )
    # default: an empty no-error row still counts as done (skip/grade policies)
    assert items_to_run(df, "c1", ["1"], 2) == []
    # rerun policy: the empty epoch makes item 1 incomplete again
    assert items_to_run(df, "c1", ["1"], 2, require_solution=True) == ["1"]


def _grading_row(grade_cond, gen_cond, item, epoch, error=None, parse_ok=True, solution_hash=None):
    return {
        "study": "t",
        "experiment_id": "r",
        "attempt": 1,
        "grade_condition_id": grade_cond,
        "grade_condition_slug": grade_cond,
        "gen_condition_id": gen_cond,
        "item_id": item,
        "epoch": epoch,
        "grade_kind": "judge",
        "parse_ok": parse_ok,
        "solution_hash": solution_hash,
        "error": error,
        "created_at": "t0",
    }


def _grading_df(rows):
    df = pd.DataFrame(rows)
    for col in GRADINGS_SCHEMA.names:
        if col not in df.columns:
            df[col] = None
    return df


def test_pending_solutions_rules():
    solutions = _sol_df(
        [
            _sol_row("c1", "1", 1),
            _sol_row("c1", "1", 2),
            _sol_row("c1", "2", 1, error="gen failed", solution=None),  # not gradable
        ]
    )
    gradings = _grading_df(
        [
            _grading_row("g1", "c1", "1", 1),  # done
            _grading_row("g1", "c1", "1", 2, error="judge died"),  # pending again
        ]
    )
    pending = pending_solutions(solutions, gradings, "g1", force=False)
    keys = {(r.condition_id, r.item_id, int(r.epoch)) for r in pending.itertuples()}
    assert keys == {("c1", "1", 2)}
    # parse failures are final, not pending
    gradings2 = _grading_df(
        [
            _grading_row("g1", "c1", "1", 1, parse_ok=False),
            _grading_row("g1", "c1", "1", 2),
        ]
    )
    assert pending_solutions(solutions, gradings2, "g1", force=False).empty
    # force re-grades everything gradable
    assert len(pending_solutions(solutions, gradings2, "g1", force=True)) == 2
    # different grade condition sees everything as pending
    assert len(pending_solutions(solutions, gradings, "g2", force=False)) == 2


def test_pending_solutions_empty_handling():
    solutions = _sol_df(
        [
            _sol_row("c1", "1", 1, solution="text"),  # gradable
            _sol_row("c1", "1", 2, solution=None),  # empty (no error)
            _sol_row("c1", "2", 1, error="gen failed", solution=None),  # error: never gradable
        ]
    )
    empty = _grading_df([])
    # default: empties excluded; only the non-empty no-error row is pending
    keys = {
        (r.item_id, int(r.epoch))
        for r in pending_solutions(solutions, empty, "g1", force=False).itertuples()
    }
    assert keys == {("1", 1)}
    # include_empty (grade policy): the empty no-error row is now pending too,
    # but the errored row is still excluded
    keys_incl = {
        (r.item_id, int(r.epoch))
        for r in pending_solutions(
            solutions, empty, "g1", force=False, include_empty=True
        ).itertuples()
    }
    assert keys_incl == {("1", 1), ("1", 2)}
