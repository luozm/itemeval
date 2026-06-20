"""Grade-stage e2e on mockllm: judge rows, parse-failure handling, decoupling."""

import pytest
from inspect_ai.model import ModelOutput, ModelUsage, get_model

from itemeval._errors import StoreError
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade
from itemeval.store._gradings import read_gradings
from itemeval.store._solutions import read_solutions


def test_grade_requires_solutions(study):
    _, prep = study
    with pytest.raises(StoreError, match="run generate first"):
        run_grade(prep)


def test_judge_e2e(study):
    _, prep = study
    run_generate(prep)
    solutions_before = read_solutions(prep.paths).copy()

    result = run_grade(prep)
    assert result.rows_written == 8  # 2 gen conditions x 2 items x 2 epochs
    assert result.parse_failures == 0
    df = read_gradings(prep.paths)
    assert len(df) == 8
    assert (df["grade_kind"] == "judge").all()
    assert df["score"].notna().all()
    assert df["parse_ok"].all()
    assert df["reasoning"].notna().all()
    assert df["judge_completion"].notna().all()
    # Cached judge calls (identical inputs across epochs of deterministic mock
    # solutions) cost $0; uncached ones are priced.
    assert df["usd"].notna().all()
    assert df["usd"].sum() > 0
    assert (df["grader_model"] == "mockllm/judge").all()

    # Two-stage decoupling: grading never touches the solutions store.
    solutions_after = read_solutions(prep.paths)
    assert solutions_after.equals(solutions_before)


def test_grade_manifest_records_endpoints(study):
    import json

    _, prep = study
    run_generate(prep)
    result = run_grade(prep)
    manifest = json.loads((prep.paths.study_dir / result.manifest_path).read_text())
    endpoints = manifest["endpoints_effective"]
    cond = prep.grid.grade[0]  # judge condition
    assert cond.id in endpoints
    assert endpoints[cond.id]["provider"] == "mockllm"
    assert set(endpoints[cond.id]) == {
        "provider",
        "base_url",
        "served_model",
        "execution_model",
        "routed",
    }
    assert endpoints[cond.id]["routed"] is False


def test_grade_resume_skips(study):
    _, prep = study
    run_generate(prep)
    run_grade(prep)
    second = run_grade(prep)
    assert all(r.status == "skipped" for r in second.conditions)


def _bad_judge_factory(model, stage, model_args=None):
    def fn(input, tools, tool_choice, config) -> ModelOutput:
        out = ModelOutput.from_content(model="bad", content="no json at all", stop_reason="stop")
        out.usage = ModelUsage(input_tokens=5, output_tokens=5, total_tokens=10)
        return out

    return get_model("mockllm/bad-judge", custom_outputs=fn)


def test_parse_failures_flagged_never_dropped_and_final(study):
    _, prep = study
    run_generate(prep)
    result = run_grade(prep, model_factory=_bad_judge_factory)
    assert result.parse_failures == 8
    df = read_gradings(prep.paths)
    assert len(df) == 8  # all rows kept
    assert (~df["parse_ok"]).all()
    assert (df["parse_error"] == "no_json_object").all()
    assert df["score"].isna().all()
    # Parse failures are final: re-running does not retry them.
    second = run_grade(prep)
    assert all(r.status == "skipped" for r in second.conditions)
    # ... but --force re-grades.
    third = run_grade(prep, force=True)
    assert third.rows_written == 8
    assert third.parse_failures == 0  # real mock judge emits valid JSON


def _seed_solutions(prep, blank_item="1"):
    """Seed the solutions store directly; blank `blank_item`'s completions
    (no error) to simulate a truncated/empty generation."""
    from itemeval.store._solutions import upsert_solutions

    rows = []
    for cond in prep.grid.generate:
        for it in prep.items_effective:
            for epoch in (1, 2):
                empty = it.id == blank_item
                rows.append(
                    {
                        "study": prep.config.study,
                        "experiment_id": "r",
                        "attempt": 1,
                        "condition_id": cond.id,
                        "condition_slug": cond.slug,
                        "item_id": it.id,
                        "dataset_id": "d",
                        "dataset_revision": "v",
                        "epoch": epoch,
                        "model": cond.model,
                        "prompt_name": cond.prompt_name,
                        "prompt_hash": "h",
                        "model_config_name": cond.model_config_name,
                        "solution": None if empty else "ANSWER: 4",
                        "stop_reason": "max_tokens" if empty else "stop",
                        "error": None,
                        "log_file": "lf",
                        "created_at": "t0",
                    }
                )
    upsert_solutions(prep.paths, rows)


