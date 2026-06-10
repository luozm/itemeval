import pytest
import yaml

from itemeval import ExperimentConfig
from itemeval._errors import ConfigError, TemplateError
from itemeval._templates import Template
from itemeval._util import sha256_hex
from itemeval.design._grid import (
    expand_generate_grid,
    expand_grid,
    resolve_gen_params,
)

CONFIG = """\
study: g
benchmark:
  adapter: hf
  datasets: [{id: a/b}]
  mapping: {input: q}
solvers:
  models: [mockllm/m1, mockllm/m2]
  temperature: 0.7
  max_tokens: 100
facets:
  prompt: [p1, p2]
  grader: [j]
  rubric: [r1]
  replications: 3
  model_config:
    - {name: plain}
    - {name: think, reasoning_effort: high, temperature: 1.0}
graders:
  j: {model: mockllm/judge}
"""


def _template(name: str, text: str) -> Template:
    return Template(
        name=name, source="local", path=f"/t/{name}.md", text=text, sha256=sha256_hex(text.encode())
    )


def _cfg() -> ExperimentConfig:
    return ExperimentConfig.model_validate(yaml.safe_load(CONFIG))


SOLVERS = {"p1": _template("p1", "A {input}"), "p2": _template("p2", "B {input}")}
RUBRICS = {"r1": _template("r1", "{input} {solution}")}


def test_full_crossing_order_and_count():
    grid = expand_grid(_cfg(), SOLVERS, RUBRICS)
    assert len(grid.generate) == 2 * 2 * 2  # models x prompts x model_configs
    assert grid.replications == 3
    # Deterministic order: model -> prompt -> model_config.
    first = grid.generate[0]
    assert (first.model, first.prompt_name, first.model_config_name) == (
        "mockllm/m1",
        "p1",
        "plain",
    )
    assert len(grid.grade) == 1
    assert grid.grade[0].kind == "judge"


def test_param_resolution_facet_overrides():
    cfg = _cfg()
    plain, think = cfg.facets.model_config_facet
    p = resolve_gen_params(cfg.solvers, plain)
    assert (p.temperature, p.max_tokens) == (0.7, 100)
    t = resolve_gen_params(cfg.solvers, think)
    assert (t.temperature, t.reasoning_effort) == (1.0, "high")


def test_condition_id_changes_with_prompt_content():
    grid1 = expand_generate_grid(_cfg(), SOLVERS)
    changed = dict(SOLVERS)
    changed["p1"] = _template("p1", "A2 {input}")
    grid2 = expand_generate_grid(_cfg(), changed)
    ids1 = {(c.model, c.prompt_name, c.model_config_name): c.id for c in grid1}
    ids2 = {(c.model, c.prompt_name, c.model_config_name): c.id for c in grid2}
    for key, cid in ids1.items():
        if key[1] == "p1":
            assert ids2[key] != cid
        else:
            assert ids2[key] == cid


def test_condition_ids_stable_across_expansions():
    a = expand_grid(_cfg(), SOLVERS, RUBRICS)
    b = expand_grid(_cfg(), SOLVERS, RUBRICS)
    assert [c.id for c in a.generate] == [c.id for c in b.generate]
    assert [c.id for c in a.grade] == [c.id for c in b.grade]


def test_solver_template_requires_input_placeholder():
    bad = {"p1": _template("p1", "no placeholder"), "p2": SOLVERS["p2"]}
    with pytest.raises(TemplateError, match=r"\{input\}"):
        expand_generate_grid(_cfg(), bad)


def test_unresolvable_grader_raises():
    cfg = _cfg()
    cfg.graders.pop("j")
    with pytest.raises(ConfigError, match="'j'"):
        expand_grid(cfg, SOLVERS, RUBRICS)


def test_verifiable_condition():
    data = yaml.safe_load(CONFIG)
    data["facets"].pop("grader")
    data["facets"]["scorer"] = "exact_match"
    data.pop("graders")
    cfg = ExperimentConfig.model_validate(data)
    grid = expand_grid(cfg, SOLVERS, {})
    assert len(grid.grade) == 1
    assert grid.grade[0].kind == "verifiable"
    assert grid.grade[0].scorer == "exact_match"
    assert grid.grade[0].payload == {"kind": "grade", "scorer": "exact_match"}
