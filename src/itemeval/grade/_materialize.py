"""Materialization-as-task: a per-item rubric from the item's reference only.

Stage 1 of two-stage grading. The materializer LLM renders the build template
over `{input, target, grading_scheme, id}` — no candidate solution exists yet —
producing the frozen rubric stage 2 (the judge) reuses via `{rubric}`.
"""

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, GenerateConfig
from inspect_ai.solver import generate

from itemeval._config import MaterializeSpec
from itemeval._item import Item
from itemeval._templates import Template, render_template
from itemeval._util import sha256_hex

__all__ = ["materialize_id", "build_materialize_input", "build_materialize_task"]


def materialize_id(build_template: Template, model: str) -> str:
    """Content-addressed id for a (build template, materializer model) pair — the
    artifact-store key prefix and the `materialize.build_hash` in the condition id.
    A changed build template or materializer model yields a new id (re-derives)."""
    return sha256_hex(f"{build_template.sha256}:{model}".encode("utf-8"))[:12]


def _render_values(item: Item) -> dict:
    # Per-item metadata columns (mapping.metadata) are exposed to the build
    # template as {colname}; canonical fields below win on a name collision.
    values = {k: "" if v is None else str(v) for k, v in (item.metadata or {}).items()}
    values.update(
        {
            "input": item.input,
            "target": item.target,
            "grading_scheme": item.grading_scheme or "",
            "id": item.id,
        }
    )
    return values


def build_materialize_input(item: Item, build_template: Template) -> str:
    return render_template(build_template.text, _render_values(item))


def build_materialize_task(
    items: "list[Item]",
    build_template: Template,
    spec: MaterializeSpec,
    study: str,
    rubric_name: str,
    cache: bool,
    batch: "bool | int | None" = None,
) -> Task:
    samples = [
        Sample(
            input=build_materialize_input(it, build_template),
            id=it.id,
            metadata={"item_id": it.id},
        )
        for it in items
    ]
    cache_policy = CachePolicy(expiry=None, per_epoch=True) if cache else False
    config = GenerateConfig(
        temperature=0.0,  # frozen artifact: deterministic draw (like the judge)
        max_tokens=spec.max_tokens,
        reasoning_effort=spec.reasoning_effort,
        batch=batch,
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:materialize:{rubric_name}"),
        solver=generate(cache=cache_policy) if cache else generate(),
        scorer=None,  # rubric text harvested post-hoc; no scoring
        config=config,
        name=f"materialize_{rubric_name}",
        metadata={"itemeval": {"stage": "materialize", "study": study, "rubric": rubric_name}},
    )
