"""Dry-run cost projection: token heuristics x policy-effective grid."""

from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from itemeval._endpoints import min_cacheable_prefix, routing_warnings
from itemeval._hints import (
    Hint,
    detect_anthropic_openrouter_no_split,
    detect_estimate_is_ceiling,
    detect_native_batch_available,
    detect_split_head_below_min,
    detect_unpriced_models,
)
from itemeval._modelsample import ModelSampleResult
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
from itemeval.budget._routing import NativeRoute, eligible_native_routes, native_batch_broken
from itemeval.grade._judge import build_judge_input, judge_head_text

if TYPE_CHECKING:
    from itemeval._prepare import PreparedStudy

# Output-token fill-ins when no max_tokens cap is configured. Generation is
# deliberately pessimistic: an uncapped run can emit far more than a typical
# completion, and the gate must not be driven by an under-estimate.
DEFAULT_OUTPUT_TOKENS_GENERATE = 4096
DEFAULT_OUTPUT_TOKENS_JUDGE = 512

# Minimum observed samples before a model's own mean is trusted for the expected
# (calibrated) projection; below it the estimate borrows the reasoning-group
# mean, then the global pooled mean (see _mean_resolver). Internal, not a config
# knob — a `--policy dev` pilot yields ~dev_items x prompts samples per model.
K_CALIBRATION_SAMPLES = 5

# Coarse per-call latency (seconds) for the wall-clock ETA when this study has
# no observed latency yet. A rough planning prior, not a measurement — the ETA
# is always labeled rough and never gates anything.
DEFAULT_CALL_LATENCY_S = 8.0


def median_latency_s(df: "pd.DataFrame | None") -> "float | None":
    """Median positive per-call latency from a stage store, or None when the
    store is empty / has no latency column / carries no positive values."""
    if df is None or getattr(df, "empty", True) or "latency_s" not in df.columns:
        return None
    vals = df["latency_s"].dropna()
    vals = vals[vals > 0]
    return float(vals.median()) if len(vals) else None


def eta_seconds(
    remaining_calls: int, concurrency: int, latency_s: "float | None"
) -> "float | None":
    """Coarse wall-clock estimate: (calls / concurrency) × per-call latency.

    None when nothing is left to run. `latency_s` is this study's observed
    median when available, else a default prior is used by the caller."""
    if remaining_calls <= 0:
        return None
    per_call = latency_s if (latency_s and latency_s > 0) else DEFAULT_CALL_LATENCY_S
    return (remaining_calls / max(1, concurrency)) * per_call


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


class Calibration(BaseModel):
    """How the stage's expected projection was calibrated (append-only).

    The four model counts partition the stage's models by which mean tier they
    used, so a *borrowed* estimate is never read as *measured*. `uncalibrated`
    (the model stayed at its ceiling) only happens at cold start — an empty
    stage store, where the expected figure equals the ceiling.
    """

    model_config = ConfigDict(extra="forbid")

    calibrated_models: int = 0  # used their own observed mean (>= K samples)
    group_models: int = 0  # borrowed the reasoning-group mean
    pooled_models: int = 0  # borrowed the global pooled mean
    uncalibrated_models: int = 0  # no data anywhere -> stayed at the ceiling
    observed_rows: int = 0  # rows the stage's means were computed from
    mean_output_tokens: float | None = None  # pooled mean output tokens/call
    mean_solution_chars: float | None = None  # grade only: pooled mean solution length


class StageEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    usd: float  # unpriced conditions contribute 0; ALWAYS the full grid
    unpriced_models: list[str]
    conditions: list[ConditionEstimate]
    # Expected (calibrated) projection alongside the ceiling `usd` (append-only).
    # Swaps each worst-case token assumption for an observed mean from the
    # stores; equals the ceiling at cold start. INFORMATIONAL ONLY — the money
    # gate keeps comparing `remaining_usd`/`usd` (UX-PATTERNS Law 2).
    expected_usd: float = 0.0  # full grid, calibrated
    expected_remaining_usd: float = 0.0  # the remaining delta, calibrated
    calibration: Calibration = Field(default_factory=Calibration)
    # Delta-aware figures (append-only; `usd` keeps meaning the full grid).
    # `remaining_usd` is what this run can actually spend — the gate operates
    # on it; completed cells are never re-paid. With force=True it equals full.
    full_usd: float = 0.0  # == usd, named for clarity alongside remaining_usd
    remaining_usd: float = 0.0
    remaining_calls: int = 0
    completed_cells: int = 0  # completed (condition x item x epoch) cells in scope
    total_cells: int = 0
    # Pre-flight local-response-cache projection (cache-projection; append-only).
    # Of the remaining calls, how many will inspect serve from its local response
    # cache ($0) vs pay fresh — so a recovery/--force/replication re-run isn't
    # over-stated. `real_remaining_usd` prices only the fresh remainder
    # (≈ remaining_usd × misses/(hits+misses)). INFORMATIONAL ONLY — the money
    # gate keeps comparing `remaining_usd`/`usd` (UX-PATTERNS Law 2). 0 / equal
    # to remaining_usd when caching is off or nothing is cached.
    cache_hits: int = 0
    cache_misses: int = 0
    real_remaining_usd: float = 0.0
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
    # Native batch routing (append-only): the discount in `remaining_usd`
    # attributable to routing eligible OpenRouter models to their native batch
    # API — realized when `budget.prefer_native_batch` is on, otherwise the
    # *available* lever the `native-batch-available` hint points at. 0 off-batch.
    native_route_savings_usd: float = 0.0
    # Coarse wall-clock projection (append-only; advice-grade, never a gate —
    # conditions run concurrently across distinct models, so wall-clock is
    # ~remaining_calls/concurrency × per-call latency). `eta_latency_basis` is
    # "observed" when seeded from this study's stored latency, else "default".
    concurrency: int = 1
    eta_seconds: "float | None" = None
    eta_latency_basis: str = "default"


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
    model_sample: "ModelSampleResult | None" = None  # set when solvers.sample drew the models
    # Native batch routing (append-only): one entry per OpenRouter model with an
    # eligible native batch endpoint + key present (batch plans only). Carries
    # the sampled/native ids and W2's batch-vs-cache comparison.
    routes: list[NativeRoute] = Field(default_factory=list)


def _batch_discount(prep: "PreparedStudy", model: str) -> bool:
    return (
        prep.plan.batch is not None
        and provider_of(model) in BATCH_PROVIDERS
        and not native_batch_broken(model)  # broken native batch runs interactively, full price
    )


def _stats_by(
    df: "pd.DataFrame", key_col: str, values: "str | pd.Series | None"
) -> "dict[str, tuple[float, int]]":
    """{model -> (sum, count)} over rows with a non-null value.

    `values` is a column name or a Series aligned to `df` (a derived series such
    as solution char length); a missing key/value column or None yields {}
    (tolerates the minimal frames callers sometimes build)."""
    if df.empty or key_col not in df.columns:
        return {}
    if isinstance(values, str):
        if values not in df.columns:
            return {}
        values = df[values]
    if values is None:
        return {}
    sub = pd.DataFrame({"k": df[key_col].astype(str), "v": values})
    sub = sub[sub["v"].notna()]
    if sub.empty:
        return {}
    grp = sub.groupby("k")["v"].agg(["sum", "count"])
    return {str(k): (float(r["sum"]), int(r["count"])) for k, r in grp.iterrows()}


