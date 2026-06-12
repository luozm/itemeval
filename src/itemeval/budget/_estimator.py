"""Dry-run cost projection: token heuristics x policy-effective grid."""

from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from itemeval._hints import Hint, detect_unpriced_models
from itemeval._templates import render_template
from itemeval._util import estimate_tokens
from itemeval.budget._pricing import (
    BATCH_PROVIDERS,
    PricingProvenance,
    cost_usd,
    describe_pricing,
    lookup_price,
    provider_of,
)
from itemeval.grade._judge import build_judge_input

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
    usd: float | None
    priced: bool
    batch_discount: bool


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


def _batch_discount(prep: "PreparedStudy", model: str) -> bool:
    return prep.plan.batch is not None and provider_of(model) in BATCH_PROVIDERS


def _priced_usd(prep: "PreparedStudy", model: str, input_tokens: int, output_tokens: int) -> float:
    """Projected USD for a call volume; 0 when the model is unpriced."""
    price = lookup_price(prep.pricing, model)
    if price is None:
        return 0.0
    discount = 0.5 if _batch_discount(prep, model) else 1.0
    return cost_usd(price, input_tokens, output_tokens) * discount


def _condition_estimate(
    prep: "PreparedStudy",
    stage: str,
    condition_id: str,
    slug: str,
    model: str,
    calls: int,
    input_tokens: int,
    output_tokens: int,
) -> ConditionEstimate:
    price = lookup_price(prep.pricing, model)
    discount = _batch_discount(prep, model)
    usd = None
    if price is not None:
        usd = cost_usd(price, input_tokens, output_tokens) * (0.5 if discount else 1.0)
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
    )


def estimate_study(
    prep: "PreparedStudy",
    solutions_df: "pd.DataFrame | None" = None,
    gradings_df: "pd.DataFrame | None" = None,
    *,
    force: bool = False,
) -> Estimate:
    """Project the policy-effective grid: full figures plus the remaining delta.

    `usd`/`full_usd` always cover the full grid; `remaining_usd` subtracts
    already-complete cells using the same predicates the runners use
    (`items_to_run` for generate, `pending_solutions` for grade), so the gate
    can operate on what this run will actually spend. `force=True` makes
    remaining equal full (everything selected re-runs). When solutions_df is
    None, the study's solutions store is read so judge input sizing uses real
    stored solutions where they exist.
    """
    from itemeval.store._gradings import pending_solutions, read_gradings
    from itemeval.store._solutions import items_to_run, read_solutions

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
    warnings: list[str] = []

    # The runners' scope: effective items, epochs within effective replications.
    scoped = solutions_df
    if not solutions_df.empty:
        scoped = solutions_df[
            solutions_df["item_id"].isin(effective_ids)
            & (solutions_df["epoch"].astype(int) <= reps)
        ]

    gen_conditions = []
    gen_delta = {"usd": 0.0, "calls": 0, "completed": 0, "total": 0, "replaced": 0}
    for cond in prep.grid.generate:
        template = prep.solver_templates[cond.prompt_name]

        def in_tokens(subset, template=template):
            return (
                sum(
                    estimate_tokens(
                        render_template(template.text, {"input": it.input, "id": it.id})
                    )
                    for it in subset
                )
                * reps
            )

        calls = len(items) * reps
        input_tokens = in_tokens(items)
        max_out = cond.gen_params.max_tokens
        if max_out is None and "uncapped-generation" not in " ".join(warnings):
            warnings.append(
                "uncapped-generation: no max_tokens configured for the generate "
                f"stage; assuming {DEFAULT_OUTPUT_TOKENS_GENERATE} output tokens "
                "per call (actuals may exceed the estimate)"
            )
        output_tokens = calls * (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE)
        gen_conditions.append(
            _condition_estimate(
                prep, "generate", cond.id, cond.slug, cond.model, calls, input_tokens, output_tokens
            )
        )

        # Delta: what the runner would actually launch for this condition.
        to_run = (
            list(item_ids)
            if force
            else items_to_run(solutions_df, cond.id, item_ids, reps, require_solution=rerun_empty)
        )
        run_set = set(to_run)
        rem_items = [it for it in items if it.id in run_set]
        rem_calls = len(rem_items) * reps
        gen_delta["calls"] += rem_calls
        gen_delta["usd"] += _priced_usd(
            prep,
            cond.model,
            in_tokens(rem_items),
            rem_calls * (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE),
        )
        gen_delta["total"] += calls
        cond_rows = scoped[scoped["condition_id"] == cond.id] if not scoped.empty else scoped
        if not cond_rows.empty:
            gen_delta["completed"] += int(cond_rows["error"].isna().sum())
            existing = set(zip(cond_rows["item_id"], cond_rows["epoch"].astype(int)))
            gen_delta["replaced"] += sum(
                1 for iid in to_run for e in range(1, reps + 1) if (iid, e) in existing
            )

    # Index stored solutions for judge input sizing (actual text when available).
    stored: dict[tuple, str] = {}
    if solutions_df is not None and not solutions_df.empty:
        ok = solutions_df[solutions_df["solution"].notna()]
        stored = {(r.condition_id, r.item_id, int(r.epoch)): r.solution for r in ok.itertuples()}

    grade_conditions = []
    grade_delta = {"usd": 0.0, "calls": 0, "completed": 0, "total": 0, "replaced": 0}
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
            )
        )
        rem_calls = len(pending)
        rem_in = sum(
            estimate_tokens(build_judge_input(prep.items_by_id[row.item_id], row.solution, rubric))
            for row in pending.itertuples()
            if row.item_id in prep.items_by_id
        )
        grade_delta["calls"] += rem_calls
        grade_delta["usd"] += _priced_usd(
            prep,
            cond.grader_model,
            rem_in,
            rem_calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE),
        )

    def stage_total(
        stage: str, conditions: "list[ConditionEstimate]", delta: dict
    ) -> StageEstimate:
        usd = sum(c.usd or 0.0 for c in conditions)
        return StageEstimate(
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
        )

    gen = stage_total("generate", gen_conditions, gen_delta)
    grade = stage_total("grade", grade_conditions, grade_delta)
    unpriced = detect_unpriced_models(sorted({*gen.unpriced_models, *grade.unpriced_models}))
    return Estimate(
        study=prep.config.study,
        policy=prep.plan.policy,
        policy_source=prep.policy_source,
        generate=gen,
        grade=grade,
        total_usd=gen.usd + grade.usd,
        warnings=warnings,
        pricing=describe_pricing(prep.pricing, refreshed=prep.pricing_refreshed),
        hints=[unpriced] if unpriced else [],
    )
