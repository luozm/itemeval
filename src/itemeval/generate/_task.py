"""inspect task builder for one generate condition."""

from typing import TYPE_CHECKING

from inspect_ai import Epochs, Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, ChatMessageSystem, ChatMessageUser, GenerateConfig
from inspect_ai.solver import generate

from itemeval._cachegate import CACHE_GROUP_KEY, gated_generate
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
    cache_prompt: "bool | None" = None,
    cache_schedule: bool = True,
    epoch_offset: int = 0,
) -> Task:
    # Replications send byte-identical prompts: every epoch of an item shares
    # the full prompt as a provider cache prefix. Gate them (warm-then-fan-out)
    # only when there is something to share (replications > 1).
    gate = cache_schedule and replications > 1
    # Cache-group key: normally per item (epochs share the full prompt). With
    # split_prompt the cacheable prefix is the rendered template head, which is
    # condition-constant unless it interpolates {id} — group accordingly so
    # parallel leaders don't duplicate the same provider cache write.
    head_is_static = cond.split_prompt and "{id}" not in template.text.split("{input}")[0]

    def group_key(item: Item) -> str:
        return cond.id if head_is_static else item.id

    def render_input(item: Item) -> "str | list":
        values = {"input": item.input, "id": item.id}
        if cond.split_prompt:
            # Static template head -> system message (provider cache breakpoint
            # lands there); remainder starting at the item -> user message.
            idx = template.text.find("{input}")
            if idx > 0:
                head = render_template(template.text[:idx], values)
                tail = render_template(template.text[idx:], values)
                return [ChatMessageSystem(content=head), ChatMessageUser(content=tail)]
        return render_template(template.text, values)

    samples = [
        Sample(
            input=render_input(item),
            target=item.target,
            id=item.id,
            metadata={
                "item_id": item.id,
                "dataset_id": origins[item.id].dataset_id,
                "dataset_revision": origins[item.id].revision,
                "condition_id": cond.id,
                **({CACHE_GROUP_KEY: group_key(item)} if gate else {}),
            },
        )
        for item in items
    ]
    cache_policy: "CachePolicy | bool" = (
        CachePolicy(expiry=None, per_epoch=True) if cache else False
    )
    if epoch_offset > 0:
        # Offset evals run epochs 1..N internally; the local response cache
        # would key them as such and silently REPLAY the wave-0 draws as "new"
        # observations. Re-observations must be fresh draws — cache off.
        cache_policy = False
    solver = gated_generate(cache=cache_policy) if gate else generate(cache=cache_policy)
    p = cond.gen_params
    config = GenerateConfig(
        temperature=p.temperature,
        top_p=p.top_p,
        max_tokens=p.max_tokens,
        seed=p.seed,
        reasoning_effort=p.reasoning_effort,
        reasoning_tokens=p.reasoning_tokens,
        cache_prompt=cache_prompt,  # None -> provider default
        batch=batch,
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:{cond.id}"),
        solver=solver,
        scorer=None,
        config=config,
        epochs=Epochs(replications),
        name=f"gen_{cond.slug}",
        metadata={
            "itemeval": {
                "stage": "generate",
                "study": study,
                "condition_id": cond.id,
                "epoch_offset": epoch_offset,
            }
        },
    )