def _mean_resolver(
    stats: "dict[str, tuple[float, int]]", reasoning_of
) -> "tuple[object, float | None, int]":
    """Coverage-aware per-model mean: own (>=K) -> reasoning-group -> pooled.

    Returns `(resolve, pooled_mean, total_count)` where `resolve(model)` is
    `(value, tier)` with tier in {"own", "group", "pooled", "none"}; value is
    None only when no observations exist anywhere (the "none" tier — cold start).
    `reasoning_of(model)` returns the model's reasoning flag (bool) or None.
    """
    total_sum = sum(s for s, _ in stats.values())
    total_cnt = sum(c for _, c in stats.values())
    pooled = total_sum / total_cnt if total_cnt else None
    grp_sum: "dict[bool, float]" = {}
    grp_cnt: "dict[bool, int]" = {}
    for m, (s, c) in stats.items():
        r = reasoning_of(m)
        if r is None:
            continue
        grp_sum[r] = grp_sum.get(r, 0.0) + s
        grp_cnt[r] = grp_cnt.get(r, 0) + c

    def resolve(model: str) -> "tuple[float | None, str]":
        s, c = stats.get(model, (0.0, 0))
        if c >= K_CALIBRATION_SAMPLES:
            return s / c, "own"
        r = reasoning_of(model)
        if r is not None and grp_cnt.get(r):
            return grp_sum[r] / grp_cnt[r], "group"
        if pooled is not None:
            return pooled, "pooled"
        return None, "none"

    return resolve, pooled, total_cnt


def _calibration(resolve, models: "list[str]", pooled: "float | None", rows: int, sol_chars=None):
    """Bucket the stage's distinct models by their mean tier into a Calibration."""
    counts = {"own": 0, "group": 0, "pooled": 0, "none": 0}
    for m in sorted(set(models)):
        counts[resolve(m)[1]] += 1
    return Calibration(
        calibrated_models=counts["own"],
        group_models=counts["group"],
        pooled_models=counts["pooled"],
        uncalibrated_models=counts["none"],
        observed_rows=rows,
        mean_output_tokens=pooled,
        mean_solution_chars=sol_chars,
    )


