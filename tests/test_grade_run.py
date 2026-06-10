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


def test_grade_resume_skips(study):
    _, prep = study
    run_generate(prep)
    run_grade(prep)
    second = run_grade(prep)
    assert all(r.status == "skipped" for r in second.conditions)


def _bad_judge_factory(model, stage):
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

    def forbidden_factory(model, stage):
        raise AssertionError("verifiable grading must not resolve a model")

    result = run_grade(prep2, model_factory=forbidden_factory)
    assert result.rows_written == 8
    df = read_gradings(prep2.paths)
    assert (df["grade_kind"] == "verifiable").all()
    assert (df["usd"] == 0.0).all()
    assert df["log_file"].isna().all()
    # Mock solutions don't match targets, but every row is scored 0.0/1.0.
    assert df["score"].notna().all()
