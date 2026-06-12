"""Dry-run cost projection: token heuristics x policy-effective grid."""

from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from itemeval._endpoints import min_cacheable_prefix, routing_warnings
from itemeval._hints import Hint, detect_split_head_below_min, detect_unpriced_models
from itemeval._templates import render_template
from itemeval.adapters._base import DatasetProvenance, dataset_provenance
from itemeval._util import estimate_tokens
from itemeval.budget._pricing import (
    BATCH_PROVIDERS,
    PricingProvenance,
    anthropic_style_caching,
    cost_usd,
    describe_pricing,
    lookup_price,
    provider_of,
)
from itemeval.grade._judge import build_judge_input, judge_head_text

if TYPE_CHECKING:
    from itemeval._prepare import PreparedStudy

# Output-token fill-ins when no max_tokens cap is configured. Generation is
# deliberately pessimistic: an uncapped run can emit far more than a typical
# completion, and the gate must not be driven by an under-estimate.
DEFAULT_OUTPUT_TOKENS_GENERATE = 4096
DEFAULT_OUTPUT_TOKENS_JUDGE = 512


class ConditionEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str
    slug: str
    stage: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    usd: float | None  # discounted projection when a cache split applies
    priced: bool
    batch_discount: bool
    # Projected provider prompt-cache split (append-only; 0 when caching is
    # not projected — see the eligibility predicates in estimate_study).
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0  # Anthropic-style write tokens; 0 for free-write providers
    cache_discount_usd: float = 0.0  # undiscounted minus usd; negative = write surcharge net loss


class StageEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    usd: float  # unpriced conditions contribute 0; ALWAYS the full grid
    unpriced_models: list[str]
    conditions: list[ConditionEstimate]
    # Delta-aware figures (append-only; `usd` keeps meaning the full grid).
    # `remaining_usd` is what this run can actually spend — the gate operates
    # on it; completed cells are never re-paid. With force=True it equals full.
    full_usd: float = 0.0  # == usd, named for clarity alongside remaining_usd
    remaining_usd: float = 0.0
    remaining_calls: int = 0
    completed_cells: int = 0  # completed (condition x item x epoch) cells in scope
    total_cells: int = 0
    rows_replaced: int = 0  # existing rows the planned run would overwrite
    # Stage-relevant subset of Estimate.warnings (append-only): what generate/
    # grade relay pre-gate without cross-stage noise.
    warnings: list[str] = Field(default_factory=list)
    # Stage-relevant estimate-time hints (append-only); generate/grade merge
    # them into the run's hints so they surface on all three commands.
    hints: list[Hint] = Field(default_factory=list)
    # Projected provider prompt-cache split, summed over conditions (full
    # grid); remaining_cache_discount_usd is the discount inside
    # remaining_usd — the figure the projection line and the gate see.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_discount_usd: float = 0.0
    remaining_cache_discount_usd: float = 0.0


class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str
    policy: str  # effective policy these projections cover
    policy_source: str = "config"  # "config" | "override"
    generate: StageEstimate
    grade: StageEstimate
    total_usd: float
    warnings: list[str]
    pricing: PricingProvenance  # which prices these projections used
    hints: list[Hint] = Field(default_factory=list)
    datasets: list[DatasetProvenance] = Field(default_factory=list)


def _batch_discount(prep: "PreparedStudy", model: str) -> bool:
    return prep.plan.batch is not None and provider_of(model) in BATCH_PROVIDERS


def _priced_usd(prep: "PreparedStudy", model: str, input_tokens: int, output_tokens: int) -> float:
    """Projected USD for a call volume; 0 when the model is unpriced."""
    price = lookup_price(prep.pricing, model)
    if price is None:
        return 0.0
    discount = 0.5 if _batch_discount(prep, model) else 1.0
    return cost_usd(price, input_tokens, output_tokens) * discount


def _discounted_usd(
    prep: "PreparedStudy", model: str, input_tokens: int, output_tokens: int, read: int, write: int
) -> float:
    """Cache-split projection: read tokens at the cache-read rate, write tokens
    at the provider's write rate (1.25x Anthropic-style, plain input elsewhere
    — those pass write=0), the rest at list. Only called for non-batch plans
    (batch and cache discounts never combine)."""
    price = lookup_price(prep.pricing, model)
    if price is None:
        return 0.0
    return cost_usd(price, input_tokens - read - write, output_tokens, read, write, model=model)


