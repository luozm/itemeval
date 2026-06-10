"""Store core: upserts, dedup, int round-trips, items_to_run, pending_solutions."""

import pandas as pd
import pyarrow as pa
import pytest

from itemeval._errors import StoreError
from itemeval.store._base import read_parquet_or_empty, upsert_parquet
from itemeval.store._gradings import GRADINGS_SCHEMA, pending_solutions
from itemeval.store._layout import StudyPaths
from itemeval.store._solutions import SOLUTIONS_SCHEMA, empty_solution_mask, items_to_run

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
    assert paths.logs_dir("generate", "c1") == tmp_path / "study/logs/generate/c1"


def _sol_row(cond: str, item: str, epoch: int, error=None, solution="s"):
    return {
        "study": "t",
        "run_id": "r",
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


def _grading_row(grade_cond, gen_cond, item, epoch, error=None, parse_ok=True):
    return {
        "study": "t",
        "run_id": "r",
        "grade_condition_id": grade_cond,
        "grade_condition_slug": grade_cond,
        "gen_condition_id": gen_cond,
        "item_id": item,
        "epoch": epoch,
        "grade_kind": "judge",
        "parse_ok": parse_ok,
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