def _prep_with_on_empty(study, policy):
    import yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, _ = study
    data = yaml.safe_load(cfg.config_path.read_text())
    data["solvers"]["on_empty"] = policy
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    return prepare_study(cfg2)


def test_grade_skip_reports_empty_solutions(study):
    _, prep = study  # default policy: skip
    _seed_solutions(prep, blank_item="1")
    result = run_grade(prep)
    assert result.on_empty == "skip"
    assert result.empty_total == 4  # item 1 x 2 gen conditions x 2 epochs
    assert result.empty_skipped == 4
    assert result.empty_stop_reasons == {"max_tokens": 4}
    assert result.rows_written == 4  # only item 2's non-empty solutions graded
    df = read_gradings(prep.paths)
    assert (df["item_id"] == "2").all()


def test_grade_policy_grades_empty_solutions(study):
    prep = _prep_with_on_empty(study, "grade")
    _seed_solutions(prep, blank_item="1")
    result = run_grade(prep)
    assert result.on_empty == "grade"
    assert result.empty_total == 4
    assert result.empty_skipped == 0  # graded as-is, not skipped
    assert result.rows_written == 8  # all solutions graded, including empties
    df = read_gradings(prep.paths)
    assert set(df["item_id"]) == {"1", "2"}


def _prep_with_max_solution_chars(study, max_chars):
    import yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, _ = study
    data = yaml.safe_load(cfg.config_path.read_text())
    data["graders"]["judge"]["max_solution_chars"] = max_chars
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    return prepare_study(cfg2)


def _seed_solutions_with_long(prep, long_item="1", long_chars=5000):
    """Seed the solutions store: `long_item`'s completions are an over-long loop
    output (no error), the rest are ordinary short answers."""
    from itemeval.store._solutions import upsert_solutions

    rows = []
    for cond in prep.grid.generate:
        for it in prep.items_effective:
            for epoch in (1, 2):
                long = it.id == long_item
                rows.append(
                    {
                        "study": prep.config.study,
                        "experiment_id": "r",
                        "attempt": 1,
                        "condition_id": cond.id,
                        "condition_slug": cond.slug,
                        "item_id": it.id,
                        "dataset_id": "d",
                        "dataset_revision": "v",
                        "epoch": epoch,
                        "model": cond.model,
                        "prompt_name": cond.prompt_name,
                        "prompt_hash": "h",
                        "model_config_name": cond.model_config_name,
                        "solution": ("loop " * long_chars) if long else "ANSWER: 4",
                        "stop_reason": "stop",
                        "error": None,
                        "log_file": "lf",
                        "created_at": "t0",
                    }
                )
    upsert_solutions(prep.paths, rows)


def _forbidden_judge_factory(model, stage, model_args=None):
    raise AssertionError("oversized solution must not be sent to the judge")


def test_grade_oversized_scored_zero_not_judged(study):
    """A solution over max_solution_chars is scored 0 without a judge call."""
    prep = _prep_with_max_solution_chars(study, 1000)
    _seed_solutions_with_long(prep, long_item="1", long_chars=5000)

    # The 4 oversized rows (item 1 x 2 gen conds x 2 epochs) never reach the
    # judge; the 4 short rows (item 2) do. A judge that raises if called proves
    # the oversized rows skipped it — so route every solution through the real
    # mock judge first, then assert the oversized ones were not judged.
    result = run_grade(prep)
    assert result.oversized_skipped == 4
    assert result.rows_written == 8  # 4 skipped (scored 0) + 4 judged
    df = read_gradings(prep.paths)
    over = df[df["item_id"] == "1"]
    assert len(over) == 4
    assert (over["score"] == 0.0).all()
    assert (~over["parse_ok"]).all()
    assert (over["parse_error"] == "oversized_skip").all()
    assert over["judge_completion"].isna().all()
    assert (over["usd"] == 0.0).all()
    # Oversized skips are not parse failures (they are a deliberate score-0 skip).
    assert result.parse_failures == 0
    # The short solutions were judged normally.
    short = df[df["item_id"] == "2"]
    assert short["judge_completion"].notna().all()
    assert short["parse_ok"].all()


