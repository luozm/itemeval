"""Grade-stage orchestrator: verifiable in-process, judge via inspect tasks."""

from typing import TYPE_CHECKING, Any

import inspect_ai
from pydantic import BaseModel, ConfigDict, Field

from itemeval._errors import StoreError
from itemeval._hints import (
    Hint,
    detect_cache_zero_reads,
    detect_empty_solutions,
    detect_unpriced_models,
)
from itemeval._manifest import build_manifest, finalize_manifest, write_manifest
from itemeval._mockmodels import is_mock_model
from itemeval.adapters._base import DatasetProvenance, dataset_provenance
from itemeval.budget._gate import GateResult
from itemeval.budget._pricing import PricingProvenance, lookup_price
from itemeval._mockmodels import resolve_model
from itemeval._util import new_run_id, utc_now_iso
from itemeval.design._grid import GradeCondition
from itemeval.generate._run import (
    ConditionRunReport,
    ModelFactory,
    cache_columns,
    endpoint_info,
    enforce_budget_cap,
    ledger_row,
    local_cache_dir,
    local_cache_rows,
    log_index_row,
    matches_filter,
    resolve_display,
    sum_usage,
    usage_columns,
    usd_for_usage,
)
from itemeval.grade._judge import build_judge_task
from itemeval.grade._parse import parse_judge_output
from itemeval.grade._verifiable import VERIFIABLE_SCORERS
from itemeval.store import _gradings, _ledger, _logs, _solutions
from itemeval.store._base import rel_to_study
from itemeval.store._solutions import empty_solution_mask

if TYPE_CHECKING:
    import pandas as pd

    from itemeval._prepare import PreparedStudy


class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    study: str
    conditions: list[ConditionRunReport]
    rows_written: int
    parse_failures: int
    total_usd: float
    manifest_path: str
    on_empty: str = "skip"  # solvers.on_empty policy in effect
    empty_total: int = 0  # scoped empty (no-error) solutions
    empty_skipped: int = 0  # of those, how many were excluded from grading
    empty_stop_reasons: "dict[str, int]" = Field(default_factory=dict)
    hints: list[Hint] = Field(default_factory=list)
    datasets: list[DatasetProvenance] = Field(default_factory=list)
    # Local response-cache reuse (Law 1: reuse announced as loudly as fetching):
    local_cache_rows: int = 0
    local_cache_dir: "str | None" = None  # set when local_cache_rows > 0
    # Filled by the CLI for `--json` parity (Python callers compute their own):
    pricing: "PricingProvenance | None" = None
    estimate_usd: "float | None" = None  # remaining figure (gate input)
    rows_replaced: "int | None" = None  # existing rows this run planned to overwrite
    gate: "GateResult | None" = None


def _base_row(
    prep: "PreparedStudy", cond: GradeCondition, run_id: str, sol_row, now: str
) -> "dict[str, Any]":
    return {
        "study": prep.config.study,
        "run_id": run_id,
        "grade_condition_id": cond.id,
        "grade_condition_slug": cond.slug,
        "gen_condition_id": sol_row.condition_id,
        "item_id": sol_row.item_id,
        "epoch": int(sol_row.epoch),
        "grade_kind": cond.kind,
        "grader_name": cond.grader_name,
        "grader_model": cond.grader_model,
        "rubric_name": cond.rubric_name,
        "rubric_hash": cond.rubric_hash,
        "scorer_name": cond.scorer,
        "created_at": now,
    }


def _verifiable_rows(
    prep: "PreparedStudy", cond: GradeCondition, pending: "pd.DataFrame", run_id: str
) -> "list[dict]":
    scorer = VERIFIABLE_SCORERS[cond.scorer]
    now = utc_now_iso()
    rows = []
    for sol_row in pending.itertuples():
        result = scorer(sol_row.solution, prep.items_by_id[sol_row.item_id])
        rows.append(
            {
                **_base_row(prep, cond, run_id, sol_row, now),
                "score": result.score,
                "score_raw": result.score_raw,
                "parse_ok": result.parse_ok,
                "parse_error": result.parse_error,
                "reasoning": None,
                "judge_completion": None,
                "error": None,
                **usage_columns(None),
                "usd": 0.0,
                "latency_s": None,
                "log_file": None,
            }
        )
    return rows


