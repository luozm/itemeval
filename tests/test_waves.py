"""Waves: re-observation over time as additive epoch blocks (growth-ux 3.x)."""

import json

import pandas as pd
import pytest

from itemeval import cli
from itemeval._config import load_config
from itemeval._prepare import prepare_study
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade
from itemeval.store._solutions import epochs_to_run, read_solutions, resolve_wave
from itemeval.store._layout import StudyPaths
from conftest import write_study_files


def _paths(config):
    return StudyPaths(load_config(config).study_dir)


def test_resolve_wave_allocates_next_block():
    empty = pd.DataFrame()
    assert resolve_wave(empty, "w", 2) == (0, 0)
    df = pd.DataFrame({"epoch": [1, 2], "wave": [0, 0], "wave_label": [None, None]})
    assert resolve_wave(df, "w", 2) == (1, 2)  # epochs 3..4
    # an existing label resumes its block instead of allocating a new one
    df2 = pd.DataFrame(
        {"epoch": [1, 2, 3, 4], "wave": [0, 0, 1, 1], "wave_label": [None, None, "w", "w"]}
    )
    assert resolve_wave(df2, "w", 2) == (1, 2)
    assert resolve_wave(df2, "w2", 2) == (2, 4)


def test_epochs_to_run_subset():
    df = pd.DataFrame(
        {
            "condition_id": ["c"] * 3,
            "item_id": ["a", "a", "b"],
            "epoch": [3, 4, 3],
            "error": [None, None, None],
            "solution": ["x", "y", "z"],
        }
    )
    missing = epochs_to_run(df, "c", ["a", "b"], (3, 4))
    assert missing == {"a": set(), "b": {4}}