def test_grade_oversized_never_calls_judge(study):
    """When *every* pending solution is oversized, the judge is never resolved."""
    prep = _prep_with_max_solution_chars(study, 1000)
    # Make both items oversized so no judge eval is needed at all.
    _seed_solutions_with_long(prep, long_item="1", long_chars=5000)
    from itemeval.store._solutions import read_solutions, upsert_solutions

    df = read_solutions(prep.paths)
    rows = df.to_dict("records")
    for r in rows:
        r["solution"] = "loop " * 5000  # all oversized
    upsert_solutions(prep.paths, rows)

    result = run_grade(prep, model_factory=_forbidden_judge_factory)
    assert result.oversized_skipped == 8
    assert result.rows_written == 8
    g = read_gradings(prep.paths)
    assert (g["score"] == 0.0).all()
    assert (g["parse_error"] == "oversized_skip").all()


def test_grade_no_threshold_unchanged(study):
    """None threshold (default) leaves behavior unchanged — the long solution is
    judged like any other (no skip, no score-0 marker)."""
    _, prep = study  # default config: graders.judge.max_solution_chars unset
    _seed_solutions_with_long(prep, long_item="1", long_chars=5000)
    result = run_grade(prep)
    assert result.oversized_skipped == 0
    assert result.rows_written == 8
    df = read_gradings(prep.paths)
    assert df["judge_completion"].notna().all()  # all judged, none skipped
    assert (df["parse_error"] != "oversized_skip").all()


def test_grade_grader_rubric_filters(study):
    _, prep = study
    run_generate(prep)
    none = run_grade(prep, graders=["not-a-grader"])
    assert none.conditions == []
    only = run_grade(prep, graders=["judge"], rubrics=["standard"])
    assert len(only.conditions) == 1 and only.rows_written == 8


def test_verifiable_grading_no_model(study, tmp_path):
    import yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, prep = study
    run_generate(prep)

    data = yaml.safe_load(cfg.config_path.read_text())
    data["facets"].pop("grader")
    data["facets"]["scorer"] = "exact_match"
    data.pop("graders")
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    prep2 = prepare_study(cfg2)

    def forbidden_factory(model, stage, model_args=None):
        raise AssertionError("verifiable grading must not resolve a model")

    result = run_grade(prep2, model_factory=forbidden_factory)
    assert result.rows_written == 8
    df = read_gradings(prep2.paths)
    assert (df["grade_kind"] == "verifiable").all()
    assert (df["usd"] == 0.0).all()
    assert df["log_file"].isna().all()
    # Mock solutions don't match targets, but every row is scored 0.0/1.0.
    assert df["score"].notna().all()


def _seed_orphan_roster(prep, condition_id="orphan-roster-cond"):
    """Seed solutions under a gen-condition id NOT in the current grid — the
    post-rehash 'old roster' case (a config change rehashed the gen ids, leaving
    the old draw's rows stranded in the append-only store)."""
    from itemeval.store._solutions import upsert_solutions

    upsert_solutions(
        prep.paths,
        [
            {
                "study": prep.config.study,
                "experiment_id": "old",
                "attempt": 1,
                "condition_id": condition_id,
                "condition_slug": "orphan",
                "item_id": it.id,
                "dataset_id": "d",
                "dataset_revision": "v",
                "epoch": epoch,
                "model": "mockllm/old-model",
                "prompt_name": "minimal",
                "prompt_hash": "h",
                "model_config_name": "default",
                "solution": "ANSWER: 4",
                "stop_reason": "stop",
                "error": None,
                "log_file": "lf",
                "created_at": "t0",
            }
            for it in prep.items_effective
            for epoch in (1, 2)
        ],
    )
    return condition_id


def test_grade_scopes_to_current_grid(study):
    """Grade must not judge solutions whose gen-condition left the current grid.
    The runner scopes to the current config's gen grid — the same scope `status`
    already uses — so an orphaned old roster in the store is never (re-)graded."""
    _, prep = study
    run_generate(prep)  # current grid: 2 gen conds x 2 items x 2 epochs = 8
    grid_gen_ids = {c.id for c in prep.grid.generate}
    orphan_id = _seed_orphan_roster(prep)  # + 4 stranded rows under a non-grid id
    assert orphan_id not in grid_gen_ids

    result = run_grade(prep)
    # Only the 8 current-grid solutions are graded; the 4 orphan rows are not
    # (pre-fix this graded all 12 — silent overspend + cross-roster mixing).
    assert result.rows_written == 8
    df = read_gradings(prep.paths)
    assert set(df["gen_condition_id"]) <= grid_gen_ids
    assert orphan_id not in set(df["gen_condition_id"])