def _cache_split(
    groups: "list[tuple[int, int, bool]]", min_tok: int, anthropic_style: bool
) -> "tuple[int, int]":
    """(cache_read_tokens, cache_write_tokens) over cache groups.

    Each group is (calls, shared_prefix_tokens, warm). A cold group's leader
    writes the shared prefix (billed only Anthropic-style — token-prefix
    providers' writes are free, i.e. plain input) and the followers read it;
    a warm group (≥1 completed row exists, so the leader already ran) is
    followers-only. Prefixes below the provider minimum never engage.
    """
    read = write = 0
    for calls, shared, warm in groups:
        if calls <= 0 or shared < min_tok:
            continue
        if warm:
            read += calls * shared
        else:
            read += (calls - 1) * shared
            if anthropic_style:
                write += shared
    return read, write


def _condition_estimate(
    prep: "PreparedStudy",
    stage: str,
    condition_id: str,
    slug: str,
    model: str,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> ConditionEstimate:
    price = lookup_price(prep.pricing, model)
    discount = _batch_discount(prep, model)
    usd = None
    cache_discount = 0.0
    if price is not None:
        usd = cost_usd(price, input_tokens, output_tokens) * (0.5 if discount else 1.0)
        if cache_read or cache_write:
            # Batch and cache discounts never combine: caching is only
            # projected for non-batch plans, so the 0.5 factor is 1 here.
            base = usd
            usd = cost_usd(
                price,
                input_tokens - cache_read - cache_write,
                output_tokens,
                cache_read,
                cache_write,
                model=model,
            )
            cache_discount = base - usd
    return ConditionEstimate(
        condition_id=condition_id,
        slug=slug,
        stage=stage,
        model=model,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=usd,
        priced=price is not None,
        batch_discount=discount,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_discount_usd=cache_discount,
    )


def estimate_study(
    prep: "PreparedStudy",
    solutions_df: "pd.DataFrame | None" = None,
    gradings_df: "pd.DataFrame | None" = None,
    *,
    force: bool = False,
    wave: "str | None" = None,
) -> Estimate:
    """Project the policy-effective grid: full figures plus the remaining delta.

    `usd`/`full_usd` always cover the full grid; `remaining_usd` subtracts
    already-complete cells using the same predicates the runners use
    (`epochs_to_run` for generate, `pending_solutions` for grade), so the gate
    can operate on what this run will actually spend. `force=True` makes
    remaining equal full (everything selected re-runs). With `wave`, the delta
    covers that wave's epoch block (1.3's remaining logic within the block).
    When solutions_df is None, the study's solutions store is read so judge
    input sizing uses real stored solutions where they exist.

    When the run would be scheduled into provider prompt caches (cache
    scheduling on, not batch, provider minimum known and met), `usd` and
    `remaining_usd` are the *discounted* projections — the same per-group
    leader-writes/followers-read split the runtime schedules — so the money
    gate reflects what the run should actually cost. Best-case projection
    (assumes scheduled hits); the post-run `cache-zero-reads` hint is the
    corrective feedback loop.
    """
    from itemeval.store._gradings import pending_solutions, read_gradings
    from itemeval.store._solutions import epochs_to_run, read_solutions, resolve_wave

    if solutions_df is None:
        solutions_df = read_solutions(prep.paths)
    if "error" not in solutions_df.columns:  # tolerate minimal caller-built frames
        solutions_df = solutions_df.assign(error=None)
    if gradings_df is None:
        gradings_df = read_gradings(prep.paths)
    items = prep.items_effective
    reps = prep.plan.replications
    item_ids = [it.id for it in items]
    effective_ids = set(item_ids)
    rerun_empty = prep.config.solvers.on_empty == "rerun"
    include_empty = prep.config.solvers.on_empty == "grade"
    gen_warnings, grade_warnings = routing_warnings(prep.config)

    # split-head-below-min detection (W4): shared heads of split layouts whose
    # chars/4 estimate falls below the provider's minimum cacheable prefix,
    # aggregated per (stage, model, minimum) — one hint line per run, not per
    # condition. Only providers with a known minimum are checked (never guess).
    head_stats: "dict[tuple[str, str, int], dict]" = {}

    def note_heads(stage: str, model: str, min_tok: int, head_tokens: "list[int]") -> None:
        if not head_tokens:
            return
        s = head_stats.setdefault((stage, model, min_tok), {"below": 0, "total": 0, "head": None})
        s["below"] += sum(1 for t in head_tokens if t < min_tok)
        s["total"] += len(head_tokens)
        s["head"] = head_tokens[0]

    def stage_hints(stage: str) -> "list[Hint]":
        out = []
        for (s, model, min_tok), v in head_stats.items():
            if s != stage:
                continue
            h = detect_split_head_below_min(
                stage=s,
                heads_below=v["below"],
                heads_total=v["total"],
                min_tokens=min_tok,
                model=model,
                head_tokens=v["head"],
            )
            if h is not None:
                out.append(h)
        return out

    if wave is not None:
        _, offset = resolve_wave(solutions_df, wave, reps)
    else:
        offset = 0
    epoch_lo, epoch_hi = offset + 1, offset + reps

    # The runners' scope: effective items, epochs within the run's block
    # (1..reps normally; the wave's block under --wave).
    scoped = solutions_df
    if not solutions_df.empty:
        epochs = solutions_df["epoch"].astype(int)
        scoped = solutions_df[
            solutions_df["item_id"].isin(effective_ids)
            & (epochs >= epoch_lo)
            & (epochs <= epoch_hi)
        ]

    # One truth value for "cache scheduling active" — the same predicate the
    # runners and task builders use (batch reorders calls; never both).
    scheduling = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None

    gen_conditions = []
    gen_delta = {"usd": 0.0, "calls": 0, "completed": 0, "total": 0, "replaced": 0, "discount": 0.0}
    for cond in prep.grid.generate:
        template = prep.solver_templates[cond.prompt_name]

        min_tok = min_cacheable_prefix(cond.model)
        idx = template.text.find("{input}")
        if cond.split_prompt and min_tok is not None and idx > 0:
            head_text = template.text[:idx]
            if "{id}" in head_text:  # per-item head (mirrors generate/_task.py)
                heads = [estimate_tokens(render_template(head_text, {"id": it.id})) for it in items]
            else:  # static head: one shared prefix for the whole condition
                heads = [estimate_tokens(head_text)]
            note_heads("generate", cond.model, min_tok, heads)

        def item_tokens(it, template=template):
            return estimate_tokens(render_template(template.text, {"input": it.input, "id": it.id}))

        def in_tokens(subset, item_tokens=item_tokens):
            return sum(item_tokens(it) for it in subset) * reps

        # Cache projection (W3) — mirror the runtime gating exactly, no more:
        # scheduling on, not batch, the task builder gates only with >1 epoch
        # (generate/_task.py), the provider has a known minimum, and Anthropic-
        # style markers are not switched off. Groups mirror the task builder's
        # group keys: condition when a split head is static, item otherwise;
        # without split, an item's epochs share the entire rendered prompt.
        anth = anthropic_style_caching(cond.model)
        cache_on = (
            scheduling
            and reps > 1
            and min_tok is not None
            and not (anth and prep.config.solvers.cache_prompt == "off")
        )

        def cache_groups(
            subset,
            warm_items=frozenset(),
            cond_warm=False,
            cond=cond,
            idx=idx,
            item_tokens=item_tokens,
            template=template,
        ):
            if cond.split_prompt and idx > 0:
                head_text = template.text[:idx]
                if "{id}" not in head_text:  # one group: the whole condition
                    if not subset:
                        return []
                    return [(len(subset) * reps, estimate_tokens(head_text), cond_warm)]
                return [
                    (
                        reps,
                        estimate_tokens(render_template(head_text, {"id": it.id})),
                        it.id in warm_items,
                    )
                    for it in subset
                ]
            return [(reps, item_tokens(it), it.id in warm_items) for it in subset]

        calls = len(items) * reps
        input_tokens = in_tokens(items)
        max_out = cond.gen_params.max_tokens
        if max_out is None and "uncapped-generation" not in " ".join(gen_warnings):
            gen_warnings.append(
                "uncapped-generation: no max_tokens configured for the generate "
                f"stage; assuming {DEFAULT_OUTPUT_TOKENS_GENERATE} output tokens "
                "per call (actuals may exceed the estimate)"
            )
        output_tokens = calls * (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE)
        cache_read = cache_write = 0
        if cache_on:
            cache_read, cache_write = _cache_split(cache_groups(items), min_tok, anth)
        gen_conditions.append(
            _condition_estimate(
                prep,
                "generate",
                cond.id,
                cond.slug,
                cond.model,
                calls,
                input_tokens,
                output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
            )
        )

        # Delta: what the runner would actually launch for this condition.
        if force:
            to_run = list(item_ids)
        else:
            missing = epochs_to_run(
                solutions_df,
                cond.id,
                item_ids,
                (epoch_lo, epoch_hi),
                require_solution=rerun_empty,
            )
            to_run = [iid for iid in item_ids if missing[iid]]
        run_set = set(to_run)
        rem_items = [it for it in items if it.id in run_set]
        rem_calls = len(rem_items) * reps
        gen_delta["calls"] += rem_calls
        rem_in = in_tokens(rem_items)
        rem_out = rem_calls * (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE)
        cond_rows = scoped[scoped["condition_id"] == cond.id] if not scoped.empty else scoped
        rem_usd = _priced_usd(prep, cond.model, rem_in, rem_out)
        if cache_on and rem_items:
            # Same per-group split over the remaining groups only; a group
            # with ≥1 completed row is warm (its leader already ran).
            done_items = (
                set(cond_rows.loc[cond_rows["error"].isna(), "item_id"])
                if not cond_rows.empty
                else set()
            )
            d_read, d_write = _cache_split(
                cache_groups(rem_items, warm_items=done_items, cond_warm=bool(done_items)),
                min_tok,
                anth,
            )
            discounted = _discounted_usd(prep, cond.model, rem_in, rem_out, d_read, d_write)
            gen_delta["discount"] += rem_usd - discounted
            rem_usd = discounted
        gen_delta["usd"] += rem_usd
        gen_delta["total"] += calls
        if not cond_rows.empty:
            gen_delta["completed"] += int(cond_rows["error"].isna().sum())
            existing = set(zip(cond_rows["item_id"], cond_rows["epoch"].astype(int)))
            gen_delta["replaced"] += sum(
                1 for iid in to_run for e in range(epoch_lo, epoch_hi + 1) if (iid, e) in existing
            )

    # Index stored solutions for judge input sizing (actual text when available).
    stored: dict[tuple, str] = {}
    if solutions_df is not None and not solutions_df.empty:
        ok = solutions_df[solutions_df["solution"].notna()]
        stored = {(r.condition_id, r.item_id, int(r.epoch)): r.solution for r in ok.itertuples()}

    grade_conditions = []
    grade_delta = {
        "usd": 0.0,
        "calls": 0,
        "completed": 0,
        "total": 0,
        "replaced": 0,
        "discount": 0.0,
    }
    for cond in prep.grid.grade:
        # Delta: the same pending predicate the grade runner uses.
        gradable = pending_solutions(
            scoped, gradings_df, cond.id, True, include_empty=include_empty
        )
        pending_nf = pending_solutions(
            scoped, gradings_df, cond.id, False, include_empty=include_empty
        )
        pending = gradable if force else pending_nf
        grade_delta["total"] += len(gradable)
        grade_delta["completed"] += len(gradable) - len(pending_nf)
        if not gradings_df.empty:
            done = gradings_df[gradings_df["grade_condition_id"] == cond.id]
            existing = set(
                zip(done["gen_condition_id"], done["item_id"], done["epoch"].astype(int))
            )
            grade_delta["replaced"] += sum(
                1
                for row in pending.itertuples()
                if (row.condition_id, row.item_id, int(row.epoch)) in existing
            )

        if cond.kind == "verifiable":
            grade_conditions.append(
                _condition_estimate(prep, "grade", cond.id, cond.slug, "(verifiable)", 0, 0, 0)
            )
            continue
        rubric = prep.rubric_templates[cond.rubric_name]
        min_tok = min_cacheable_prefix(cond.grader_model)
        if cond.split_rubric and min_tok is not None:
            heads = [
                estimate_tokens(head)
                for it in items
                if (head := judge_head_text(it, rubric)) is not None
            ]
            note_heads("grade", cond.grader_model, min_tok, heads)

        # Cache projection (W3): judge tasks always group by item (the shared
        # head is rubric+problem+scheme+reference) and always request markers
        # (cache_prompt="auto"); Anthropic-style block caching additionally
        # needs the split layout to place the boundary at the head — token-
        # prefix providers share the head automatically either way.
        anth = anthropic_style_caching(cond.grader_model)
        cache_on = scheduling and min_tok is not None and (not anth or cond.split_rubric)

        def head_tokens_of(item_id, rubric=rubric):
            it = prep.items_by_id.get(item_id)
            head = judge_head_text(it, rubric) if it is not None else None
            return estimate_tokens(head) if head is not None else None

        calls = 0
        input_tokens = 0
        for gen_cond in prep.grid.generate:
            placeholder_len = 4 * (gen_cond.gen_params.max_tokens or DEFAULT_OUTPUT_TOKENS_GENERATE)
            for it in items:
                for epoch in range(1, reps + 1):
                    solution = stored.get((gen_cond.id, it.id, epoch), "x" * placeholder_len)
                    input_tokens += estimate_tokens(build_judge_input(it, solution, rubric))
                    calls += 1
        output_tokens = calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)
        cache_read = cache_write = 0
        if cache_on:
            group_calls = len(prep.grid.generate) * reps
            groups = [
                (group_calls, head_tok, False)
                for it in items
                if (head_tok := head_tokens_of(it.id)) is not None
            ]
            cache_read, cache_write = _cache_split(groups, min_tok, anth)
        grade_conditions.append(
            _condition_estimate(
                prep,
                "grade",
                cond.id,
                cond.slug,
                cond.grader_model,
                calls,
                input_tokens,
                output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
            )
        )
        rem_calls = len(pending)
        rem_in = sum(
            estimate_tokens(build_judge_input(prep.items_by_id[row.item_id], row.solution, rubric))
            for row in pending.itertuples()
            if row.item_id in prep.items_by_id
        )
        rem_out = rem_calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)
        grade_delta["calls"] += rem_calls
        rem_usd = _priced_usd(prep, cond.grader_model, rem_in, rem_out)
        if cache_on and rem_calls:
            # Remaining groups only; an item with already-graded rows is warm
            # (gradable rows minus still-pending rows > 0 for that item).
            gradable_n = {k: int(v) for k, v in gradable["item_id"].value_counts().items()}
            pending_n = {k: int(v) for k, v in pending_nf["item_id"].value_counts().items()}
            groups = [
                (int(n), head_tok, gradable_n.get(item_id, 0) > pending_n.get(item_id, 0))
                for item_id, n in pending["item_id"].value_counts().items()
                if (head_tok := head_tokens_of(item_id)) is not None
            ]
            d_read, d_write = _cache_split(groups, min_tok, anth)
            discounted = _discounted_usd(prep, cond.grader_model, rem_in, rem_out, d_read, d_write)
            grade_delta["discount"] += rem_usd - discounted
            rem_usd = discounted
        grade_delta["usd"] += rem_usd

    def stage_total(
        stage: str, conditions: "list[ConditionEstimate]", delta: dict, stage_warnings: "list[str]"
    ) -> StageEstimate:
        usd = sum(c.usd or 0.0 for c in conditions)
        return StageEstimate(
            hints=stage_hints(stage),
            stage=stage,
            calls=sum(c.calls for c in conditions),
            input_tokens=sum(c.input_tokens for c in conditions),
            output_tokens=sum(c.output_tokens for c in conditions),
            usd=usd,
            unpriced_models=sorted({c.model for c in conditions if not c.priced and c.calls > 0}),
            conditions=conditions,
            full_usd=usd,
            remaining_usd=delta["usd"],
            remaining_calls=delta["calls"],
            completed_cells=delta["completed"],
            total_cells=delta["total"],
            rows_replaced=delta["replaced"],
            warnings=stage_warnings,
            cache_read_tokens=sum(c.cache_read_tokens for c in conditions),
            cache_write_tokens=sum(c.cache_write_tokens for c in conditions),
            cache_discount_usd=sum(c.cache_discount_usd for c in conditions),
            remaining_cache_discount_usd=delta["discount"],
        )

    gen = stage_total("generate", gen_conditions, gen_delta, gen_warnings)
    grade = stage_total("grade", grade_conditions, grade_delta, grade_warnings)
    unpriced = detect_unpriced_models(sorted({*gen.unpriced_models, *grade.unpriced_models}))
    return Estimate(
        study=prep.config.study,
        policy=prep.plan.policy,
        policy_source=prep.policy_source,
        generate=gen,
        grade=grade,
        total_usd=gen.usd + grade.usd,
        warnings=gen_warnings + grade_warnings,
        pricing=describe_pricing(prep.pricing, refreshed=prep.pricing_refreshed),
        hints=[*gen.hints, *grade.hints, *([unpriced] if unpriced else [])],
        datasets=dataset_provenance(prep.datasets),
    )
