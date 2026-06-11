"""Judge-as-task: grading dataset built from stored solutions."""

from typing import TYPE_CHECKING

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, ChatMessageSystem, ChatMessageUser, GenerateConfig
from inspect_ai.solver import generate

from itemeval._cachegate import CACHE_GROUP_KEY, gated_generate
from itemeval._item import Item
from itemeval._templates import Template, render_template
from itemeval.design._grid import JUDGE_FORMAT_VERSION, GradeCondition

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "JUDGE_FORMAT_SUFFIX",
    "JUDGE_FORMAT_VERSION",
    "build_judge_input",
    "build_judge_messages",
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


def _clean_solution(solution: "str | None") -> str:
    # `solution` may be null/blank under the `grade` empty-solution policy
    # (judging an empty answer); pandas hands us a float NaN for null cells.
    return "" if solution is None or solution != solution else str(solution)


def _render_values(item: Item, solution: str) -> dict:
    return {
        "input": item.input,
        "solution": solution,
        "target": item.target,
        "grading_scheme": item.grading_scheme or "",
        "id": item.id,
    }


def build_judge_input(item: Item, solution: "str | None", rubric: Template) -> str:
    values = _render_values(item, _clean_solution(solution))
    return render_template(rubric.text, values) + JUDGE_FORMAT_SUFFIX


def build_judge_messages(
    item: Item, solution: "str | None", rubric: Template
) -> "list[ChatMessageSystem | ChatMessageUser]":
    """Split-rubric layout: shared head as system message, solution as user.

    The rubric template is split at its `{solution}` placeholder. Everything
    before it (rubric header + problem + grading scheme + reference — the
    content shared by every solution of the same item) renders into a system
    message, where providers with block-granular prompt caching (Anthropic)
    get an explicit cache breakpoint. The remainder, starting at the
    solution, renders into the user message. The concatenated text is
    byte-identical to `build_judge_input`, so token-prefix providers see the
    exact same prompt.
    """
    values = _render_values(item, _clean_solution(solution))
    idx = rubric.text.find("{solution}")
    if idx <= 0:  # no placeholder or nothing shared before it: single message
        return [ChatMessageUser(content=build_judge_input(item, solution, rubric))]
    head = render_template(rubric.text[:idx], values)
    tail = render_template(rubric.text[idx:], values) + JUDGE_FORMAT_SUFFIX
    return [ChatMessageSystem(content=head), ChatMessageUser(content=tail)]


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
    cache_schedule: bool = True,
) -> Task:
    # Same-item solutions share the longest cacheable prefix (rubric + problem
    # + scheme + reference). Sort so same-prefix calls are adjacent in the
    # schedule, and group them for warm-then-fan-out gating.
    pending = pending.sort_values(["item_id", "condition_id", "epoch"])
    samples = []
    for row in pending.itertuples():
        item = items_by_id[row.item_id]
        if cond.split_rubric:
            sample_input: "str | list" = build_judge_messages(item, row.solution, rubric)
        else:
            sample_input = build_judge_input(item, row.solution, rubric)
        samples.append(
            Sample(
                input=sample_input,
                target=item.target,
                id=judge_sample_id(row.condition_id, row.item_id, int(row.epoch)),
                metadata={
                    "gen_condition_id": row.condition_id,
                    "item_id": row.item_id,
                    "epoch": int(row.epoch),
                    "grade_condition_id": cond.id,
                    **({CACHE_GROUP_KEY: row.item_id} if cache_schedule else {}),
                },
            )
        )
    cache_policy = CachePolicy(expiry=None, per_epoch=True) if cache else False
    solver = (
        gated_generate(cache=cache_policy)
        if cache_schedule
        else (generate(cache=cache_policy) if cache else generate())
    )
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