def test_wave_rows_never_replace_wave_zero(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    before = read_solutions(prep.paths)
    wave0 = before.set_index(["condition_id", "item_id", "epoch"])["created_at"].to_dict()

    result = run_generate(prep, display="none", wave="2026-07")
    assert result.wave == 1 and result.wave_label == "2026-07" and result.epoch_offset == 2
    after = read_solutions(prep.paths)
    assert len(after) == 16  # 8 wave-0 + 8 wave-1 rows; key-disjoint
    assert sorted(after["epoch"].unique()) == [1, 2, 3, 4]
    # wave-0 rows byte-stable (same created_at -> not rewritten)
    for key, created in wave0.items():
        row = after.set_index(["condition_id", "item_id", "epoch"]).loc[key]
        assert row["created_at"] == created
    wave1 = after[after["wave"] == 1]
    assert set(wave1["epoch"].astype(int)) == {3, 4}
    assert (wave1["wave_label"] == "2026-07").all()


def test_wave_runs_cache_off_fresh_draws(tmp_path, offline_adapter):
    """Wave rows must be real calls (usage present), never local-cache replays
    of the wave-0 draws — even though the prompts are byte-identical."""
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")  # warms the local response cache
    result = run_generate(prep, display="none", wave="w1")
    assert result.local_cache_rows == 0  # no replays
    wave1 = read_solutions(prep.paths).query("wave == 1")
    assert wave1["total_tokens"].notna().all()  # fresh draws carry usage


def test_mid_wave_crash_resumes_within_block(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    run_generate(prep, display="none", wave="w1")
    # simulate a crash: drop one item's wave-1 rows for every condition
    df = read_solutions(prep.paths)
    victim = df[df["wave"] == 1]["item_id"].iloc[0]
    df = df[~((df["wave"] == 1) & (df["item_id"] == victim))]
    df.to_parquet(prep.paths.solutions)

    result = run_generate(prep, display="none", wave="w1")
    assert result.wave == 1 and result.epoch_offset == 2  # resumed, not re-allocated
    assert all(r.items_run == 1 for r in result.conditions if r.status == "run")
    assert len(read_solutions(prep.paths)) == 16  # block complete again


def test_old_store_reads_with_wave_zero(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    # strip the wave columns to simulate a pre-wave store
    df = pd.read_parquet(prep.paths.solutions).drop(columns=["wave", "wave_label"])
    df.to_parquet(prep.paths.solutions)
    loaded = read_solutions(prep.paths)
    assert (loaded["wave"] == 0).all()
    assert loaded["wave_label"].isna().all()


def test_grade_wave_grades_that_block(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    run_grade(prep, display="none")
    run_generate(prep, display="none", wave="w1")
    result = run_grade(prep, display="none", wave="w1")
    assert result.wave == 1 and result.rows_written == 8
    from itemeval.store._gradings import read_gradings

    gradings = read_gradings(prep.paths)
    wave1 = gradings[gradings["wave"] == 1]
    assert len(wave1) == 8
    assert set(wave1["epoch"].astype(int)) == {3, 4}
    assert (wave1["wave_label"] == "w1").all()
    # plain grade stays scoped to wave 0 (zero noise): nothing new to do
    again = run_grade(prep, display="none")
    assert again.rows_written == 0
    # the grade matrix stays wave-0-scoped (8/8, not 16/8); per-wave graded
    # counts carry the wave's grading progress instead
    from itemeval._status import build_status

    report = build_status(cfg, prep)
    assert report.grade[0].expected == 8 and report.grade[0].completed == 8
    assert [(w.wave, w.graded, w.grade_expected) for w in report.waves] == [(0, 8, 8), (1, 8, 8)]


def test_grade_unknown_wave_errors(tmp_path, offline_adapter):
    from itemeval._errors import StoreError

    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    with pytest.raises(StoreError, match="no solutions for wave 'nope'"):
        run_grade(prep, display="none", wave="nope")


def test_export_carries_wave_columns(tmp_path, offline_adapter):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert cli.main(["grade", str(config), "--yes"]) == 0
    assert cli.main(["generate", str(config), "--yes", "--wave", "w1"]) == 0
    assert cli.main(["grade", str(config), "--yes", "--wave", "w1"]) == 0
    assert cli.main(["export", str(config)]) == 0
    long = pd.read_parquet(_paths(config).export_dir / "gradings_long.parquet")
    assert sorted(long["wave"].dropna().astype(int).unique()) == [0, 1]
    assert set(long[long["wave"] == 1]["wave_label"]) == {"w1"}


def test_status_silent_at_one_wave_per_wave_at_two(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["status", str(config)]) == 0
    assert "waves:" not in capsys.readouterr().out
    assert cli.main(["generate", str(config), "--yes", "--wave", "w1"]) == 0
    capsys.readouterr()
    assert cli.main(["status", str(config)]) == 0
    out = capsys.readouterr().out
    assert "waves: 0 — gen 8/8 · graded 0/8, 1 (w1) — gen 8/8 · graded 0/8" in out


def test_wave_status_excludes_stranded_drift_rows(tmp_path, offline_adapter):
    """Rows stranded under a drifted (abandoned) condition must not count toward
    per-wave totals — expected comes from the current grid, so counting them
    showed >100% (e.g. 16/8)."""
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert cli.main(["generate", str(config), "--yes", "--wave", "w1"]) == 0
    # sampling drift: all 16 rows stay stranded under the old condition id
    config.write_text(config.read_text().replace("temperature: 0.3", "temperature: 0.9"))
    assert cli.main(["generate", str(config), "--yes"]) == 0  # wave 0 under the new id
    from itemeval._status import build_status

    report = build_status(load_config(config))
    assert all(w.completed <= w.expected for w in report.waves)
    assert [(w.wave, w.completed, w.expected) for w in report.waves] == [(0, 8, 8)]
    # the grade matrix is scoped the same way: stranded/wave gradings never
    # push done past expected
    assert all(c.completed <= c.expected for c in report.grade)


def test_wave_cli_announcement_summary_and_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["generate", str(config), "--yes", "--wave", "2026-07"]) == 0
    out = capsys.readouterr().out
    assert "wave 2026-07: local response cache off — re-observations must be fresh draws" in out
    assert "wave 2026-07: epochs 3–4 · 8 rows ·" in out
    assert cli.main(["generate", str(config), "--yes", "--wave", "2026-07", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["wave"] == 1 and doc["wave_label"] == "2026-07" and doc["epoch_offset"] == 2
    # the wave was already complete: second run skips everything
    assert all(c["status"] == "skipped" for c in doc["conditions"])


def test_manifest_and_ledger_record_offset(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    result = run_generate(prep, display="none", wave="w1")
    manifest = json.loads((cfg.study_dir / result.manifest_path).read_text())
    assert manifest["wave"] == 1
    assert manifest["wave_label"] == "w1"
    assert manifest["epoch_offset"] == 2
    from itemeval.store._ledger import read_ledger

    ledger = read_ledger(prep.paths)
    wave_rows = ledger[
        (ledger["experiment_id"] == result.experiment_id) & (ledger["attempt"] == result.attempt)
    ]
    assert (wave_rows["epoch_offset"] == 2).all()
