"""Judge-as-task: grading dataset built from stored solutions."""

from typing import TYPE_CHECKING

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, GenerateConfig
from inspect_ai.solver import generate

from itemeval._item import Item
from itemeval._templates import Template, render_template
from itemeval.design._grid import JUDGE_FORMAT_VERSION, GradeCondition

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "JUDGE_FORMAT_SUFFIX",
    "JUDGE_FORMAT_VERSION",
    "build_judge_input",
    "build_judge_task",
    "judge_sample_id",
]

# Appended to every rendered rubric; versioned via JUDGE_FORMAT_VERSION in
# the judge condition payload (design/_grid.py).
JUDGE_FORMAT_SUFFIX = (
    "\n\n---\n"
    "After your evaluation, output your final grade as a JSON object in a fenced\n"
    "code block, exactly in this form (score must be a number):\n"
    "```json\n"
    '{"score": <number>, "reasoning": "<one-paragraph justification>"}\n'
    "```\n"
    "The JSON code block must be the last thing in your response.\n"
)


def build_judge_input(item: Item, solution: str, rubric: Template) -> str:
    values = {
        "input": item.input,
        "solution": solution,
        "target": item.target,
        "grading_scheme": item.grading_scheme or "",
        "id": item.id,
    }
    return render_template(rubric.text, values) + JUDGE_FORMAT_SUFFIX


def judge_sample_id(gen_condition_id: str, item_id: str, epoch: int) -> str:
    return f"{gen_condition_id}::{item_id}::{epoch}"


def build_judge_task(
    pending: "pd.DataFrame",
    items_by_id: "dict[str, Item]",
    cond: GradeCondition,
    rubric: Template,
    study: str,
    cache: bool,
    batch: "bool | int | None" = None,
) -> Task:
    samples = []
    for row in pending.itertuples():
        item = items_by_id[row.item_id]
        samples.append(
            Sample(
                input=build_judge_input(item, row.solution, rubric),
                target=item.target,
                id=judge_sample_id(row.condition_id, row.item_id, int(row.epoch)),
                metadata={
                    "gen_condition_id": row.condition_id,
                    "item_id": row.item_id,
                    "epoch": int(row.epoch),
                    "grade_condition_id": cond.id,
                },
            )
        )
    solver = generate(cache=CachePolicy(expiry=None, per_epoch=True)) if cache else generate()
    config = GenerateConfig(
        temperature=0.0,  # judge temperature pinned for v0.1 (ROADMAP M3)
        max_tokens=cond.grader_max_tokens,
        reasoning_effort=cond.grader_reasoning_effort,
        cache_prompt="auto",  # prompt caching on repeated rubric+problem prefixes
        batch=batch,
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:{cond.id}"),
        solver=solver,
        scorer=None,  # scores parsed post-hoc (grade/_parse.py)
        config=config,
        name=f"judge_{cond.slug}",
        metadata={"itemeval": {"stage": "grade", "study": study, "condition_id": cond.id}},
    )
