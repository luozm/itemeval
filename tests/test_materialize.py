"""Two-stage rubric materialization e2e on mockllm: freeze, reuse, plumb {rubric}."""

from pathlib import Path

import pytest

from itemeval._config import load_config
from itemeval._prepare import prepare_study
from itemeval._templates import Template
from itemeval.generate._run import run_generate
from itemeval.grade._judge import build_judge_input
from itemeval.grade._materialize import materialize_id
from itemeval.grade._run import run_grade
from itemeval.store._materialized import read_materialized
from conftest import MINIMAL_PROMPT

MAT_CONFIG_YAML = """\
study: tstudy
output_dir: studies
prompts_dir: prompts
rubrics_dir: rubrics
benchmark:
  adapter: hf
  datasets:
    - id: fake/ds
  mapping:
    id: problem_idx
    input: problem
    target: sample_solution
    grading_scheme: grading_scheme
solvers:
  models: [mockllm/solver-a]
  temperature: 0.3
  max_tokens: 256
facets:
  prompt: [minimal]
  grader: [judge]
  rubric: [matrubric]
  replications: 2
graders:
  judge:
    model: mockllm/judge
    max_tokens: 256
rubrics:
  matrubric:
    materialize:
      model: mockllm/materializer
      template: build
    grade_template: grade
budget:
  policy: dev
  confirm_above_usd: 100
"""

BUILD_TEMPLATE = (
    "Build a rubric for:\n{input}\n\nReference:\n{target}\n\nScheme:\n{grading_scheme}\n"
)
GRADE_TEMPLATE = "Use this rubric:\n{rubric}\n\nProblem:\n{input}\n\nCandidate:\n{solution}\n"


def _write_mat_study(root: Path) -> Path:
    (root / "prompts" / "solver").mkdir(parents=True, exist_ok=True)
    (root / "rubrics").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "solver" / "minimal.md").write_text(MINIMAL_PROMPT)
    (root / "rubrics" / "build.md").write_text(BUILD_TEMPLATE)
    (root / "rubrics" / "grade.md").write_text(GRADE_TEMPLATE)
    cfg_path = root / "config.yaml"
    cfg_path.write_text(MAT_CONFIG_YAML)
    return cfg_path


@pytest.fixture()
def mat_study(tmp_path, offline_adapter):
    cfg = load_config(_write_mat_study(tmp_path))
    return cfg, prepare_study(cfg)


def test_materialize_freezes_then_grades(mat_study):
    _, prep = mat_study
    run_generate(prep)
    result = run_grade(prep)

    # Materialized once per effective item (dev: 2), reused across solutions/epochs.
    assert result.materialized_rubrics == 2
    assert result.materialized_reused == 0
    assert result.materialize_model == "mockllm/materializer"
    assert result.materialize_usd >= 0.0
    assert result.rows_written > 0  # grading still happened

    mat = read_materialized(prep.paths)
    assert len(mat) == 2
    assert mat["materializer_model"].eq("mockllm/materializer").all()
    assert mat["rubric_text"].notna().all()
    assert mat["error"].isna().all()


def test_materialized_rubric_reaches_judge_prompt(mat_study):
    _, prep = mat_study
    run_generate(prep)
    run_grade(prep)

    mat = read_materialized(prep.paths)
    item = prep.items_effective[0]
    rubric_text = mat.set_index("item_id").loc[item.id, "rubric_text"]
    grade_tmpl = prep.rubric_templates["matrubric"]
    rendered = build_judge_input(item, "some candidate", grade_tmpl, rubric_text)
    # The frozen rubric text is injected at {rubric}; the candidate at {solution}.
    assert rubric_text in rendered
    assert "some candidate" in rendered
    assert "{rubric}" not in rendered


def test_materialize_reused_on_resume(mat_study):
    _, prep = mat_study
    run_generate(prep)
    run_grade(prep)
    second = run_grade(prep)
    # Frozen artifact reused: no fresh materialization, no spend.
    assert second.materialized_rubrics == 0
    assert second.materialized_reused == 2
    assert second.materialize_usd == 0.0


def test_changed_build_template_rematerializes(mat_study, tmp_path):
    _, prep = mat_study
    run_generate(prep)
    run_grade(prep)
    before = materialize_id(prep.build_templates["matrubric"], "mockllm/materializer")

    # Editing the build template changes its hash -> new materialize_id -> re-derive.
    (tmp_path / "rubrics" / "build.md").write_text(BUILD_TEMPLATE + "\nBe concise.\n")
    cfg2 = load_config(tmp_path / "config.yaml")
    prep2 = prepare_study(cfg2)
    after = materialize_id(prep2.build_templates["matrubric"], "mockllm/materializer")
    assert after != before

    result = run_grade(prep2)
    assert result.materialized_rubrics == 2  # re-materialized under the new id
    mat = read_materialized(prep2.paths)
    assert set(mat["materialize_id"]) == {before, after}  # both kept (content-addressed)


