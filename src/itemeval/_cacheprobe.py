"""Pre-flight local-response-cache hit projection (cache-projection).

Before the money gate, predict which of a run's planned calls inspect will serve
from its **local response cache** ($0) — so a recovery / `--force` / replication-
bump re-run isn't over-stated (a call missing from the *store* may still replay
from the *response cache*: inspect writes that cache inside `Model.generate`, right
after the API returns, before the sample reaches the `.eval`/store).

Mechanism: reconstruct the **identical `CacheEntry`** inspect builds
(`_model.py` → `CacheEntry(base_url, config, input, model, policy, tool_choice,
tools)`) and test whether its key file exists under `cache_path(model)`. itemeval
always caches with `CachePolicy(expiry=None, per_epoch=True)`, and `expiry=None`
entries never expire, so a hit is exactly `(cache_path(model)/key).exists()`. We
reuse inspect's own `CacheEntry`/`cache_path`/`_cache_key` (wrap, don't fork)
rather than re-deriving the md5; `tests/test_cacheprobe.py` round-trips a real
write through the probe to pin this to the installed inspect (an inspect
`_cache_key` change goes red, not silently wrong).

Boundary: inspect imports are **lazy** (inside functions) so importing this module
— and the otherwise engine-free estimate path that calls it — never pays inspect's
import cost. The probe runs only when `config.cache` is on and there are fresh
(non-store-skipped) calls to check.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from itemeval._prepare import PreparedStudy


class CacheProbe(BaseModel):
    """Per-stage local-response-cache projection over a run's remaining calls."""

    model_config = ConfigDict(extra="forbid")

    cache_hits: int = 0  # remaining calls already in the local response cache ($0)
    cache_misses: int = 0  # remaining calls that will be paid fresh
    cache_dir: "str | None" = None  # the response-cache dir probed (None if not probed)


def _cache_is_cold() -> bool:
    """No response-cache entries exist yet — so every call is a guaranteed miss.
    Cheap early-out that avoids resolving models / building messages on a cold
    cache (the first-run common case), and sidesteps a keyless-model resolution."""
    from pathlib import Path

    from inspect_ai.model._cache import cache_path

    root = Path(cache_path())
    return not root.exists() or not any(p.is_file() for p in root.rglob("*"))


def _as_messages(rendered: "str | list") -> list:
    """The message list inspect's Model.generate receives for a Sample.input.

    inspect tags every message built from a sample's input with `source="input"`
    (a bare string becomes one such user message) — that field is in the cache key,
    so the probe must set it too or every call reads as a miss."""
    from inspect_ai.model import ChatMessageUser

    msgs = rendered if isinstance(rendered, list) else [ChatMessageUser(content=rendered)]
    return [m.model_copy(update={"source": "input"}) for m in msgs]


def _key_exists(model_obj, messages: list, config, epoch_num: int) -> bool:
    """Whether the response-cache file for this exact call already exists."""
    from inspect_ai.model._cache import CacheEntry, CachePolicy, cache_path
    from inspect_ai.model._cache import epoch as epoch_var

    # _cache_key reads the epoch ContextVar when policy.per_epoch — inspect sets it
    # from the sample's (1-based) epoch during a run; mirror that here.
    epoch_var.set(epoch_num)
    entry = CacheEntry(
        base_url=model_obj.api.base_url,
        config=config,
        input=messages,
        model=str(model_obj),
        policy=CachePolicy(expiry=None, per_epoch=True),
        # inspect's generate() solver sends tool_choice="none" (a string, in the
        # key) and an empty tools list when the task has no tools — match both.
        tool_choice="none",
        tools=[],
    )
    return (cache_path(str(model_obj)) / entry.key).exists()


def _gen_config(prep: "PreparedStudy", cond) -> object:
    """The GenerateConfig a generate condition sends — the cache-key-bearing fields
    must match `generate/_task.build_generate_task` (batch/cache are excluded from
    the key, so they are omitted). max_tokens uses the requested design value: the
    runtime context clamp is not reproduced, so a clamped small-context model is
    conservatively counted fresh (never a false $0)."""
    from inspect_ai.model import GenerateConfig

    from itemeval.generate._params import resolve_cache_prompt

    # Same resolution as generate/_run (shared helper, design reps) so the probe's
    # reconstructed key matches the call's — a dev-pilot epoch the full run replays
    # must read as a hit, not a miss.
    cache_prompt = resolve_cache_prompt(
        prep.config.solvers.cache_prompt, prep.config.facets.replications
    )
    p = cond.gen_params
    return GenerateConfig(
        temperature=p.temperature,
        top_p=p.top_p,
        max_tokens=p.max_tokens,
        seed=p.seed,
        reasoning_effort=p.reasoning_effort,
        reasoning_tokens=p.reasoning_tokens,
        cache_prompt=cache_prompt,
        attempt_timeout=prep.config.solvers.attempt_timeout,
    )