def _judge_rows(
    prep: "PreparedStudy", cond: GradeCondition, pending: "pd.DataFrame", log, run_id: str
) -> "list[dict]":
    now = utc_now_iso()
    log_file = rel_to_study(prep.paths, log.location)
    sol_by_key = {(r.condition_id, r.item_id, int(r.epoch)): r for r in pending.itertuples()}
    rows = []
    for sample in log.samples or []:
        meta = sample.metadata or {}
        key = (meta["gen_condition_id"], meta["item_id"], int(meta["epoch"]))
        sol_row = sol_by_key[key]
        usage = sum_usage(sample)
        error = sample.error.message if sample.error else None
        completion = (
            sample.output.completion
            if (error is None and sample.output and sample.output.completion)
            else None
        )
        if completion is not None:
            parsed = parse_judge_output(completion)
            parse_cols = {
                "score": parsed.score,
                "score_raw": parsed.score_raw,
                "parse_ok": parsed.parse_ok,
                "parse_error": parsed.parse_error,
                "reasoning": parsed.reasoning,
            }
        else:
            # Sample-level error: not a parse failure — the row is pending again.
            parse_cols = {
                "score": None,
                "score_raw": None,
                "parse_ok": False,
                "parse_error": None,
                "reasoning": None,
            }
        rows.append(
            {
                **_base_row(prep, cond, run_id, sol_row, now),
                **parse_cols,
                "judge_completion": completion,
                "error": error,
                **usage_columns(usage),
                "usd": usd_for_usage(prep.pricing, cond.grader_model, usage, prep.plan.batch),
                "latency_s": sample.total_time,
                "log_file": log_file,
            }
        )
    return rows


