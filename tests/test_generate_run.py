"""Generate-stage e2e on mockllm: store rows, resume, manifest, ledger."""

import json
from types import SimpleNamespace

import pandas as pd

from itemeval.generate._run import resolve_display, rows_from_generate_log, run_generate
from itemeval.store._ledger import read_ledger
from itemeval.store._logs import read_log_index
from itemeval.store._solutions import read_solutions


def test_attempt_timeout_bounds_retries(study):
    """Regression: attempt_timeout without max_retries must not retry forever
    (inspect's stop_never). The task gets a bounded max_retries so a stalled call
    gives up and the cell errors, instead of looping."""
    from itemeval._endpoints import RETRY_AFTER_TIMEOUT
    from itemeval.generate._task import build_generate_task

    _, prep = study
    cond = prep.grid.generate[0]
    template = prep.solver_templates[cond.prompt_name]

    def _cfg(attempt_timeout, max_retries):
        return build_generate_task(
            prep.items_effective,
            cond,
            template,
            prep.config.study,
            prep.plan.replications,
            prep.config.cache,
            prep.origins,
            attempt_timeout=attempt_timeout,
            max_retries=max_retries,
        ).config

    # The bug case: timeout set, no cap -> bounded default (was None = retry forever).
    assert _cfg(600, None).max_retries == RETRY_AFTER_TIMEOUT
    assert _cfg(600, None).attempt_timeout == 600
    # An explicit cap wins; no timeout leaves it to inspect (None).
    assert _cfg(600, 5).max_retries == 5
    assert _cfg(None, None).max_retries is None
    assert _cfg(None, 3).max_retries == 3


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
    assert result.cells_filled == 0  # a fresh run is all whole-missing — no holes
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
    assert second.cells_filled == 0 and second.items_holed == 0  # no holes -> no fill


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


def _corrupt_to_soft_failure(prep, *, n=1):
    """Overwrite the first `n` stored solution rows into soft failures (HTTP 200 +
    finish=error, blank text) — the output-validity-reroute detection target.

    Direct-writes (not `upsert_solutions`): the quality-preferring merge would
    refuse to downgrade the existing good row to a blank soft failure."""
    from conftest import force_write_solutions

    from itemeval.store._solutions import read_solutions

    df = read_solutions(prep.paths)
    rows = []
    for i in range(n):
        bad = df.iloc[i].to_dict()
        bad["solution"] = ""
        bad["native_finish_reason"] = "error"
        bad["served_provider"] = "BadCo"
        rows.append(bad)
    force_write_solutions(prep, rows)
    return rows


def test_reroute_recovers_soft_failure(study):
    """A soft-failed cell is re-issued and recovered by the reroute (the main eval
    skips it as 'done'; Phase 4 re-runs it on a fresh backend)."""
    _, prep = study
    run_generate(prep)  # 8 good rows
    bad = _corrupt_to_soft_failure(prep)[0]

    prep.config.solvers.max_reroutes = 2
    result = run_generate(prep)  # default mock recovers on re-issue

    assert result.reroute_recovered == 1
    assert result.reroute_unresolved == 0
    assert result.rerouted >= 1
    df = read_solutions(prep.paths)
    cell = df[
        (df["condition_id"] == bad["condition_id"])
        & (df["item_id"] == bad["item_id"])
        & (df["epoch"] == bad["epoch"])
    ]
    assert len(cell) == 1  # overwritten in place at the original epoch, not duplicated
    assert cell["native_finish_reason"].isna().all()  # recovered: no soft-failure marker
    assert (cell["solution"].str.len() > 0).all()


def test_reroute_off_by_default(study):
    """max_reroutes=None (default) leaves a soft-failed cell untouched."""
    _, prep = study
    run_generate(prep)
    bad = _corrupt_to_soft_failure(prep)[0]

    result = run_generate(prep)  # max_reroutes unset

    assert result.rerouted == 0 and result.reroute_recovered == 0
    df = read_solutions(prep.paths)
    cell = df[
        (df["condition_id"] == bad["condition_id"])
        & (df["item_id"] == bad["item_id"])
        & (df["epoch"] == bad["epoch"])
    ]
    assert cell["native_finish_reason"].iloc[0] == "error"  # untouched