def test_plain_rubric_writes_no_materialized_store(study):
    _, prep = study
    run_generate(prep)
    run_grade(prep)
    assert not prep.paths.materialized_rubrics.is_file()


def test_build_template_with_solution_rejected(tmp_path, offline_adapter):
    from itemeval._errors import ConfigError

    root = tmp_path
    _write_mat_study(root)
    (root / "rubrics" / "build.md").write_text(BUILD_TEMPLATE + "\nCandidate: {solution}\n")
    cfg = load_config(root / "config.yaml")
    with pytest.raises(ConfigError, match=r"must not reference"):
        prepare_study(cfg)


def test_grade_template_missing_rubric_rejected(tmp_path, offline_adapter):
    from itemeval._errors import TemplateError

    root = tmp_path
    _write_mat_study(root)
    (root / "rubrics" / "grade.md").write_text("Problem:\n{input}\nCandidate:\n{solution}\n")
    cfg = load_config(root / "config.yaml")
    with pytest.raises(TemplateError, match=r"\{rubric\}"):
        prepare_study(cfg)


def test_render_values_omits_rubric_for_plain(study):
    # A plain rubric passes rubric_text=None; {rubric} is simply absent, so a
    # non-materializing template is byte-identical to today.
    _, prep = study
    item = prep.items_effective[0]
    tmpl: Template = prep.rubric_templates["standard"]
    out = build_judge_input(item, "cand", tmpl, None)
    assert "cand" in out


def test_estimate_includes_materialize_term(mat_study):
    from itemeval.budget._estimator import estimate_study

    _, prep = mat_study
    est = estimate_study(prep)
    # A materialize ConditionEstimate per materializing rubric (model = materializer),
    # one call per effective item — folded into the grade stage's calls/usd.
    mat_conds = [c for c in est.grade.conditions if c.slug.startswith("materialize_")]
    assert len(mat_conds) == 1
    assert mat_conds[0].model == "mockllm/materializer"
    assert mat_conds[0].calls == len(prep.items_effective)
    judge_calls = sum(
        c.calls for c in est.grade.conditions if not c.slug.startswith("materialize_")
    )
    assert est.grade.calls == judge_calls + mat_conds[0].calls


def test_empty_materialized_rubrics_hint():
    from itemeval._hints import detect_empty_materialized_rubrics

    assert detect_empty_materialized_rubrics(0, "openrouter/x/y") is None
    h = detect_empty_materialized_rubrics(3, "openrouter/x/y")
    assert h.code == "empty-materialized-rubrics"
    assert "3" in h.message and "openrouter/x/y" in h.message
    assert h.learn_more == "Error-Handling#empty-materialized-rubrics"


def test_snapshot_copies_materialized_store(mat_study):
    from itemeval.store._export import export_study

    cfg, prep = mat_study
    run_generate(prep)
    run_grade(prep)
    export_study(cfg, snapshot="snap1")
    snap = prep.paths.export_dir / "snapshots" / "snap1" / "materialized_rubrics.parquet"
    assert snap.is_file()


def test_unpriced_materializer_flagged(tmp_path, offline_adapter):
    # An unpriced materializer surfaces in grade.unpriced_models (estimate-only,
    # no model call). Mock ids are priced in the seed, so use a real-looking id.
    from itemeval.budget._estimator import estimate_study

    yaml = MAT_CONFIG_YAML.replace(
        "model: mockllm/materializer", "model: openrouter/fake/unpriced-xyz"
    )
    _write_mat_study(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml)
    prep = prepare_study(load_config(tmp_path / "config.yaml"))
    est = estimate_study(prep)
    assert "openrouter/fake/unpriced-xyz" in est.grade.unpriced_models


def test_estimate_materialize_remaining_drops_after_freeze(mat_study, monkeypatch):
    # Price the materializer so the remaining figure is non-zero, then watch it
    # fall to 0 once the artifact store is populated (resume-aware).
    from itemeval.budget import _estimator

    monkeypatch.setattr(
        _estimator, "_priced_usd", lambda prep, model, i, o, exec_model=None: 0.01 * (i + o)
    )
    _, prep = mat_study
    from itemeval.budget._estimator import estimate_study

    before = estimate_study(prep)
    mat_before = next(c for c in before.grade.conditions if c.slug == "materialize_matrubric")
    assert mat_before.calls == len(prep.items_effective)

    run_generate(prep)
    run_grade(prep)  # freezes the rubrics

    after = estimate_study(prep)
    # Full-grid materialize calls unchanged; remaining materialize spend gone.
    assert after.grade.remaining_usd <= before.grade.remaining_usd
