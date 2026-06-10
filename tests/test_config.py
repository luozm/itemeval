import pytest

from itemeval import ExperimentConfig, load_config
from itemeval._config import GraderSpec
from itemeval._errors import ConfigError

# The README "Experiment config (sketch)" YAML, verbatim (comments dropped).
README_SKETCH = """\
study: my_study
benchmark:
  adapter: hf
  datasets:
    - id: SomeOrg/some_benchmark
  mapping: {input: question, target: answer}
solvers:
  models: [openai/gpt-5-mini, anthropic/claude-haiku-4-5, openrouter/deepseek/deepseek-v3.2]
  temperature: 0.7
facets:
  prompt: [builtin:minimal, builtin:standard]
  grader: [judge_a, judge_b]
  rubric: [builtin:standard]
  replications: 4
graders:
  judge_a: {model: openai/gpt-5-mini}
  judge_b: {model: anthropic/claude-haiku-4-5}
crossing: full
budget:
  policy: dev
  confirm_above_usd: 5
  batch: auto
"""


def test_readme_sketch_validates():
    import yaml

    cfg = ExperimentConfig.model_validate(yaml.safe_load(README_SKETCH))
    assert cfg.facets.scorer is None
    assert cfg.facets.rubric == ["builtin:standard"]
    assert cfg.budget.batch == "auto"
    assert cfg.facets.replications == 4
    assert [m.name for m in cfg.facets.model_config_facet] == ["default"]
    assert cfg.grader_spec("judge_a").model == "openai/gpt-5-mini"


def test_facets_default_to_builtin_standard():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["facets"] = {"grader": ["judge_a"]}  # omit prompt/rubric -> defaults apply
    data["graders"] = {"judge_a": {"model": "mockllm/judge"}}
    cfg = ExperimentConfig.model_validate(data)
    assert cfg.facets.prompt == ["builtin:standard"]
    assert cfg.facets.rubric == ["builtin:standard"]


def test_grader_unresolved_raises_config_error():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data.pop("graders")
    cfg = ExperimentConfig.model_validate(data)  # shape-valid without graders:
    with pytest.raises(ConfigError, match="judge_a"):
        cfg.grader_spec("judge_a")


def test_grader_resolution():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["graders"] = {"judge_a": {"model": "mockllm/judge"}}
    cfg = ExperimentConfig.model_validate(data)
    assert cfg.grader_spec("judge_a").model == "mockllm/judge"
    assert cfg.grader_spec("openai/gpt-5-mini") == GraderSpec(model="openai/gpt-5-mini")


def test_model_config_alias():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["facets"]["model_config"] = [
        {"name": "thinking", "reasoning_effort": "high"},
        {"name": "plain"},
    ]
    cfg = ExperimentConfig.model_validate(data)
    assert [m.name for m in cfg.facets.model_config_facet] == ["thinking", "plain"]


def test_typo_rejected():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["solvers"]["temprature"] = 1.0
    with pytest.raises(Exception):
        ExperimentConfig.model_validate(data)


def test_facets_require_grading():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["facets"].pop("grader")
    with pytest.raises(Exception, match="grader / scorer"):
        ExperimentConfig.model_validate(data)


def test_grader_temperature_rejected():
    # Judge temperature is pinned to 0.0 in v0.1; the field does not exist.
    with pytest.raises(Exception):
        GraderSpec(model="m/j", temperature=0.5)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_sets_private_state(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(README_SKETCH)
    cfg = load_config(p, work_dir=tmp_path)
    assert cfg.config_dir == tmp_path
    assert cfg.work_dir == tmp_path
    assert cfg.config_path == p
    assert len(cfg.config_sha256) == 64
    assert cfg.study_dir == (tmp_path / "studies" / "my_study").resolve()


def test_load_config_rejects_non_mapping(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)
