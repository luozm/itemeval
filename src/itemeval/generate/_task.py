"""inspect task builder for one generate condition."""

from typing import TYPE_CHECKING

from inspect_ai import Epochs, Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, GenerateConfig
from inspect_ai.solver import generate

from itemeval._item import Item
from itemeval._templates import Template, render_template
from itemeval.design._grid import GenCondition

if TYPE_CHECKING:
    from itemeval._prepare import DatasetOrigin


def build_generate_task(
    items: "list[Item]",
    cond: GenCondition,
    template: Template,
    study: str,
    replications: int,
    cache: bool,
    origins: "dict[str, DatasetOrigin]",
    batch: "bool | int | None" = None,
) -> Task:
    samples = [
        Sample(
            input=render_template(template.text, {"input": item.input, "id": item.id}),
            target=item.target,
            id=item.id,
            metadata={
                "item_id": item.id,
                "dataset_id": origins[item.id].dataset_id,
                "dataset_revision": origins[item.id].revision,
                "condition_id": cond.id,
            },
        )
        for item in items
    ]
    solver = generate(cache=CachePolicy(expiry=None, per_epoch=True)) if cache else generate()
    p = cond.gen_params
    config = GenerateConfig(
        temperature=p.temperature,
        top_p=p.top_p,
        max_tokens=p.max_tokens,
        seed=p.seed,
        reasoning_effort=p.reasoning_effort,
        reasoning_tokens=p.reasoning_tokens,
        batch=batch,
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:{cond.id}"),
        solver=solver,
        scorer=None,
        config=config,
        epochs=Epochs(replications),
        name=f"gen_{cond.slug}",
        metadata={"itemeval": {"stage": "generate", "study": study, "condition_id": cond.id}},
    )
