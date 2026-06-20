"""recoverable-harvest: project a crashed run's `.eval` logs back into the stores.

A hard mid-run death leaves progress only in inspect's on-disk `.eval`; harvest
reads it back through the existing row builders so the stores reflect reality
again. Hermetic — mockllm, no network, no paid APIs.
"""

import glob
import os

from itemeval._harvest import _local_path, classify_logs, harvest_study
from itemeval.cli import main
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade
from itemeval.store import _gradings, _solutions


def _wipe_stores(prep):
    """Simulate a hard crash: the `.eval` logs survive on disk, the parquet stores
    were never written (or were lost)."""
    for p in glob.glob(str(prep.paths.study_dir / "**" / "*.parquet"), recursive=True):
        if any(k in os.path.basename(p) for k in ("solutions", "gradings", "ledger")):
            os.unlink(p)


def test_local_path_normalizes_file_uri():
    # read_eval_log returns file:// URIs; the stores key on the plain path the live
    # run wrote, so harvest must normalize to match (else dedup/classify break).
    assert _local_path("file:///a/b/c.eval") == "/a/b/c.eval"
    assert _local_path("logs/generate/x.eval") == "logs/generate/x.eval"


def test_harvest_recovers_generate_and_grade_after_crash(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    sol_before = len(_solutions.read_solutions(prep.paths))
    grd_before = len(_gradings.read_gradings(prep.paths))
    assert sol_before > 0 and grd_before > 0

    _wipe_stores(prep)
    assert _solutions.read_solutions(prep.paths).empty
    assert _gradings.read_gradings(prep.paths).empty

    report = harvest_study(prep)
    assert report.generate_rows == sol_before
    assert report.grade_rows == grd_before
    assert report.recovered
    # the store is whole again
    assert len(_solutions.read_solutions(prep.paths)) == sol_before
    assert len(_gradings.read_gradings(prep.paths)) == grd_before


def test_harvest_noop_when_store_already_current(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    # everything already harvested live → nothing to recover
    report = harvest_study(prep)
    assert report.rows == 0
    assert not report.recovered


def test_harvest_is_idempotent(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    n_sol = len(_solutions.read_solutions(prep.paths))
    n_grd = len(_gradings.read_gradings(prep.paths))

    _wipe_stores(prep)
    harvest_study(prep)
    second = harvest_study(prep)  # re-harvest: classifier skips already-projected logs
    assert second.rows == 0
    assert not second.recovered
    assert len(_solutions.read_solutions(prep.paths)) == n_sol
    assert len(_gradings.read_gradings(prep.paths)) == n_grd


def test_harvest_recovers_partial_generate_only(study):
    """A crash during generate (grade never ran): only solutions recover."""
    cfg, prep = study
    run_generate(prep)
    n_sol = len(_solutions.read_solutions(prep.paths))
    _wipe_stores(prep)
    report = harvest_study(prep)
    assert report.generate_rows == n_sol
    assert report.grade_rows == 0


def test_classify_logs_splits_harvested_vs_unharvested(study):
    cfg, prep = study
    run_generate(prep)
    harvested, unharvested = classify_logs(prep, "generate")
    assert unharvested == []  # live-written logs are already in the store
    assert len(harvested) >= 1

    _wipe_stores(prep)
    harvested2, unharvested2 = classify_logs(prep, "generate")
    assert harvested2 == []
    assert len(unharvested2) >= 1


def test_harvest_preserves_wave_identity(study):
    """A `--wave` run's harvested rows keep their wave / wave_label / epoch block —
    recovered from the run's manifest, not defaulted to wave 0."""
    cfg, prep = study
    run_generate(prep)  # wave 0
    run_generate(prep, wave="w1")  # wave 1: a new epoch block
    sol = _solutions.read_solutions(prep.paths)
    labels_before = sorted({v for v in sol["wave_label"].dropna()})
    epochs_before = sorted(set(sol["epoch"].astype(int)))
    assert "w1" in labels_before
    assert max(epochs_before) > prep.plan.replications  # the wave shifted the block

    _wipe_stores(prep)
    harvest_study(prep)
    sol2 = _solutions.read_solutions(prep.paths)
    assert sorted({v for v in sol2["wave_label"].dropna()}) == labels_before
    assert sorted(set(sol2["epoch"].astype(int))) == epochs_before


def test_harvest_skips_condition_outside_current_grid(study):
    """A `.eval` whose condition left the current grid (config changed since the
    crash) is skipped, never errored — harvest recovers only current conditions."""
    cfg, prep = study
    run_generate(prep)
    _wipe_stores(prep)
    prep.grid.generate = []  # the "current" grid no longer has these conditions
    report = harvest_study(prep)
    assert report.generate_rows == 0
    assert not report.recovered


def test_cli_harvest_command_recovers_and_reports(study, capsys):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    _wipe_stores(prep)
    rc = main(["harvest", "config.yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recovered" in out
    assert len(_solutions.read_solutions(prep.paths)) > 0


def test_cli_harvest_nothing_to_recover(study, capsys):
    cfg, prep = study
    run_generate(prep)
    rc = main(["harvest", "config.yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to recover" in out


def test_cli_status_auto_harvests_and_announces(study, capsys):
    cfg, prep = study
    run_generate(prep)
    _wipe_stores(prep)
    main(["status", "config.yaml"])
    out = capsys.readouterr().out
    assert any(line.startswith("recovered") for line in out.splitlines())
    # store was refreshed: status now sees completed rows
    assert len(_solutions.read_solutions(prep.paths)) > 0


def test_cli_no_harvest_leaves_store_untouched(study, capsys):
    cfg, prep = study
    run_generate(prep)
    _wipe_stores(prep)
    main(["status", "config.yaml", "--no-harvest"])
    out = capsys.readouterr().out
    assert not any(line.startswith("recovered") for line in out.splitlines())
    assert _solutions.read_solutions(prep.paths).empty


def test_generate_resume_after_harvest_does_not_re_pay(study):
    """The recovery payoff: harvest a crashed run, then a re-run resumes (skips the
    recovered cells) instead of re-generating them."""
    cfg, prep = study
    run_generate(prep)
    n_sol = len(_solutions.read_solutions(prep.paths))
    _wipe_stores(prep)
    harvest_study(prep)
    result = run_generate(prep)  # everything already present → all skipped
    assert result.rows_written == 0
    assert all(r.status == "skipped" for r in result.conditions)
    assert len(_solutions.read_solutions(prep.paths)) == n_sol
