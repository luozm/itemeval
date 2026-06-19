"""Live API smoke — excluded from CI (marker ``live``).

A real, ~sub-cent generate->grade on the cheapest non-reasoning OpenAI models.
It exists because two things in the parallel-conditions path are unreachable
with ``mockllm`` and only fail against a real provider through inspect's eval:

  * the eval runs conditions *concurrently* only with >=2 distinct execution
    models (a single-model stage is serial), and
  * the regression that shipped (a model resolved to a bare ``str``, which lacks
    ``.model_args``) is raised inside inspect's task resolution, not our code.

So a successful run *is* the assertion: a bare-str model would crash the
concurrent eval and leave the conditions errored with no rows.

Two distinct non-reasoning models (gpt-4.1-nano + gpt-4o-mini) drive the
concurrent path; gpt-4.1-nano judges. Non-reasoning on purpose — a reasoning
model can spend a tiny ``max_tokens`` entirely on hidden reasoning and return
empty, flaking the assertions. Run via ``make test-live`` or the pre-push CC
hook; never in CI (no key there, and the marker is deselected).
"""

import os

import pytest

from conftest import write_study_files

pytestmark = pytest.mark.live

LIVE_CONFIG_YAML = """\
study: live_smoke
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
    metadata: [points]
solvers:
  models: [openai/gpt-4.1-nano, openai/gpt-4o-mini]
  temperature: 0.0
  max_tokens: 128
facets:
  prompt: [minimal]
  grader: [judge]
  rubric: [standard]
  replications: 1
graders:
  judge:
    model: openai/gpt-4.1-nano
    max_tokens: 256
budget:
  policy: dev
  dev_items: 1
  confirm_above_usd: 100
"""


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
def test_live_parallel_generate_then_grade(tmp_path, offline_adapter):
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study
    from itemeval.generate._run import run_generate
    from itemeval.grade._run import run_grade
    from itemeval.store._gradings import read_gradings

    cfg = load_config(write_study_files(tmp_path, LIVE_CONFIG_YAML))
    prep = prepare_study(cfg)

    # Generate: 2 distinct models x 1 item x 1 epoch -> 2 rows, run concurrently.
    # The shipped bug crashed exactly here for real (non-mock) models.
    gen = run_generate(prep)
    assert gen.rows_written == 2, gen.conditions
    assert {c.status for c in gen.conditions} == {"run"}, gen.conditions  # neither errored
    assert all(c.errors == 0 for c in gen.conditions), gen.conditions

    # Grade: the real judge scores both solutions; the structured score parses.
    grade = run_grade(prep)
    assert grade.rows_written == 2, grade.conditions
    assert grade.parse_failures == 0, grade.conditions
    assert read_gradings(prep.paths)["score"].notna().all()
