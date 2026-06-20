"""inspect task builder for one generate condition."""

from typing import TYPE_CHECKING

from inspect_ai import Epochs, Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import CachePolicy, ChatMessageSystem, ChatMessageUser, GenerateConfig
from inspect_ai.solver import generate

from itemeval._cachegate import CACHE_GROUP_KEY, gated_generate
from itemeval._endpoints import resolve_max_retries
from itemeval._item import Item
from itemeval._templates import Template, render_template
from itemeval.design._grid import GenCondition

if TYPE_CHECKING:
    from itemeval._prepare import DatasetOrigin


def render_generate_input(item: Item, cond: GenCondition, template: Template) -> "str | list":
    """The exact `Sample.input` a generate condition sends for one item — a plain
    rendered string, or (split_prompt) a system(head)+user(item) message pair.
    Shared by the task builder and the cache probe so the two never drift (the
    probe must reconstruct byte-identical messages to predict a response-cache hit).
    """
    values = {"input": item.input, "id": item.id}
    if cond.split_prompt:
        # Static template head -> system message (provider cache breakpoint lands
        # there); remainder starting at the item -> user message.
        idx = template.text.find("{input}")
        if idx > 0:
            head = render_template(template.text[:idx], values)
            tail = render_template(template.text[idx:], values)
            return [ChatMessageSystem(content=head), ChatMessageUser(content=tail)]
    return render_template(template.text, values)


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
    max_tokens_override: "int | None" = None,
    attempt_timeout: "int | None" = None,
    max_retries: "int | None" = None,
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

    samples = [
        Sample(
            input=render_generate_input(item, cond, template),
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
    # max_tokens_override: a runtime clamp to fit the model's context window
    # (see generate/_params.fit_max_tokens). None -> use the requested design
    # value. The condition id is keyed on cond.gen_params, so the clamp never
    # moves the id — only what the provider is actually asked for.
    config = GenerateConfig(
        temperature=p.temperature,
        top_p=p.top_p,
        max_tokens=p.max_tokens if max_tokens_override is None else max_tokens_override,
        seed=p.seed,
        reasoning_effort=p.reasoning_effort,
        reasoning_tokens=p.reasoning_tokens,
        cache_prompt=cache_prompt,  # None -> provider default
        batch=batch,
        attempt_timeout=attempt_timeout,  # None -> inspect default (unbounded)
        # Bound the timeout/transient retry so a stalled attempt can't loop forever
        # (None unless attempt_timeout is set; see _endpoints.resolve_max_retries).
        max_retries=resolve_max_retries(attempt_timeout, max_retries),
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


def build_reroute_task(
    cells: "list[tuple[Item, int]]",
    cond: GenCondition,
    template: Template,
    study: str,
    origins: "dict[str, DatasetOrigin]",
    max_tokens_override: "int | None" = None,
    attempt_timeout: "int | None" = None,
    max_retries: "int | None" = None,
) -> Task:
    """A one-shot generate task re-issuing specific (item, target_epoch) cells on a
    fresh backend (output-validity-reroute). One sample per cell with `epochs=1`,
    each carrying `reroute_epoch` in metadata so the harvest writes the *original*
    epoch — overwriting the soft-failed row in place rather than at a positional
    epoch. Cache is OFF: inspect's response-cache key does not vary on the
    `provider` routing object, so a cached bad response would otherwise replay
    instead of hitting a different backend. The model (with the failed provider
    excluded) is set by the caller via `task.model`."""
    samples = [
        Sample(
            input=render_generate_input(item, cond, template),
            target=item.target,
            id=f"{item.id}#e{epoch}",  # unique per cell (an item may have >1 bad epoch)
            metadata={
                "item_id": item.id,
                "reroute_epoch": epoch,
                "dataset_id": origins[item.id].dataset_id,
                "dataset_revision": origins[item.id].revision,
                "condition_id": cond.id,
            },
        )
        for item, epoch in cells
    ]
    p = cond.gen_params
    config = GenerateConfig(
        temperature=p.temperature,
        top_p=p.top_p,
        max_tokens=p.max_tokens if max_tokens_override is None else max_tokens_override,
        seed=p.seed,
        reasoning_effort=p.reasoning_effort,
        reasoning_tokens=p.reasoning_tokens,
        cache_prompt=None,
        batch=None,
        attempt_timeout=attempt_timeout,
        max_retries=resolve_max_retries(attempt_timeout, max_retries),
    )
    return Task(
        dataset=MemoryDataset(samples, name=f"{study}:{cond.id}:reroute"),
        solver=generate(cache=False),
        scorer=None,
        config=config,
        epochs=Epochs(1),
        name=f"reroute_{cond.slug}",
        metadata={
            "itemeval": {
                "stage": "generate",
                "study": study,
                "condition_id": cond.id,
                "epoch_offset": 0,
            }
        },
    )
