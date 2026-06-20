"""request-timeout: solvers/graders attempt_timeout pass-through.

A per-attempt request timeout (inspect's GenerateConfig.attempt_timeout) lets a
stalled model attempt be abandoned and retried (via OpenRouter, possibly onto a
healthier route). Opt-in (default None). It is a pure execution/robustness knob,
so it must never move a condition id or the experiment_id digest — the guard
tests below freeze that.
"""

import copy

import pandas as pd
import pytest

from itemeval._config import ExperimentConfig
from itemeval._identity import experiment_id, normalized_config_digest
from itemeval._item import Item
from itemeval._templates import Template
from itemeval._util import sha256_hex
from itemeval.design._grid import expand_generate_grid, expand_grade_grid
from itemeval.generate._task import build_generate_task
from itemeval.grade._judge import build_judge_task

BASE = {
    "study": "s",
    "benchmark": {"adapter": "hf", "datasets": [{"id": "org/ds"}], "mapping": {"input": "q"}},
    "solvers": {"models": ["mockllm/m1", "mockllm/m2"], "max_tokens": 256},
    "facets": {"prompt": ["p"], "grader": ["judge"], "rubric": ["r1"], "replications": 2},
    "graders": {"judge": {"model": "mockllm/judge", "max_tokens": 256}},
}


def _template(text: str, name: str = "t") -> Template:
    return Template(
        name=name, source="local", path=f"/x/{name}.md", text=text, sha256=sha256_hex(text.encode())
    )


def _with_timeouts() -> dict:
    cfg = copy.deepcopy(BASE)
    cfg["solvers"]["attempt_timeout"] = 120
    cfg["graders"]["judge"]["attempt_timeout"] = 90
    return cfg


# --- config validation ---------------------------------------------------------


def test_config_accepts_attempt_timeout_on_solvers_and_grader():
    cfg = ExperimentConfig.model_validate(_with_timeouts())
    assert cfg.solvers.attempt_timeout == 120
    assert cfg.grader_spec("judge").attempt_timeout == 90


def test_config_defaults_attempt_timeout_to_none():
    cfg = ExperimentConfig.model_validate(BASE)
    assert cfg.solvers.attempt_timeout is None
    assert cfg.grader_spec("judge").attempt_timeout is None


@pytest.mark.parametrize("bad", [0, -5])
def test_config_rejects_nonpositive_attempt_timeout(bad):
    data = copy.deepcopy(BASE)
    data["solvers"]["attempt_timeout"] = bad
    with pytest.raises(Exception, match="attempt_timeout"):
        ExperimentConfig.model_validate(data)


# --- identity guards (schema-evolution gate: additive + non-identity) -----------


def test_attempt_timeout_is_not_in_the_experiment_id_digest():
    base = ExperimentConfig.model_validate(BASE)
    timed = ExperimentConfig.model_validate(_with_timeouts())
    # A pure execution knob: the semantic digest (and thus experiment_id) is
    # unchanged whether or not attempt_timeout is set, on solvers AND on a grader.
    assert normalized_config_digest(timed) == normalized_config_digest(base)
    for stage in ("generate", "grade"):
        assert experiment_id(timed, stage) == experiment_id(base, stage)


def test_attempt_timeout_does_not_move_condition_ids():
    base = ExperimentConfig.model_validate(BASE)
    timed = ExperimentConfig.model_validate(_with_timeouts())
    solver_t = {"p": _template("A {input}", "p")}
    rubric_t = {"r1": _template("{input} {solution}", "r1")}
    assert [c.id for c in expand_generate_grid(timed, solver_t)] == [
        c.id for c in expand_generate_grid(base, solver_t)
    ]
    assert [c.id for c in expand_grade_grid(timed, rubric_t)] == [
        c.id for c in expand_grade_grid(base, rubric_t)
    ]


# --- pass-through into the per-condition GenerateConfig -------------------------


def test_build_generate_task_threads_attempt_timeout():
    tmpl = _template("Solve:\n{input}", "p")
    item = Item(id="p1", input="2+2?", target="4", grading_scheme=None, metadata={})
    cfg = ExperimentConfig.model_validate(_with_timeouts())
    cond = expand_generate_grid(cfg, {"p": tmpl})[0]

    class Origin:
        dataset_id = "org/ds"
        revision = "r"

    task = build_generate_task(
        [item], cond, tmpl, "s", 1, False, {"p1": Origin()}, attempt_timeout=120
    )
    assert task.config.attempt_timeout == 120
    # Default: no bound (today's behavior).
    untimed = build_generate_task([item], cond, tmpl, "s", 1, False, {"p1": Origin()})
    assert untimed.config.attempt_timeout is None


def test_build_judge_task_threads_attempt_timeout():
    rubric = _template("{input} {target} {solution}", "rubric")
    item = Item(id="p1", input="2+2?", target="4", grading_scheme=None, metadata={})
    cfg = ExperimentConfig.model_validate(_with_timeouts())
    cond = expand_grade_grid(cfg, {"r1": rubric})[0]
    pending = pd.DataFrame(
        [("genA", "p1", 1, "s1")], columns=["condition_id", "item_id", "epoch", "solution"]
    )
    task = build_judge_task(
        pending, {"p1": item}, cond, rubric, "study", cache=False, attempt_timeout=90
    )
    assert task.config.attempt_timeout == 90
    untimed = build_judge_task(pending, {"p1": item}, cond, rubric, "study", cache=False)
    assert untimed.config.attempt_timeout is None
