"""Judge-as-task: grading dataset built from stored solutions."""

from typing import TYPE_CHECKING

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, ChatMessageSystem, ChatMessageUser, GenerateConfig
from inspect_ai.solver import generate

from itemeval._cachegate import CACHE_GROUP_KEY, gated_generate
from itemeval._endpoints import resolve_max_retries
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
    "judge_head_text",
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


def _render_values(item: Item, solution: str, rubric_text: "str | None" = None) -> dict:
    values = {
        "input": item.input,
        "solution": solution,
        "target": item.target,
        "grading_scheme": item.grading_scheme or "",
        "id": item.id,
    }
    # Two-stage materialization: fill {rubric} with the frozen per-item rubric.
    # None means a plain (non-materializing) rubric, which has no {rubric}.
    if rubric_text is not None:
        values["rubric"] = rubric_text
    return values


def build_judge_input(
    item: Item, solution: "str | None", rubric: Template, rubric_text: "str | None" = None
) -> str:
    values = _render_values(item, _clean_solution(solution), rubric_text)
    return render_template(rubric.text, values) + JUDGE_FORMAT_SUFFIX


def build_judge_messages(
    item: Item, solution: "str | None", rubric: Template, rubric_text: "str | None" = None
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
    values = _render_values(item, _clean_solution(solution), rubric_text)
    idx = rubric.text.find("{solution}")
    if idx <= 0:  # no placeholder or nothing shared before it: single message
        return [ChatMessageUser(content=build_judge_input(item, solution, rubric, rubric_text))]
    head = render_template(rubric.text[:idx], values)
    tail = render_template(rubric.text[idx:], values) + JUDGE_FORMAT_SUFFIX
    return [ChatMessageSystem(content=head), ChatMessageUser(content=tail)]


def judge_head_text(item: Item, rubric: Template, rubric_text: "str | None" = None) -> "str | None":
    """The split-rubric layout's shared head for `item` (rendered rubric text
    before {solution}) — what same-item judge calls share as a cache prefix.
    None when the rubric has nothing before {solution} (build_judge_messages
    falls back to a single message there). The materialized {rubric} is
    solution-independent, so it renders into this shared head. Used by the
    estimator for the min-cacheable-prefix check and the cache projection."""
    idx = rubric.text.find("{solution}")
    if idx <= 0:
        return None
    return render_template(rubric.text[:idx], _render_values(item, "", rubric_text))


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
    rubric_texts: "dict[str, str] | None" = None,
    attempt_timeout: "int | None" = None,
    max_retries: "int | None" = None,
) -> Task:
    # Same-item solutions share the longest cacheable prefix (rubric + problem
    # + scheme + reference). Sort so same-prefix calls are adjacent in the
    # schedule, and group them for warm-then-fan-out gating.
    pending = pending.sort_values(["item_id", "condition_id", "epoch"])
    samples = []
    for row in pending.itertuples():
        item = items_by_id[row.item_id]
        # Frozen per-item rubric for a materializing condition ({rubric}); None
        # for plain rubrics (the template has no {rubric} placeholder).
        rubric_text = rubric_texts.get(row.item_id) if rubric_texts is not None else None
        if cond.split_rubric:
            sample_input: "str | list" = build_judge_messages(
                item, row.solution, rubric, rubric_text
            )
        else:
            sample_input = build_judge_input(item, row.solution, rubric, rubric_text)
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
        attempt_timeout=attempt_timeout,  # None -> inspect default (unbounded)
        # Bound the timeout/transient retry so a stalled judge call can't loop
        # forever (None unless attempt_timeout is set; resolve_max_retries).
        max_retries=resolve_max_retries(attempt_timeout, max_retries),
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:{cond.id}"),
        solver=solver,
        scorer=None,  # scores parsed post-hoc (grade/_parse.py)
        config=config,
        name=f"judge_{cond.slug}",
        metadata={"itemeval": {"stage": "grade", "study": study, "condition_id": cond.id}},
    )
