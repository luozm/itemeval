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
    usd: float  # unpriced conditions contribute 0
    unpriced_models: list[str]
    conditions: list[ConditionEstimate]


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


def estimate_study(prep: "PreparedStudy", solutions_df: "pd.DataFrame | None" = None) -> Estimate:
    """Project the full policy-effective grid; resume state is NOT subtracted.

    When solutions_df is None, the study's solutions store is read so judge
    input sizing uses real stored solutions where they exist.
    """
    if solutions_df is None:
        from itemeval.store._solutions import read_solutions

        solutions_df = read_solutions(prep.paths)
    items = prep.items_effective
    reps = prep.plan.replications
    warnings: list[str] = []

    gen_conditions = []
    for cond in prep.grid.generate:
        template = prep.solver_templates[cond.prompt_name]
        calls = len(items) * reps
        input_tokens = (
            sum(
                estimate_tokens(render_template(template.text, {"input": it.input, "id": it.id}))
                for it in items
            )
            * reps
        )
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

    # Index stored solutions for judge input sizing (actual text when available).
    stored: dict[tuple, str] = {}
    if solutions_df is not None and not solutions_df.empty:
        ok = solutions_df[solutions_df["solution"].notna()]
        stored = {(r.condition_id, r.item_id, int(r.epoch)): r.solution for r in ok.itertuples()}

    grade_conditions = []
    for cond in prep.grid.grade:
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

    def stage_total(stage: str, conditions: "list[ConditionEstimate]") -> StageEstimate:
        return StageEstimate(
            stage=stage,
            calls=sum(c.calls for c in conditions),
            input_tokens=sum(c.input_tokens for c in conditions),
            output_tokens=sum(c.output_tokens for c in conditions),
            usd=sum(c.usd or 0.0 for c in conditions),
            unpriced_models=sorted({c.model for c in conditions if not c.priced and c.calls > 0}),
            conditions=conditions,
        )

    gen = stage_total("generate", gen_conditions)
    grade = stage_total("grade", grade_conditions)
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
