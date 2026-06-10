"""Grid-completion status report (no model API calls)."""

import pandas as pd
from pydantic import BaseModel, ConfigDict

from itemeval._config import ExperimentConfig
from itemeval._prepare import PreparedStudy, prepare_study
from itemeval.store import _gradings, _ledger, _solutions
from itemeval.store._solutions import empty_solution_mask


class DatasetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    revision: str
    n_items: int


class ConditionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str
    slug: str
    stage: str
    detail: dict[str, str]  # generate: model/prompt/model_config; grade: grader/rubric|scorer
    expected: int
    completed: int
    errors: int
    incomplete: int = 0  # generate: empty (no-error) completions, e.g. truncated
    parse_failures: int = 0


class StatusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str
    policy: str
    config_path: str
    datasets: list[DatasetStatus]
    n_items_total: int
    n_items_effective: int
    replications_requested: int
    replications_effective: int
    generate: list[ConditionStatus]
    grade: list[ConditionStatus]
    spend_generate_usd: float
    spend_grade_usd: float
    manifests: list[str]  # sorted filenames


def _usd(series) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0.0).sum())


def build_status(config: ExperimentConfig, prep: "PreparedStudy | None" = None) -> StatusReport:
    prep = prep or prepare_study(config)
    solutions = _solutions.read_solutions(prep.paths)
    gradings = _gradings.read_gradings(prep.paths)
    ledger = _ledger.read_ledger(prep.paths)

    effective_ids = {it.id for it in prep.items_effective}
    reps = prep.plan.replications
    expected_gen = len(prep.items_effective) * reps
    # rerun policy re-attempts empty completions, so they don't count as done.
    rerun_empty = config.solvers.on_empty == "rerun"

    gen_status = []
    for cond in prep.grid.generate:
        rows = solutions[solutions["condition_id"] == cond.id] if not solutions.empty else solutions
        in_scope = (
            rows[rows["item_id"].isin(effective_ids) & (rows["epoch"].astype(int) <= reps)]
            if not rows.empty
            else rows
        )
        incomplete = int(empty_solution_mask(in_scope).sum()) if not in_scope.empty else 0
        err_null = int(in_scope["error"].isna().sum()) if not in_scope.empty else 0
        gen_status.append(
            ConditionStatus(
                condition_id=cond.id,
                slug=cond.slug,
                stage="generate",
                detail={
                    "model": cond.model,
                    "prompt": cond.prompt_name,
                    "model_config": cond.model_config_name,
                },
                expected=expected_gen,
                completed=err_null - (incomplete if rerun_empty else 0),
                errors=int(in_scope["error"].notna().sum()) if not in_scope.empty else 0,
                incomplete=incomplete,
            )
        )

    gradable = 0
    if not solutions.empty:
        scoped = solutions[
            solutions["item_id"].isin(effective_ids) & (solutions["epoch"].astype(int) <= reps)
        ]
        # gradable = produced a non-empty completion (error null, not blank). With
        # on_empty=grade the empties are gradable too (judged as-is).
        if config.solvers.on_empty == "grade":
            gradable = int(scoped["error"].isna().sum())
        else:
            gradable = int((scoped["error"].isna() & ~empty_solution_mask(scoped)).sum())

    grade_status = []
    for cond in prep.grid.grade:
        rows = (
            gradings[gradings["grade_condition_id"] == cond.id] if not gradings.empty else gradings
        )
        completed = int(rows["error"].isna().sum()) if not rows.empty else 0
        errors = int(rows["error"].notna().sum()) if not rows.empty else 0
        parse_failures = (
            int((rows["error"].isna() & ~rows["parse_ok"].astype(bool)).sum())
            if not rows.empty
            else 0
        )
        detail = (
            {"scorer": cond.scorer or ""}
            if cond.kind == "verifiable"
            else {"grader": cond.grader_name or "", "rubric": cond.rubric_name or ""}
        )
        grade_status.append(
            ConditionStatus(
                condition_id=cond.id,
                slug=cond.slug,
                stage="grade",
                detail=detail,
                expected=gradable,
                completed=completed,
                errors=errors,
                parse_failures=parse_failures,
            )
        )

    spend_gen = spend_grade = 0.0
    if not ledger.empty:
        spend_gen = _usd(ledger[ledger["stage"] == "generate"]["usd"])
        spend_grade = _usd(ledger[ledger["stage"] == "grade"]["usd"])

    manifests = (
        sorted(p.name for p in prep.paths.manifests_dir.glob("*.json"))
        if prep.paths.manifests_dir.is_dir()
        else []
    )

    return StatusReport(
        study=config.study,
        policy=prep.plan.policy,
        config_path=str(config.config_path) if config.config_path else "(in-memory)",
        datasets=[
            DatasetStatus(id=ds.dataset_id, revision=ds.revision, n_items=len(ds.items))
            for ds in prep.datasets
        ],
        n_items_total=len(prep.items_all),
        n_items_effective=len(prep.items_effective),
        replications_requested=config.facets.replications,
        replications_effective=reps,
        generate=gen_status,
        grade=grade_status,
        spend_generate_usd=spend_gen,
        spend_grade_usd=spend_grade,
        manifests=manifests,
    )