def _priced_usd(
    prep: "PreparedStudy",
    model: str,
    input_tokens: int,
    output_tokens: int,
    exec_model: "str | None" = None,
) -> float:
    """Projected USD for a call volume; 0 when the model is unpriced.

    Price is read under `model` — the sampled/scientific id the pricing table
    carries. Batch-discount eligibility is checked on `exec_model` (the native
    id when this model is routed, else `model`), so native batch routing earns
    the discount while the cost stays priced off the roster id (the table keys
    models under OpenRouter's spelling; the native id isn't reliably priceable).
    See budget/_routing.py."""
    price = lookup_price(prep.pricing, model)
    if price is None:
        return 0.0
    discount = 0.5 if _batch_discount(prep, exec_model or model) else 1.0
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
    exec_model: "str | None" = None,
) -> ConditionEstimate:
    # Price under the sampled `model`; batch eligibility under `exec_model` (the
    # native id when routed). `ConditionEstimate.model` stays the sampled id.
    price = lookup_price(prep.pricing, model)
    discount = _batch_discount(prep, exec_model or model)
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
    from itemeval.store._solutions import (
        empty_solution_mask,
        epochs_to_run,
        read_solutions,
        resolve_wave,
    )

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
    # Grade scopes to the current gen grid: solutions whose gen-condition left
    # the grid (a config change rehashed the ids) are orphans, never (re-)graded
    # — the same scope the grade runner and `status` use. Without this the grade
    # *remaining* projection counts orphaned rows and can exceed the ceiling usd.
    grid_gen_ids = {c.id for c in prep.grid.generate}
    rerun_empty = prep.config.solvers.on_empty == "rerun"
    include_empty = prep.config.solvers.on_empty == "grade"
    gen_warnings, grade_warnings = routing_warnings(prep.config)

    # split-head-below-min detection (W4): shared heads of split layouts whose
    # chars/4 estimate falls below the provider's minimum cacheable prefix,
    # aggregated per (stage, model, minimum) — one hint line per run, not per
    # condition. Only providers with a known minimum are checked (never guess).
    head_stats: "dict[tuple[str, str, int], dict]" = {}

    # anthropic-openrouter-no-split: models whose projected discount was
    # suppressed because monolithic prompts via OpenRouter cannot engage the
    # provider cache (no cache_control breakpoint lands on a single
    # string-content user message — verified live 2026-06-12, inspect 0.3.239).
    no_split_models: "dict[str, set[str]]" = {"generate": set(), "grade": set()}

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
        h = detect_anthropic_openrouter_no_split(stage=stage, models=sorted(no_split_models[stage]))
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
            & solutions_df["condition_id"].isin(grid_gen_ids)
        ]

    # One truth value for "cache scheduling active" — the same predicate the
    # runners and task builders use (batch reorders calls; never both).
    scheduling = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None

    # Native batch routing: `active_routes` (= prep.native_routes) apply the batch
    # discount and are non-empty only under a batch plan + the opt-in knob;
    # `eligible_routes` (key-present routable models, knob aside) drive the
    # savings lever and the `native-batch-available` hint even when the knob is
    # off. Routing only buys the batch discount, so both are empty off-batch.
    batch_plan = prep.plan.batch is not None
    eligible_routes = eligible_native_routes(prep.config)[0] if batch_plan else {}
    active_routes = prep.native_routes
    # W2 dual projection: per eligible model, the *expected remaining* cost under
    # the two achievable modes — native batch (route + ×0.5) vs OpenRouter cache
    # (stay on the sampled id, non-batch, cache scheduling). Accumulated in the
    # loops below from the same rem_in/rem_out_exp the live passes compute, so
    # the live figures are untouched. cache scheduling is "would it engage if you
    # ran non-batch" — config setting, not the live (batch) plan.
    cache_cfg_on = prep.config.budget.cache_schedule != "off"
    route_work = {m: {"batch": 0.0, "cache": 0.0} for m in eligible_routes}

    # Expected (calibrated) projection substrate: per-model means read from the
    # stores — generate/judge output tokens, and solution length for the grade
    # input stub — each with a coverage fallback (own >= K -> reasoning-group ->
    # pooled). Pure read of existing rows; no model calls, never feeds the gate.
    def reasoning_of(model: str) -> "bool | None":
        price = lookup_price(prep.pricing, model)
        return price.reasoning if price is not None else None

    sol_ok = solutions_df[solutions_df["error"].isna()] if not solutions_df.empty else solutions_df
    gen_out_resolve, gen_out_mean, gen_out_rows = _mean_resolver(
        _stats_by(sol_ok, "model", "output_tokens"), reasoning_of
    )
    sol_lens = (
        sol_ok["solution"].astype("string").str.len().where(~empty_solution_mask(sol_ok))
        if not sol_ok.empty and "solution" in sol_ok.columns
        else None
    )
    gen_sol_resolve, gen_sol_mean, _ = _mean_resolver(
        _stats_by(sol_ok, "model", sol_lens), reasoning_of
    )
    grd_ok = gradings_df[gradings_df["error"].isna()] if not gradings_df.empty else gradings_df
    judge_out_resolve, judge_out_mean, judge_out_rows = _mean_resolver(
        _stats_by(grd_ok, "grader_model", "output_tokens"), reasoning_of
    )

    gen_conditions = []
    gen_delta = {
        "usd": 0.0,
        "calls": 0,
        "completed": 0,
        "total": 0,
        "replaced": 0,
        "discount": 0.0,
        "exp": 0.0,
        "route_savings": 0.0,
    }
    gen_exp_full = 0.0
    for cond in prep.grid.generate:
        template = prep.solver_templates[cond.prompt_name]
        # Native batch routing: `exec_model` (the native id when active) earns the
        # batch discount; cost stays priced under the sampled `cond.model`.
        exec_model = active_routes.get(cond.model, cond.model)

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
        would_cache = (
            scheduling
            and reps > 1
            and min_tok is not None
            and not (anth and prep.config.solvers.cache_prompt == "off")
        )
        # Anthropic-style markers cannot land on monolithic prompts routed
        # through OpenRouter: a single string-content user message gets no
        # cache_control breakpoint from inspect's openrouter provider, so the
        # discount is structurally unreachable — not best-case-optimistic
        # (verified live 2026-06-12, inspect 0.3.239; COST-OPTIMIZATION.md).
        or_mono = anth and not cond.split_prompt and provider_of(cond.model) == "openrouter"
        cache_on = would_cache and not or_mono
        if would_cache and or_mono:
            no_split_models["generate"].add(cond.model)

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
        # Expected output: the model's calibrated mean output tokens/call (ceiling
        # per-call when uncalibrated, so expected == ceiling at cold start).
        exp_out_val, _ = gen_out_resolve(cond.model)
        exp_per_call = (
            exp_out_val if exp_out_val is not None else (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE)
        )
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
                exec_model=exec_model,
            )
        )
        # Expected full: same input + cache split, calibrated output (input-side
        # caching is unaffected by the output assumption). Routing and caching
        # never co-occur (routing is batch-only, caching non-batch), so the
        # cache branch never sees a routed exec model.
        exp_out_full = calls * exp_per_call
        if cache_on:
            gen_exp_full += _discounted_usd(
                prep, cond.model, input_tokens, exp_out_full, cache_read, cache_write
            )
        else:
            gen_exp_full += _priced_usd(prep, cond.model, input_tokens, exp_out_full, exec_model)

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
        rem_out_exp = rem_calls * exp_per_call
        cond_rows = scoped[scoped["condition_id"] == cond.id] if not scoped.empty else scoped
        rem_usd = _priced_usd(prep, cond.model, rem_in, rem_out, exec_model)
        rem_exp_usd = _priced_usd(prep, cond.model, rem_in, rem_out_exp, exec_model)
        # Native batch routing savings (W1) + dual projection (W2), over the
        # remaining expected spend of an eligible condition. `route_savings` is
        # the available/realized batch discount; `route_work` records both
        # achievable modes for the per-model comparison.
        nat = eligible_routes.get(cond.model)
        if nat is not None and rem_calls:
            undisc = _priced_usd(prep, cond.model, rem_in, rem_out_exp)
            batch_c = _priced_usd(prep, cond.model, rem_in, rem_out_exp, nat)
            gen_delta["route_savings"] += undisc - batch_c
            # OpenRouter-cache counterfactual: non-batch with caching, honoring
            # the same eligibility the runtime would (anthropic-monolithic-via-OR
            # can't cache -> full price, correctly showing cache buys nothing).
            cf_on = (
                cache_cfg_on
                and reps > 1
                and min_tok is not None
                and not (anth and prep.config.solvers.cache_prompt == "off")
                and not or_mono
            )
            if cf_on and rem_items:
                cf_done = (
                    set(cond_rows.loc[cond_rows["error"].isna(), "item_id"])
                    if not cond_rows.empty
                    else set()
                )
                cf_r, cf_w = _cache_split(
                    cache_groups(rem_items, warm_items=cf_done, cond_warm=bool(cf_done)),
                    min_tok,
                    anth,
                )
                cache_c = _discounted_usd(prep, cond.model, rem_in, rem_out_exp, cf_r, cf_w)
            else:
                cache_c = undisc
            route_work[cond.model]["batch"] += batch_c
            route_work[cond.model]["cache"] += cache_c
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
            rem_exp_usd = _discounted_usd(prep, cond.model, rem_in, rem_out_exp, d_read, d_write)
        gen_delta["usd"] += rem_usd
        gen_delta["exp"] += rem_exp_usd
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
        "exp": 0.0,
        "route_savings": 0.0,
    }
    grade_exp_full = 0.0
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
        # Native batch routing for the judge model (priced under the sampled id).
        exec_grader = active_routes.get(cond.grader_model, cond.grader_model)
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
        if (
            scheduling
            and min_tok is not None
            and anth
            and not cond.split_rubric
            and provider_of(cond.grader_model) == "openrouter"
        ):
            no_split_models["grade"].add(cond.grader_model)

        def head_tokens_of(item_id, rubric=rubric):
            it = prep.items_by_id.get(item_id)
            head = judge_head_text(it, rubric) if it is not None else None
            return estimate_tokens(head) if head is not None else None

        # Expected judge output: the grader's calibrated mean (ceiling per-call
        # when uncalibrated).
        exp_judge_val, _ = judge_out_resolve(cond.grader_model)
        exp_judge_per_call = (
            exp_judge_val
            if exp_judge_val is not None
            else (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)
        )
        calls = 0
        input_tokens = 0
        exp_input_tokens = 0
        for gen_cond in prep.grid.generate:
            placeholder_len = 4 * (gen_cond.gen_params.max_tokens or DEFAULT_OUTPUT_TOKENS_GENERATE)
            # Expected stub length for un-generated solutions: the gen model's
            # calibrated mean solution length (ceiling 4xmax_tokens chars when
            # uncalibrated). Stored real solutions size both passes identically.
            exp_sol_val, _ = gen_sol_resolve(gen_cond.model)
            exp_ph_len = max(1, round(exp_sol_val)) if exp_sol_val is not None else placeholder_len
            for it in items:
                for epoch in range(1, reps + 1):
                    real = stored.get((gen_cond.id, it.id, epoch))
                    if real is not None:
                        tok = estimate_tokens(build_judge_input(it, real, rubric))
                        input_tokens += tok
                        exp_input_tokens += tok
                    else:
                        input_tokens += estimate_tokens(
                            build_judge_input(it, "x" * placeholder_len, rubric)
                        )
                        exp_input_tokens += estimate_tokens(
                            build_judge_input(it, "x" * exp_ph_len, rubric)
                        )
                    calls += 1
        output_tokens = calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)
        exp_output_tokens = calls * exp_judge_per_call
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
                exec_model=exec_grader,
            )
        )
        # Expected full: calibrated input stub + calibrated judge output, same
        # head-based cache split (the head is solution-independent).
        if cache_on:
            grade_exp_full += _discounted_usd(
                prep,
                cond.grader_model,
                exp_input_tokens,
                exp_output_tokens,
                cache_read,
                cache_write,
            )
        else:
            grade_exp_full += _priced_usd(
                prep, cond.grader_model, exp_input_tokens, exp_output_tokens, exec_grader
            )
        rem_calls = len(pending)
        rem_in = sum(
            estimate_tokens(build_judge_input(prep.items_by_id[row.item_id], row.solution, rubric))
            for row in pending.itertuples()
            if row.item_id in prep.items_by_id
        )
        rem_out = rem_calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)
        # Remaining grades all have real stored solutions, so `rem_in` is the same
        # for both passes; only the judge output assumption differs.
        rem_out_exp = rem_calls * exp_judge_per_call
        grade_delta["calls"] += rem_calls
        rem_usd = _priced_usd(prep, cond.grader_model, rem_in, rem_out, exec_grader)
        rem_exp_usd = _priced_usd(prep, cond.grader_model, rem_in, rem_out_exp, exec_grader)
        nat_grader = eligible_routes.get(cond.grader_model)
        if nat_grader is not None and rem_calls:
            undisc = _priced_usd(prep, cond.grader_model, rem_in, rem_out_exp)
            batch_c = _priced_usd(prep, cond.grader_model, rem_in, rem_out_exp, nat_grader)
            grade_delta["route_savings"] += undisc - batch_c
            cf_on = cache_cfg_on and min_tok is not None and (not anth or cond.split_rubric)
            if cf_on:
                gradable_n = {k: int(v) for k, v in gradable["item_id"].value_counts().items()}
                pending_n = {k: int(v) for k, v in pending_nf["item_id"].value_counts().items()}
                cf_groups = [
                    (int(n), head_tok, gradable_n.get(iid, 0) > pending_n.get(iid, 0))
                    for iid, n in pending["item_id"].value_counts().items()
                    if (head_tok := head_tokens_of(iid)) is not None
                ]
                cf_r, cf_w = _cache_split(cf_groups, min_tok, anth)
                cache_c = _discounted_usd(prep, cond.grader_model, rem_in, rem_out_exp, cf_r, cf_w)
            else:
                cache_c = undisc
            route_work[cond.grader_model]["batch"] += batch_c
            route_work[cond.grader_model]["cache"] += cache_c
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
            rem_exp_usd = _discounted_usd(
                prep, cond.grader_model, rem_in, rem_out_exp, d_read, d_write
            )
        grade_delta["usd"] += rem_usd
        grade_delta["exp"] += rem_exp_usd

    # Materialization sub-term: one call per item per materializing rubric, shared
    # across graders (dedup by materialize_id) and resume-aware via the artifact
    # store. Folded into the grade stage so the single money gate covers it. The
    # ceiling assumption (output at the materializer's max_tokens) also feeds the
    # expected figure — a one-shot per-item rubric is small and not separately
    # calibrated. force re-grades but never re-materializes (the rubric is frozen),
    # so the remaining materialize cost ignores force.
    from itemeval.grade._materialize import build_materialize_input, materialize_id
    from itemeval.store._materialized import read_materialized, stored_texts

    mat_existing = read_materialized(prep.paths)
    seen_mat: "set[str]" = set()
    for cond in prep.grid.grade:
        if cond.kind != "judge" or not cond.materialize_model:
            continue
        build_t = prep.build_templates.get(cond.rubric_name)
        if build_t is None:
            continue
        mid = materialize_id(build_t, cond.materialize_model)
        if mid in seen_mat:
            continue
        seen_mat.add(mid)
        exec_mat = active_routes.get(cond.materialize_model, cond.materialize_model)
        out_per = cond.materialize_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE
        in_tok = sum(estimate_tokens(build_materialize_input(it, build_t)) for it in items)
        out_tok = len(items) * out_per
        grade_conditions.append(
            _condition_estimate(
                prep,
                "grade",
                f"materialize:{cond.rubric_name}",
                f"materialize_{cond.rubric_name}",
                cond.materialize_model,
                len(items),
                in_tok,
                out_tok,
                exec_model=exec_mat,
            )
        )
        grade_exp_full += _priced_usd(prep, cond.materialize_model, in_tok, out_tok, exec_mat)
        frozen = stored_texts(mat_existing, mid)
        rem_items = [it for it in items if it.id not in frozen]
        rem_in = sum(estimate_tokens(build_materialize_input(it, build_t)) for it in rem_items)
        rem_out = len(rem_items) * out_per
        rem_usd = _priced_usd(prep, cond.materialize_model, rem_in, rem_out, exec_mat)
        grade_delta["usd"] += rem_usd
        grade_delta["exp"] += rem_usd
        grade_delta["calls"] += len(rem_items)

    def stage_total(
        stage: str,
        conditions: "list[ConditionEstimate]",
        delta: dict,
        stage_warnings: "list[str]",
        exp_full: float,
        calibration: "Calibration",
    ) -> StageEstimate:
        usd = sum(c.usd or 0.0 for c in conditions)
        # Concurrency = distinct execution models that make calls (the run's
        # parallel-eval cap). Latency seeded from this study's matching store.
        exec_models = {prep.native_routes.get(c.model, c.model) for c in conditions if c.calls > 0}
        concurrency = max(1, len(exec_models))
        lat = median_latency_s(solutions_df if stage == "generate" else gradings_df)
        # Pre-flight response-cache projection: of the remaining calls, how many
        # already sit in inspect's local response cache ($0). Only when caching is
        # on (else the probe is a no-op); the inspect-importing probe is reached
        # lazily, so a no-cache estimate stays engine-free and light.
        cache_hits = cache_misses = 0
        real_remaining = delta["usd"]
        if prep.config.cache:
            from itemeval._cacheprobe import probe_stage

            probe = probe_stage(prep, stage, force=force)
            cache_hits, cache_misses = probe.cache_hits, probe.cache_misses
            probed = cache_hits + cache_misses
            if probed > 0:
                real_remaining = delta["usd"] * cache_misses / probed
        return StageEstimate(
            hints=stage_hints(stage),
            stage=stage,
            calls=sum(c.calls for c in conditions),
            concurrency=concurrency,
            eta_seconds=eta_seconds(delta["calls"], concurrency, lat),
            eta_latency_basis="observed" if lat else "default",
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
            expected_usd=exp_full,
            expected_remaining_usd=delta["exp"],
            calibration=calibration,
            native_route_savings_usd=delta["route_savings"],
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            real_remaining_usd=real_remaining,
        )

    gen_cal = _calibration(
        gen_out_resolve, [c.model for c in prep.grid.generate], gen_out_mean, gen_out_rows
    )
    grade_cal = _calibration(
        judge_out_resolve,
        [c.grader_model for c in prep.grid.grade if c.kind != "verifiable"],
        judge_out_mean,
        judge_out_rows,
        sol_chars=gen_sol_mean,
    )
    gen = stage_total("generate", gen_conditions, gen_delta, gen_warnings, gen_exp_full, gen_cal)
    grade = stage_total(
        "grade", grade_conditions, grade_delta, grade_warnings, grade_exp_full, grade_cal
    )
    # Cold-start ceiling hint (one per run): a stage that would spend but has no
    # observations to calibrate from shows a pure ceiling — point at the pilot.
    # Lives on Estimate.hints (the planning surface), NOT StageEstimate.hints —
    # the run commands raise it only at a gate stop (pre-spend), never on a
    # proceeding run where "pilot first" would be stale (see cli._run_stage).
    ceiling_hint = None
    for st in (gen, grade):
        ceiling_hint = ceiling_hint or detect_estimate_is_ceiling(
            observed_rows=st.calibration.observed_rows, projected_usd=st.remaining_usd
        )
    unpriced = detect_unpriced_models(sorted({*gen.unpriced_models, *grade.unpriced_models}))
    # Native batch routing surface (batch plans only): one NativeRoute per
    # eligible OpenRouter model; W2 fills the batch-vs-cache comparison. The
    # savings hint fires only when the knob is off and a positive lever exists.
    routes = [
        NativeRoute(
            sampled=sampled,
            execution=native,
            provider=provider_of(native),
            batch_usd=route_work[sampled]["batch"],
            cache_usd=route_work[sampled]["cache"],
            cheaper="batch"
            if route_work[sampled]["batch"] <= route_work[sampled]["cache"]
            else "cache",
        )
        for sampled, native in eligible_routes.items()
    ]
    route_savings = gen.native_route_savings_usd + grade.native_route_savings_usd
    native_hint = detect_native_batch_available(
        n_models=len(routes),
        providers=sorted({r.provider for r in routes}),
        savings_usd=route_savings,
        knob_on=prep.config.budget.prefer_native_batch,
    )
    return Estimate(
        study=prep.config.study,
        policy=prep.plan.policy,
        policy_source=prep.policy_source,
        generate=gen,
        grade=grade,
        total_usd=gen.usd + grade.usd,
        warnings=gen_warnings + grade_warnings,
        pricing=describe_pricing(prep.pricing, refreshed=prep.pricing_refreshed),
        hints=[
            *gen.hints,
            *grade.hints,
            *([ceiling_hint] if ceiling_hint else []),
            *([native_hint] if native_hint else []),
            *([unpriced] if unpriced else []),
        ],
        routes=routes,
        datasets=dataset_provenance(prep.datasets),
        model_sample=prep.model_sample,
    )