def probe_generate(prep: "PreparedStudy", *, force: bool = False) -> CacheProbe:
    """Project the local-response-cache hits over `generate`'s remaining calls.

    Empty (no probe) when `config.cache` is off. Counts, per remaining
    (condition, item, epoch), whether the identical call is already cached."""
    if not prep.config.cache or _cache_is_cold():
        return CacheProbe()
    from inspect_ai.model._cache import cache_path

    from itemeval._endpoints import model_args_for
    from itemeval._mockmodels import resolve_model
    from itemeval.generate._task import render_generate_input
    from itemeval.store._solutions import epochs_to_run, read_solutions

    store = read_solutions(prep.paths)
    reps = prep.plan.replications
    epoch_range = (1, reps)
    item_ids = [it.id for it in prep.items_effective]
    items_by_id = {it.id: it for it in prep.items_effective}
    require_solution = prep.config.solvers.on_empty == "rerun"
    cache_scheduling = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None

    hits = misses = 0
    model_cache: dict = {}
    for cond in prep.grid.generate:
        exec_id = prep.native_routes.get(cond.model, cond.model)
        if exec_id not in model_cache:
            model_cache[exec_id] = resolve_model(
                exec_id,
                "generate",
                model_args_for(
                    exec_id,
                    provider_routing=prep.config.solvers.provider_routing,
                    cache_scheduling=cache_scheduling,
                    study=prep.config.study,
                    condition_id=cond.id,
                ),
            )
        model_obj = model_cache[exec_id]
        config = _gen_config(prep, cond)
        template = prep.solver_templates[cond.prompt_name]
        if force:
            per_item = {iid: set(range(1, reps + 1)) for iid in item_ids}
        else:
            per_item = epochs_to_run(
                store, cond.id, item_ids, epoch_range, require_solution=require_solution
            )
        for iid, epochs in per_item.items():
            if not epochs:
                continue
            messages = _as_messages(render_generate_input(items_by_id[iid], cond, template))
            for epoch_num in epochs:
                if _key_exists(model_obj, messages, config, epoch_num):
                    hits += 1
                else:
                    misses += 1
    return CacheProbe(cache_hits=hits, cache_misses=misses, cache_dir=str(cache_path()))


def _grade_config(prep: "PreparedStudy", cond) -> object:
    """The judge GenerateConfig (cache-key-bearing fields must match
    `grade/_judge.build_judge_task`)."""
    from inspect_ai.model import GenerateConfig

    return GenerateConfig(
        temperature=0.0,
        max_tokens=cond.grader_max_tokens,
        reasoning_effort=cond.grader_reasoning_effort,
        cache_prompt="auto",
        attempt_timeout=prep.config.grader_spec(cond.grader_name).attempt_timeout,
    )


def probe_grade(prep: "PreparedStudy", *, force: bool = False) -> CacheProbe:
    """Project the local-response-cache hits over `grade`'s remaining judge calls.

    Verifiable conditions make no model call (skipped). A **materializing** rubric
    needs its frozen per-item text to rebuild the judge prompt; reconstructing that
    is out of this probe's scope, so a materializing condition's calls are
    conservatively counted **fresh** (never a false $0). The judge runs a single
    epoch, so the cache-key epoch is always 1."""
    if not prep.config.cache or _cache_is_cold():
        return CacheProbe()
    from inspect_ai.model._cache import cache_path

    from itemeval._endpoints import model_args_for
    from itemeval._mockmodels import resolve_model
    from itemeval.grade._judge import build_judge_input, build_judge_messages
    from itemeval.store._gradings import pending_solutions, read_gradings
    from itemeval.store._solutions import read_solutions

    solutions = read_solutions(prep.paths)
    gradings = read_gradings(prep.paths)
    if solutions.empty:
        return CacheProbe(cache_hits=0, cache_misses=0, cache_dir=str(cache_path()))
    effective_ids = {it.id for it in prep.items_effective}
    grid_gen_ids = {c.id for c in prep.grid.generate}
    scoped = solutions[
        solutions["item_id"].isin(effective_ids)
        & (solutions["epoch"].astype(int) <= prep.plan.replications)
        & solutions["condition_id"].isin(grid_gen_ids)
    ]
    include_empty = prep.config.solvers.on_empty == "grade"
    cache_scheduling = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None

    hits = misses = 0
    model_cache: dict = {}
    for cond in prep.grid.grade:
        if cond.kind == "verifiable":
            continue  # no model call
        pending = pending_solutions(scoped, gradings, cond.id, force, include_empty=include_empty)
        if pending.empty:
            continue
        config = _grade_config(prep, cond)
        rubric = prep.rubric_templates[cond.rubric_name]
        materializing = cond.materialize_model is not None
        exec_id = prep.native_routes.get(cond.grader_model, cond.grader_model)
        if exec_id not in model_cache:
            model_cache[exec_id] = resolve_model(
                exec_id,
                "grade",
                model_args_for(
                    exec_id,
                    provider_routing=prep.config.grader_spec(cond.grader_name).provider_routing,
                    cache_scheduling=cache_scheduling,
                    study=prep.config.study,
                    condition_id=cond.id,
                ),
            )
        model_obj = model_cache[exec_id]
        for row in pending.itertuples():
            if materializing:  # frozen per-item rubric not reconstructed — conservative miss
                misses += 1
                continue
            item = prep.items_by_id[row.item_id]
            rendered = (
                build_judge_messages(item, row.solution, rubric, None)
                if cond.split_rubric
                else build_judge_input(item, row.solution, rubric, None)
            )
            if _key_exists(model_obj, _as_messages(rendered), config, 1):
                hits += 1
            else:
                misses += 1
    return CacheProbe(cache_hits=hits, cache_misses=misses, cache_dir=str(cache_path()))


def probe_stage(prep: "PreparedStudy", stage: str, *, force: bool = False) -> CacheProbe:
    """Response-cache projection for one stage (`generate`/`grade`).

    Defensive: a projection must never break the estimate. Any failure (e.g. a
    model that can't be resolved without an API key) yields an empty probe — the
    estimate then simply shows no cache projection. (The guard test calls the
    per-stage probes directly, so reconstruction correctness is still pinned.)"""
    try:
        if stage == "generate":
            return probe_generate(prep, force=force)
        return probe_grade(prep, force=force)
    except Exception:  # noqa: BLE001 — informational projection, never fatal
        return CacheProbe()
