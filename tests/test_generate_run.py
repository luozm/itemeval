"""Generate-stage e2e on mockllm: store rows, resume, manifest, ledger."""

import json
from types import SimpleNamespace

import pandas as pd

from itemeval.generate._run import resolve_display, rows_from_generate_log, run_generate
from itemeval.store._ledger import read_ledger
from itemeval.store._logs import read_log_index
from itemeval.store._solutions import read_solutions


def test_resolve_display_precedence(monkeypatch):
    """Explicit value wins, then INSPECT_DISPLAY, then the "rich" default."""
    monkeypatch.delenv("INSPECT_DISPLAY", raising=False)
    assert resolve_display(None) == "rich"  # itemeval default: live progress on
    assert resolve_display("none") == "none"  # explicit opt-out is honored
    assert resolve_display("full") == "full"
    monkeypatch.setenv("INSPECT_DISPLAY", "plain")
    assert resolve_display(None) == "plain"  # env fills in when arg omitted
    assert resolve_display("full") == "full"  # explicit still beats the env


def test_generate_e2e(study):
    cfg, prep = study
    result = run_generate(prep)

    # 2 conditions x 2 dev items x 2 epochs
    assert result.rows_written == 8
    assert all(r.status == "run" for r in result.conditions)
    df = read_solutions(prep.paths)
    assert len(df) == 8
    assert set(df["epoch"]) == {1, 2}
    assert df["solution"].notna().all()
    assert df["error"].isna().all()
    assert (df["input_tokens"] > 0).all()
    assert (df["usd"] > 0).all()  # mockllm priced via seed table
    assert (df["temperature_requested"] == 0.3).all()
    assert df["log_file"].notna().all()

    # items snapshot covers ALL loaded items, not just policy scope
    items_df = pd.read_parquet(prep.paths.items)
    assert len(items_df) == 3

    # log index + ledger
    logs = read_log_index(prep.paths)
    assert len(logs) == 2 and (logs["stage"] == "generate").all()
    ledger = read_ledger(prep.paths)
    assert len(ledger) == 2
    assert ledger["usd"].sum() == df["usd"].sum()
    # provider column = the inspect prefix that determines which dashboard billed
    assert (ledger["provider"] == "mockllm").all()

    # manifest written and backfilled with effective params + endpoints
    manifest_file = prep.paths.study_dir / result.manifest_path
    manifest = json.loads(manifest_file.read_text())
    assert manifest["stage"] == "generate"
    assert manifest["replications_effective"] == 2
    assert len(manifest["grid_generate"]) == 2
    assert manifest["datasets"][0]["revision_resolved"]
    assert manifest["config_sha256"] == cfg.config_sha256
    assert manifest["sampling_effective"]  # backfilled post-run
    # endpoints backfilled per condition: provider/base_url/served_model
    endpoints = manifest["endpoints_effective"]
    assert set(endpoints) == {c.id for c in prep.grid.generate}
    one = endpoints[prep.grid.generate[0].id]
    assert one["provider"] == "mockllm"
    assert set(one) == {"provider", "base_url", "served_model", "execution_model", "routed"}
    # mock models are never native-batch-routed: execution == sampled, not routed
    assert one["routed"] is False
    assert one["execution_model"] == prep.grid.generate[0].model


def test_generate_resume_skips_complete(study):
    _, prep = study
    run_generate(prep)
    second = run_generate(prep)
    assert all(r.status == "skipped" for r in second.conditions)
    assert second.rows_written == 0


def test_generate_force_reruns(study):
    _, prep = study
    run_generate(prep)
    forced = run_generate(prep, force=True)
    assert forced.rows_written == 8
    assert len(read_solutions(prep.paths)) == 8  # upsert, no duplicates


def test_generate_condition_filter(study):
    _, prep = study
    cond = prep.grid.generate[0]
    result = run_generate(prep, condition_filter=[cond.slug])
    assert len(result.conditions) == 1
    assert result.conditions[0].condition_id == cond.id
    df = read_solutions(prep.paths)
    assert set(df["condition_id"]) == {cond.id}


def test_rows_from_errored_empty_sample(study):
    """An errored sample whose ModelOutput has empty `choices` must yield one row
    (error populated, solution/stop_reason None), not crash extracting stop_reason."""
    from inspect_ai.log import EvalError, EvalSample
    from inspect_ai.model import ModelOutput

    _, prep = study
    cond = prep.grid.generate[0]
    item_id = next(iter(prep.origins))

    sample = EvalSample(
        id=item_id,
        epoch=1,
        input="ignored",
        target="",
        output=ModelOutput(model=cond.model, choices=[]),  # errored/empty: no choices
        error=EvalError(message="provider errored", traceback="", traceback_ansi=""),
    )
    log = SimpleNamespace(samples=[sample], location="logs/errored.eval")

    rows = rows_from_generate_log(log, cond, prep, "exp1", 1)

    assert len(rows) == 1
    row = rows[0]
    assert row["experiment_id"] == "exp1" and row["attempt"] == 1
    assert row["error"] == "provider errored"
    assert row["solution"] is None
    assert row["stop_reason"] is None


def test_generate_eval_failure_reported_not_raised(study, monkeypatch):
    _, prep = study

    def broken_factory(model, stage, model_args=None):
        raise RuntimeError("provider exploded")

    result = run_generate(prep, model_factory=broken_factory)
    assert all(r.status == "error" for r in result.conditions)
    assert "provider exploded" in result.conditions[0].message
    assert read_solutions(prep.paths).empty


def test_generate_one_condition_failure_isolated(study):
    """One model failing must not block the others (conditions run in one
    parallel eval; a failure is recorded per condition, siblings proceed)."""
    from itemeval._mockmodels import resolve_model

    _, prep = study

    def partial_factory(model, stage, model_args=None):
        if model == "mockllm/solver-b":
            raise RuntimeError("solver-b down")
        return resolve_model(model, stage, model_args)

    result = run_generate(prep, model_factory=partial_factory)

    model_by_cond = {c.id: c.model for c in prep.grid.generate}
    for rep in result.conditions:
        if model_by_cond[rep.condition_id] == "mockllm/solver-b":
            assert rep.status == "error" and "solver-b down" in rep.message
        else:
            assert rep.status == "run" and rep.rows_written == 4  # 2 items x 2 epochs

    # only the healthy model's rows persisted; the failed one wrote nothing
    df = read_solutions(prep.paths)
    assert set(df["model"]) == {"mockllm/solver-a"}
    assert len(df) == 4