def _drop_cell(prep, condition_id, item_id, epoch):
    """Remove one (condition, item, epoch) solution row, punching a hole into an
    otherwise-complete item (cell-granular-resume test setup)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from itemeval.store._base import _coerce_to_schema
    from itemeval.store._solutions import SOLUTIONS_SCHEMA, read_solutions

    df = read_solutions(prep.paths)
    keep = df[
        ~(
            (df["condition_id"] == condition_id)
            & (df["item_id"] == item_id)
            & (df["epoch"] == epoch)
        )
    ]
    table = pa.Table.from_pandas(
        _coerce_to_schema(keep, SOLUTIONS_SCHEMA), schema=SOLUTIONS_SCHEMA, preserve_index=False
    )
    pq.write_table(table, prep.paths.solutions)


def _fill_factory(text="FILLED-NEW ANSWER: 1"):
    """A model factory whose output differs from the stored siblings — so a
    re-drawn sibling would visibly change, proving it was (not) re-run."""
    from inspect_ai.model import ModelOutput, get_model

    def fn(input, tools, tool_choice, config):
        return ModelOutput.from_content(model="mockllm/m", content=text, stop_reason="stop")

    return lambda model, stage, model_args=None: get_model(model, custom_outputs=fn)


def test_resume_fills_hole_without_touching_siblings(study):
    """The incident regression: a holed item (one epoch missing) refills ONLY the
    missing cell — the completed sibling epoch is never re-drawn or overwritten."""
    _, prep = study
    run_generate(prep)  # 8 good rows (2 conds x 2 items x 2 epochs)
    df0 = read_solutions(prep.paths)
    cond = prep.grid.generate[0]
    item = prep.items_effective[0]
    sibling_text = df0[
        (df0["condition_id"] == cond.id) & (df0["item_id"] == item.id) & (df0["epoch"] == 1)
    ].iloc[0]["solution"]

    _drop_cell(prep, cond.id, item.id, 2)  # punch a hole at epoch 2; epoch 1 stays
    assert len(read_solutions(prep.paths)) == 7

    result = run_generate(prep, model_factory=_fill_factory())

    assert result.cells_filled == 1 and result.items_holed == 1
    assert result.rows_written == 1  # only the missing cell ran (not the whole item)
    df = read_solutions(prep.paths)
    assert len(df) == 8  # hole refilled, nothing duplicated
    filled = df[
        (df["condition_id"] == cond.id) & (df["item_id"] == item.id) & (df["epoch"] == 2)
    ].iloc[0]
    assert "FILLED-NEW" in filled["solution"]  # the missing cell got the fresh draw
    sib_now = df[
        (df["condition_id"] == cond.id) & (df["item_id"] == item.id) & (df["epoch"] == 1)
    ].iloc[0]
    assert sib_now["solution"] == sibling_text  # sibling untouched (never re-run)


def test_resume_mixed_whole_missing_and_holed(study):
    """A condition with both a whole-missing item (main epochs=N path) and a holed
    item (cell-granular fill): both run, the holed item's sibling stays, and the
    per-condition report merges the main + filled rows."""
    _, prep = study
    run_generate(prep)
    df0 = read_solutions(prep.paths)
    cond = prep.grid.generate[0]
    holed, whole = prep.items_effective[0], prep.items_effective[1]
    sibling_text = df0[
        (df0["condition_id"] == cond.id) & (df0["item_id"] == holed.id) & (df0["epoch"] == 1)
    ].iloc[0]["solution"]

    _drop_cell(prep, cond.id, holed.id, 2)  # holed item: epoch 2 missing
    _drop_cell(prep, cond.id, whole.id, 1)  # whole-missing item: both epochs gone
    _drop_cell(prep, cond.id, whole.id, 2)
    assert len(read_solutions(prep.paths)) == 5

    result = run_generate(prep, model_factory=_fill_factory())

    assert result.cells_filled == 1 and result.items_holed == 1  # holed item only
    assert result.rows_written == 3  # 2 (whole item, main) + 1 (hole, fill)
    rep = next(r for r in result.conditions if r.condition_id == cond.id)
    assert rep.status == "run" and rep.items_run == 2 and rep.rows_written == 3
    df = read_solutions(prep.paths)
    assert len(df) == 8
    sib_now = df[
        (df["condition_id"] == cond.id) & (df["item_id"] == holed.id) & (df["epoch"] == 1)
    ].iloc[0]
    assert sib_now["solution"] == sibling_text  # holed item's sibling untouched


def test_reroute_unresolved_after_cap(study):
    """A backend that keeps soft-failing exhausts the cap and leaves an honest
    residue — the loop is bounded (no infinite retry)."""
    from inspect_ai.model import ModelOutput, get_model

    _, prep = study
    prep.config.solvers.max_reroutes = 2

    def unknown_fn(input, tools, tool_choice, config):
        # HTTP-200 soft failure: inspect flattens the provider reason to "unknown".
        return ModelOutput.from_content(model="mockllm/m", content="x", stop_reason="unknown")

    def unknown_factory(model, stage, model_args=None):
        return get_model(model, custom_outputs=unknown_fn)

    result = run_generate(prep, model_factory=unknown_factory)

    # 2 conds x 2 items x 2 epochs = 8 cells, all soft-invalid; 2 reroute rounds
    # re-issue all 8 each round (16) and none recover.
    assert result.reroute_unresolved == 8
    assert result.rerouted == 16  # bounded by the cap: 8 cells x 2 rounds
    assert result.reroute_recovered == 0
    assert any(h.code == "reroute-residue" for h in result.hints)