def run_grade(
    prep: "PreparedStudy",
    *,
    run_id: "str | None" = None,
    force: bool = False,
    condition_filter: "list[str] | None" = None,
    graders: "list[str] | None" = None,
    rubrics: "list[str] | None" = None,
    display: "str | None" = None,
    model_factory: "ModelFactory | None" = None,
    estimate_usd: "float | None" = None,
    estimate_full_usd: "float | None" = None,
    max_usd: "float | None" = None,
) -> GradeResult:
    enforce_budget_cap(prep, "grade", max_usd, force)
    run_id = run_id or new_run_id("grade")
    prep.paths.ensure()
    solutions_df = _solutions.read_solutions(prep.paths)
    if solutions_df.empty:
        raise StoreError("no solutions in store; run generate first")

    # Policy scope: effective items, epochs within the effective replications.
    effective_ids = {it.id for it in prep.items_effective}
    scoped = solutions_df[
        solutions_df["item_id"].isin(effective_ids)
        & (solutions_df["epoch"].astype(int) <= prep.plan.replications)
    ]

    # Empty (no-error) completions: a distinct channel from API errors. The
    # solvers.on_empty policy decides whether they are graded as-is or skipped;
    # either way they are surfaced (never silently folded into "complete").
    on_empty = prep.config.solvers.on_empty
    include_empty = on_empty == "grade"
    empties = scoped[empty_solution_mask(scoped)]
    empty_total = int(len(empties))
    empty_skipped = 0 if include_empty else empty_total
    empty_stop_reasons = {
        str(k): int(v) for k, v in empties["stop_reason"].fillna("(none)").value_counts().items()
    }

    selected = []
    for cond in prep.grid.grade:
        if not matches_filter(cond.id, cond.slug, condition_filter):
            continue
        if graders or rubrics:
            if cond.kind != "judge":
                continue
            if graders and cond.grader_name not in graders:
                continue
            if rubrics and cond.rubric_name not in rubrics:
                continue
        selected.append(cond)

    manifest = build_manifest(
        prep, "grade", run_id, [c.id for c in selected], estimate_usd, estimate_full_usd
    )
    manifest_path = write_manifest(manifest, prep.paths)

    reports: list[ConditionRunReport] = []
    endpoints_effective: dict[str, Any] = {}
    rows_written = 0
    parse_failures = 0
    total_usd = 0.0
    judge_models: list[str] = []  # grader models of judge conditions that ran
    repeated_prefix_calls = 0  # judge calls beyond each same-item group's leader
    factory = model_factory or resolve_model

    for cond in selected:
        gradings_df = _gradings.read_gradings(prep.paths)
        pending = _gradings.pending_solutions(
            scoped, gradings_df, cond.id, force, include_empty=include_empty
        )
        if pending.empty:
            reports.append(
                ConditionRunReport(
                    condition_id=cond.id,
                    slug=cond.slug,
                    status="skipped",
                    items_run=0,
                    rows_written=0,
                    errors=0,
                    usd=None,
                    log_file=None,
                )
            )
            continue

        if cond.kind == "verifiable":
            rows = _verifiable_rows(prep, cond, pending, run_id)
            log_file = None
            cond_usd = 0.0
            local_rows = 0  # in-process scoring: no model calls, no cache
            _ledger.upsert_ledger(
                prep.paths,
                [ledger_row(run_id, "grade", cond.id, "(verifiable)", rows, None)],
            )
        else:
            task = build_judge_task(
                pending,
                prep.items_by_id,
                cond,
                prep.rubric_templates[cond.rubric_name],
                prep.config.study,
                prep.config.cache,
                batch=prep.plan.batch,
                cache_schedule=(
                    prep.config.budget.cache_schedule != "off" and prep.plan.batch is None
                ),
            )
            try:
                logs = inspect_ai.eval(
                    task,
                    model=factory(cond.grader_model, "grade"),
                    display=resolve_display(display),
                    log_dir=str(prep.paths.logs_dir("grade", cond.id)),
                    log_format="eval",
                    fail_on_error=False,
                    retry_on_error=1,
                    tags=["itemeval", "grade"],
                    metadata={
                        "itemeval_run_id": run_id,
                        "itemeval_study": prep.config.study,
                        "itemeval_condition_id": cond.id,
                    },
                )
                log = logs[0]
            except Exception as e:
                reports.append(
                    ConditionRunReport(
                        condition_id=cond.id,
                        slug=cond.slug,
                        status="error",
                        items_run=len(pending),
                        rows_written=0,
                        errors=0,
                        usd=None,
                        log_file=None,
                        message=f"{type(e).__name__}: {e}",
                    )
                )
                continue
            rows = _judge_rows(prep, cond, pending, log, run_id)
            local_rows = local_cache_rows(rows)
            judge_models.append(cond.grader_model)
            repeated_prefix_calls += int(len(pending) - pending["item_id"].nunique())
            endpoints_effective[cond.id] = endpoint_info(log, cond.grader_model)
            log_file = rel_to_study(prep.paths, log.location)
            usd_vals = [r["usd"] for r in rows if r["usd"] is not None]
            cond_usd = sum(usd_vals) if usd_vals else None
            _logs.upsert_log_index(
                prep.paths,
                [
                    log_index_row(
                        log, prep.paths, run_id, "grade", cond.id, cond.grader_model, cond_usd
                    )
                ],
            )
            _ledger.upsert_ledger(
                prep.paths,
                [ledger_row(run_id, "grade", cond.id, cond.grader_model, rows, prep.plan.batch)],
            )

        n = _gradings.upsert_gradings(prep.paths, rows)
        rows_written += n
        total_usd += cond_usd or 0.0
        n_parse_fail = sum(1 for r in rows if not r["parse_ok"] and r["error"] is None)
        parse_failures += n_parse_fail
        reports.append(
            ConditionRunReport(
                condition_id=cond.id,
                slug=cond.slug,
                status="run",
                items_run=len(pending),
                rows_written=n,
                errors=sum(1 for r in rows if r["error"] is not None),
                usd=cond_usd,
                log_file=log_file,
                local_cache_rows=local_rows,
                **cache_columns(rows),
            )
        )

    if endpoints_effective:
        finalize_manifest(manifest_path, endpoints_effective=endpoints_effective)

    run_reports = [r for r in reports if r.status == "run"]
    scheduled = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None
    hints = [
        h
        for h in (
            detect_cache_zero_reads(
                scheduled=scheduled,
                repeated_prefix_calls=repeated_prefix_calls,
                cache_read_tokens=sum(r.cache_read_tokens for r in run_reports),
                real_provider=any(not is_mock_model(m) for m in judge_models),
            ),
            detect_empty_solutions(empty_total, empty_skipped, on_empty, empty_stop_reasons),
            detect_unpriced_models(
                sorted({m for m in judge_models if lookup_price(prep.pricing, m) is None})
            ),
        )
        if h is not None
    ]
    return GradeResult(
        run_id=run_id,
        study=prep.config.study,
        conditions=reports,
        rows_written=rows_written,
        parse_failures=parse_failures,
        total_usd=total_usd,
        manifest_path=rel_to_study(prep.paths, manifest_path),
        on_empty=on_empty,
        empty_total=empty_total,
        empty_skipped=empty_skipped,
        empty_stop_reasons=empty_stop_reasons,
        hints=hints,
        datasets=dataset_provenance(prep.datasets),
        local_cache_rows=sum(r.local_cache_rows for r in reports),
        local_cache_dir=(local_cache_dir() if any(r.local_cache_rows for r in reports) else None),
    )
