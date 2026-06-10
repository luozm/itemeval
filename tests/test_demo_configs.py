"""The shipped demo configs load and expand to the documented grid sizes."""

from pathlib import Path

import pytest

from itemeval import load_config

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["usamo_demo.yaml", "usamo_demo_gate.yaml"])
def test_demo_config_loads(name):
    cfg = load_config(REPO / "configs" / name)
    assert cfg.benchmark.datasets[0].revision  # revision pinned in the file
    assert len(cfg.solvers.models) == 3
    assert all(m.startswith("mockllm/") for m in cfg.solvers.models)
    assert cfg.facets.prompt == ["builtin:minimal", "builtin:standard"]
    assert cfg.facets.replications == 2
    assert cfg.grader_spec("mock_judge").model == "mockllm/judge"
    assert cfg.budget.policy == "dev"
    # grid arithmetic: 3 models x 2 prompts x 1 model_config = 6 gen conditions
    assert (
        len(cfg.solvers.models) * len(cfg.facets.prompt) * len(cfg.facets.model_config_facet) == 6
    )


def test_gate_config_differs_only_in_gate():
    demo = load_config(REPO / "configs" / "usamo_demo.yaml")
    gate = load_config(REPO / "configs" / "usamo_demo_gate.yaml")
    assert gate.budget.confirm_above_usd == 0.0
    assert demo.budget.confirm_above_usd == 5.0
    assert gate.study != demo.study


def test_quickstart_config_verifiable_path():
    """The README quickstart config loads and expands to a free numeric scorer."""
    from itemeval.design._grid import expand_grade_grid

    cfg = load_config(REPO / "configs" / "quickstart_aime.yaml")
    assert cfg.benchmark.datasets[0].id == "MathArena/aime_2025"
    assert cfg.benchmark.datasets[0].revision  # pinned in the file for reproducibility
    assert cfg.benchmark.mapping.target == "answer"
    assert cfg.facets.scorer == "numeric"
    assert not cfg.facets.grader  # verifiable benchmark — no judge model
    assert cfg.solvers.models == ["openai/gpt-5-mini"]
    # grade grid is a single verifiable condition; no rubric templates needed.
    grade = expand_grade_grid(cfg, {})
    assert len(grade) == 1
    assert grade[0].kind == "verifiable"
    assert grade[0].scorer == "numeric"


def test_demo_templates_exist():
    demo = load_config(REPO / "configs" / "usamo_demo.yaml")
    from itemeval._templates import rubric_registry, solver_registry

    for name in demo.facets.prompt:
        assert solver_registry(demo).get(name).text
    for name in demo.facets.rubric:
        assert rubric_registry(demo).get(name).text
