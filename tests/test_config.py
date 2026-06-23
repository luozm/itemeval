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


def test_on_empty_default_and_validation():
    import yaml

    cfg = ExperimentConfig.model_validate(yaml.safe_load(README_SKETCH))
    assert cfg.solvers.on_empty == "skip"  # default

    data = yaml.safe_load(README_SKETCH)
    data["solvers"]["on_empty"] = "rerun"
    assert ExperimentConfig.model_validate(data).solvers.on_empty == "rerun"

    data["solvers"]["on_empty"] = "bogus"
    with pytest.raises(Exception, match="on_empty"):
        ExperimentConfig.model_validate(data)


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


def test_mapping_id_accepts_composite_forms():
    from itemeval._config import MappingSpec

    assert MappingSpec(input="q", id="problem_idx").id == "problem_idx"
    assert MappingSpec(input="q", id=["{dataset}", "problem_idx"]).id == [
        "{dataset}",
        "problem_idx",
    ]


def test_mapping_id_rejects_empty():
    from itemeval._config import MappingSpec

    with pytest.raises(Exception, match="non-empty"):
        MappingSpec(input="q", id=[])
    with pytest.raises(Exception, match="non-empty"):
        MappingSpec(input="q", id=["ok", " "])


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


def test_solvers_sample_pricing_table_with_where():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["solvers"] = {
        "sample": {
            "n": 20,
            "seed": 7,
            "stratify_by": "provider",
            "universe": "pricing-table",
            "where": {"provider": ["anthropic", "openai"], "max_output_usd_per_mtok": 15},
        }
    }
    cfg = ExperimentConfig.model_validate(data)
    assert cfg.solvers.models == []  # filled by the draw at prepare time
    assert cfg.solvers.sample.n == 20
    assert cfg.solvers.sample.where.provider == ["anthropic", "openai"]


def test_solvers_models_xor_sample():
    import yaml

    both = yaml.safe_load(README_SKETCH)
    both["solvers"]["sample"] = {"n": 2, "seed": 1, "universe": "pricing-table"}
    with pytest.raises(Exception, match="exactly one of models / sample"):
        ExperimentConfig.model_validate(both)

    neither = yaml.safe_load(README_SKETCH)
    neither["solvers"].pop("models")
    with pytest.raises(Exception, match="exactly one of models / sample"):
        ExperimentConfig.model_validate(neither)


def test_sample_inline_list_universe():
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["solvers"] = {"sample": {"n": 2, "seed": 1, "universe": ["m/a", "m/b", "m/c"]}}
    cfg = ExperimentConfig.model_validate(data)
    assert cfg.solvers.sample.universe == ["m/a", "m/b", "m/c"]

    data["solvers"]["sample"]["n"] = 5  # n > universe size
    with pytest.raises(Exception, match="exceeds"):
        ExperimentConfig.model_validate(data)

    data["solvers"]["sample"] = {"n": 2, "seed": 1, "universe": ["m/a", "m/a", "m/b"]}
    with pytest.raises(Exception, match="unique"):
        ExperimentConfig.model_validate(data)


def test_solvers_retry_on_error_bounds():
    from itemeval._config import SolversConfig

    # default None (= itemeval's built-in 1); 0 (fail-fast) and positive accepted;
    # negative rejected (ge=0, unlike max_retries' ge=1 — 0 is the meaningful value).
    assert SolversConfig(models=["mockllm/m"]).retry_on_error is None
    assert SolversConfig(models=["mockllm/m"], retry_on_error=0).retry_on_error == 0
    assert SolversConfig(models=["mockllm/m"], retry_on_error=2).retry_on_error == 2
    with pytest.raises(Exception):
        SolversConfig(models=["mockllm/m"], retry_on_error=-1)


def test_sample_where_rejected_for_curated_universe():
    import yaml

    for universe in (["m/a", "m/b"], "models.txt"):  # inline list and file path
        data = yaml.safe_load(README_SKETCH)
        data["solvers"] = {
            "sample": {"n": 1, "seed": 1, "universe": universe, "where": {"provider": ["m"]}}
        }
        with pytest.raises(Exception, match="pricing-table"):
            ExperimentConfig.model_validate(data)


def test_sample_stratify_and_extra_forbid():
    import yaml

    base = {"n": 2, "seed": 1, "universe": "pricing-table"}
    for sample in (
        {**base, "stratify_by": "family"},
        {**base, "bogus": 1},
        {**base, "where": {"x": 1}},
    ):
        data = yaml.safe_load(README_SKETCH)
        data["solvers"] = {"sample": sample}
        with pytest.raises(Exception):
            ExperimentConfig.model_validate(data)


def _sample_cfg(sample: dict):
    import yaml

    data = yaml.safe_load(README_SKETCH)
    data["solvers"] = {"sample": sample}
    return ExperimentConfig.model_validate(data)


def test_sample_recency_dimensions():
    base = {"n": 2, "seed": 1, "universe": "pricing-table"}
    # released_after + recency stratify accepted on a pricing-table universe
    cfg = _sample_cfg({**base, "stratify_by": "recency", "where": {"released_after": "2025-01-01"}})
    assert cfg.solvers.sample.stratify_by == "recency"
    assert cfg.solvers.sample.where.released_after == "2025-01-01"
    # malformed cutoff rejected at load
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        _sample_cfg({**base, "where": {"released_after": "Jan 2025"}})
    # recency reads roster metadata -> pricing-table only
    with pytest.raises(Exception, match="requires universe: pricing-table"):
        _sample_cfg({"n": 1, "seed": 1, "universe": ["m/a", "m/b"], "stratify_by": "recency"})
    # released_after lives on where -> rejected for a curated (list) universe
    with pytest.raises(Exception, match="pricing-table"):
        _sample_cfg(
            {"n": 1, "seed": 1, "universe": ["m/a"], "where": {"released_after": "2025-01-01"}}
        )


def test_sample_allocation_equal_requires_stratify():
    base = {"n": 2, "seed": 1, "universe": "pricing-table"}
    cfg = _sample_cfg({**base, "stratify_by": "provider", "allocation": "equal"})
    assert cfg.solvers.sample.allocation == "equal"
    assert _sample_cfg(base).solvers.sample.allocation == "proportional"  # default
    with pytest.raises(Exception, match="equal requires stratify_by"):
        _sample_cfg({**base, "allocation": "equal"})
    with pytest.raises(Exception):  # unknown allocation value
        _sample_cfg({**base, "stratify_by": "provider", "allocation": "weighted"})


def test_sample_include_validation():
    base = {"n": 3, "seed": 1, "universe": "pricing-table"}
    cfg = _sample_cfg({**base, "include": ["openrouter/o/a", "openrouter/o/b"]})
    assert cfg.solvers.sample.include == ["openrouter/o/a", "openrouter/o/b"]
    with pytest.raises(Exception, match="include entries must be unique"):
        _sample_cfg({**base, "include": ["m/a", "m/a"]})
    with pytest.raises(Exception, match="include.*exceeds n"):
        _sample_cfg({"n": 1, "seed": 1, "universe": "pricing-table", "include": ["m/a", "m/b"]})
