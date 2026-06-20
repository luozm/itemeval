"""Grade-stage orchestrator: verifiable in-process, judge via inspect tasks."""

from typing import TYPE_CHECKING, Any

import inspect_ai  # the materialization pre-pass runs its own eval (sequential, pre-judge)
from pydantic import BaseModel, ConfigDict, Field

from itemeval._endpoints import cache_provider_of, model_args_for
from itemeval._errors import StoreError
from itemeval._harvest import HarvestReport
from itemeval._hints import (
    Hint,
    detect_cache_zero_reads,
    detect_empty_materialized_rubrics,
    detect_empty_solutions,
    detect_openrouter_unpinned_cache,
    detect_unpriced_models,
)
from itemeval._manifest import build_manifest, finalize_manifest, write_manifest
from itemeval._mockmodels import is_mock_model
from itemeval._modelsample import ModelSampleResult
from itemeval.adapters._base import DatasetProvenance, dataset_provenance
from itemeval.budget._gate import GateResult
from itemeval.budget._pricing import (
    PricingProvenance,
    batch_providers_used,
    lookup_price,
    provider_of,
)
from itemeval.budget._routing import NativeRoute
from itemeval._classify import labeled
from itemeval._mockmodels import resolve_model
from itemeval._identity import invocation_handle, resolve_identity
from itemeval._experiments import update_experiment_index
from itemeval._util import sha256_hex, utc_now_iso
from itemeval.design._grid import GradeCondition
from itemeval.generate._run import (
    ConditionRunReport,
    ModelFactory,
    cache_columns,
    endpoint_info,
    enforce_budget_cap,
    eval_error_message,
    ledger_row,
    local_cache_dir,
    local_cache_rows,
    log_index_row,
    matches_filter,
    max_tasks_for,
    resolve_display,
    run_condition_evals,
    sum_usage,
    usage_columns,
    usd_for_usage,
)
from itemeval.grade._judge import build_judge_task
from itemeval.grade._materialize import build_materialize_task, materialize_id
from itemeval.grade._parse import parse_judge_output
from itemeval.grade._verifiable import VERIFIABLE_SCORERS
from itemeval.store import _gradings, _ledger, _logs, _materialized, _solutions
from itemeval.store._base import rel_to_study
from itemeval.store._solutions import empty_solution_mask

if TYPE_CHECKING:
    import pandas as pd

    from itemeval._prepare import PreparedStudy


class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Run identity (recovery-run-identity), as on GenerateResult.
    experiment_id: str
    attempt: int
    run_kind: str  # "recovery" | "new"
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
    # Two-stage rubric materialization (0 unless a `rubrics:` materialize ran):
    materialized_rubrics: int = 0  # rubrics materialized this run (fresh model calls)
    materialized_reused: int = 0  # reused from the frozen artifact store ($0)
    materialize_usd: float = 0.0  # spend on the materialization pre-pass
    materialize_empty: int = 0  # materializations that returned no rubric text
    materialize_model: "str | None" = None  # the materializer model (when one ran)
    hints: list[Hint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # drift warnings — never block
    datasets: list[DatasetProvenance] = Field(default_factory=list)
    model_sample: "ModelSampleResult | None" = None  # set when solvers.sample drew the models
    # Local response-cache reuse (Law 1: reuse announced as loudly as fetching):
    local_cache_rows: int = 0
    local_cache_dir: "str | None" = None  # set when local_cache_rows > 0
    # Provider batch mode (Law 1: provider-side job creation is announced):
    batch: bool = False
    batch_providers: list[str] = Field(default_factory=list)  # ran via a batch API
    # Native batch routing (Law 1: serving-endpoint change is announced):
    routed_models: list[NativeRoute] = Field(default_factory=list)
    # Wave (re-observation) provenance; 0/None/0 for ordinary runs:
    wave: int = 0
    wave_label: "str | None" = None
    epoch_offset: int = 0
    # Filled by the CLI for `--json` parity (Python callers compute their own):
    pricing: "PricingProvenance | None" = None
    estimate_usd: "float | None" = None  # remaining figure (gate input)
    expected_estimate_usd: "float | None" = None  # calibrated remaining; informational
    rows_replaced: "int | None" = None  # existing rows this run planned to overwrite
    gate: "GateResult | None" = None
    # Crash recovery (recoverable-harvest): rows projected from a prior run's
    # `.eval` into the store before this run, when the CLI auto-harvested first.
    harvested: "HarvestReport | None" = None


def _base_row(
    prep: "PreparedStudy", cond: GradeCondition, experiment_id: str, attempt: int, sol_row, now: str
) -> "dict[str, Any]":
    wave_label = getattr(sol_row, "wave_label", None)
    return {
        "study": prep.config.study,
        "experiment_id": experiment_id,
        "attempt": attempt,
        "grade_condition_id": cond.id,
        "grade_condition_slug": cond.slug,
        "gen_condition_id": sol_row.condition_id,
        "item_id": sol_row.item_id,
        "epoch": int(sol_row.epoch),
        # inherited from the graded solution row (NaN -> default 0/null)
        "wave": int(getattr(sol_row, "wave", 0) or 0),
        "wave_label": wave_label if isinstance(wave_label, str) else None,
        "grade_kind": cond.kind,
        "grader_name": cond.grader_name,
        "grader_model": cond.grader_model,
        "rubric_name": cond.rubric_name,
        "rubric_hash": cond.rubric_hash,
        "scorer_name": cond.scorer,
        "created_at": now,
    }


def _verifiable_rows(
    prep: "PreparedStudy",
    cond: GradeCondition,
    pending: "pd.DataFrame",
    experiment_id: str,
    attempt: int,
) -> "list[dict]":
    scorer = VERIFIABLE_SCORERS[cond.scorer]
    now = utc_now_iso()
    rows = []
    for sol_row in pending.itertuples():
        result = scorer(sol_row.solution, prep.items_by_id[sol_row.item_id])
        rows.append(
            {
                **_base_row(prep, cond, experiment_id, attempt, sol_row, now),
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
    prep: "PreparedStudy",
    cond: GradeCondition,
    pending: "pd.DataFrame",
    log,
    experiment_id: str,
    attempt: int,
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
                **_base_row(prep, cond, experiment_id, attempt, sol_row, now),
                **parse_cols,
                "judge_completion": completion,
                "error": error,
                **usage_columns(usage),
                "usd": usd_for_usage(
                    prep.pricing,
                    cond.grader_model,
                    usage,
                    prep.plan.batch,
                    exec_model=prep.native_routes.get(cond.grader_model, cond.grader_model),
                ),
                "latency_s": sample.total_time,
                "log_file": log_file,
            }
        )
    return rows


def persist_grade_condition(
    prep: "PreparedStudy",
    cond: GradeCondition,
    pending: "pd.DataFrame",
    log,
    experiment_id: str,
    attempt: int,
) -> "tuple[list[dict], int, float | None]":
    """Build judge gradings rows from one grade log and write them + the log
    index + ledger to the durable stores. The single home for the judge harvest
    write — shared by the live run (Phase 3) and disk harvest (`_harvest.py`).
    Returns ``(rows, rows_written, cond_usd)``. Verifiable conditions never go
    through here (no model call → no `.eval` to recover)."""
    exec_grader = prep.native_routes.get(cond.grader_model, cond.grader_model)
    rows = _judge_rows(prep, cond, pending, log, experiment_id, attempt)
    n = _gradings.upsert_gradings(prep.paths, rows)
    usd_vals = [r["usd"] for r in rows if r["usd"] is not None]
    cond_usd = sum(usd_vals) if usd_vals else None
    _logs.upsert_log_index(
        prep.paths,
        [
            log_index_row(
                log,
                prep.paths,
                experiment_id,
                attempt,
                "grade",
                cond.id,
                cond.grader_model,
                cond_usd,
            )
        ],
    )
    _ledger.upsert_ledger(
        prep.paths,
        [
            ledger_row(
                experiment_id,
                attempt,
                "grade",
                cond.id,
                cond.grader_model,
                rows,
                prep.plan.batch,
                exec_model=exec_grader,
            )
        ],
    )
    return rows, n, cond_usd


def _materialize_rubrics(
    prep: "PreparedStudy",
    selected: "list[GradeCondition]",
    experiment_id: str,
    attempt: int,
    factory: ModelFactory,
    display: "str | None",
    batch: "bool | int | None",
) -> "tuple[dict[str, dict[str, str]], dict[str, Any]]":
    """Stage-1 pre-pass: materialize (and freeze) one rubric per item for every
    selected materializing rubric, once per (rubric, materializer) — shared
    across graders, solutions, replications, and resumed runs. Returns
    ({rubric_name: {item_id: rubric_text}}, stats). Already-stored rubrics are
    reused at $0; only un-materialized items make a model call."""
    rubrics: "dict[str, GradeCondition]" = {}
    for cond in selected:
        if cond.kind == "judge" and cond.materialize_model and cond.rubric_name not in rubrics:
            rubrics[cond.rubric_name] = cond
    texts: "dict[str, dict[str, str]]" = {}
    stats: "dict[str, Any]" = {
        "materialized": 0,
        "reused": 0,
        "usd": 0.0,
        "empty": 0,
        "model": None,
    }
    if not rubrics:
        return texts, stats
    items = prep.items_effective
    existing = _materialized.read_materialized(prep.paths)
    ledger_rows = []
    for rubric_name, cond in rubrics.items():
        build_template = prep.build_templates[rubric_name]
        spec = prep.config.rubrics[rubric_name].materialize
        model = cond.materialize_model
        stats["model"] = model
        mid = materialize_id(build_template, model)
        frozen = _materialized.stored_texts(existing, mid)
        texts.setdefault(rubric_name, {}).update(frozen)
        for txt in frozen.values():
            stats["reused"] += 1
            stats["empty"] += 0 if txt else 1
        pending = [it for it in items if it.id not in frozen]
        if not pending:
            continue
        exec_model = prep.native_routes.get(model, model)
        task = build_materialize_task(
            pending, build_template, spec, prep.config.study, rubric_name, prep.config.cache, batch
        )
        logs = inspect_ai.eval(
            task,
            model=factory(exec_model, "materialize", model_args_for(exec_model)),
            display=resolve_display(display),
            log_dir=str(prep.paths.logs_stage_dir("materialize")),
            log_format="eval",
            fail_on_error=False,
            retry_on_error=1,
            tags=["itemeval", "materialize"],
            metadata={
                "itemeval_run_id": invocation_handle(experiment_id, attempt),
                "itemeval_experiment_id": experiment_id,
                "itemeval_attempt": attempt,
                "itemeval_study": prep.config.study,
                "itemeval_rubric": rubric_name,
            },
        )
        log = logs[0]
        now = utc_now_iso()
        rows = []
        for sample in log.samples or []:
            iid = str((sample.metadata or {}).get("item_id"))
            error = sample.error.message if sample.error else None
            completion = (
                sample.output.completion
                if (error is None and sample.output and sample.output.completion)
                else None
            )
            usage = sum_usage(sample)
            usd = usd_for_usage(prep.pricing, model, usage, batch, exec_model=exec_model)
            rows.append(
                {
                    "materialize_id": mid,
                    "rubric_name": rubric_name,
                    "item_id": iid,
                    "materializer_model": model,
                    "build_template_hash": build_template.hash12,
                    "rubric_text": completion,
                    "rubric_hash": sha256_hex(completion.encode("utf-8"))[:12]
                    if completion
                    else None,
                    "usd": usd,
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "error": error,
                    "experiment_id": experiment_id,
                    "attempt": attempt,
                    "created_at": now,
                }
            )
            if error is None:  # empty completion (no error) is a valid, frozen ""
                resolved = completion or ""
                texts.setdefault(rubric_name, {})[iid] = resolved
                stats["materialized"] += 1
                stats["empty"] += 0 if resolved else 1
            stats["usd"] += usd or 0.0
        _materialized.upsert_materialized(prep.paths, rows)
        ledger_rows.append(
            ledger_row(
                experiment_id,
                attempt,
                "grade",
                f"materialize:{rubric_name}",
                model,
                rows,
                batch,
                exec_model=exec_model,
            )
        )
    if ledger_rows:
        _ledger.upsert_ledger(prep.paths, ledger_rows)
    return texts, stats


def run_grade(
    prep: "PreparedStudy",
    *,
    new_run: bool = False,
    force: bool = False,
    condition_filter: "list[str] | None" = None,
    graders: "list[str] | None" = None,
    rubrics: "list[str] | None" = None,
    display: "str | None" = None,
    model_factory: "ModelFactory | None" = None,
    estimate_usd: "float | None" = None,
    estimate_full_usd: "float | None" = None,
    max_usd: "float | None" = None,
    wave: "str | None" = None,
) -> GradeResult:
    enforce_budget_cap(prep, "grade", max_usd, force, wave=wave)
    ident = resolve_identity(prep.config, prep.paths, "grade", new_run=new_run)
    prep.paths.ensure()
    solutions_df = _solutions.read_solutions(prep.paths)
    if solutions_df.empty:
        raise StoreError("no solutions in store; run generate first")

    # Policy scope: effective items, epochs within the effective replications —
    # or, with --wave, exactly that wave's epoch block. Also scoped to the
    # current gen grid: solutions whose gen-condition left the grid (a config
    # change rehashed the ids) are orphans, never (re-)graded — the same scope
    # the estimator and `status` use. Without this, grade would judge every
    # stored roster for these items (silent overspend + cross-roster mixing).
    effective_ids = {it.id for it in prep.items_effective}
    grid_gen_ids = {c.id for c in prep.grid.generate}
    if wave is not None:
        wave_rows = solutions_df[solutions_df["wave_label"] == wave]
        if wave_rows.empty:
            raise StoreError(f"no solutions for wave '{wave}'; run generate --wave {wave} first")
        wave_num = int(wave_rows["wave"].iloc[0])
        scoped = wave_rows[
            wave_rows["item_id"].isin(effective_ids) & wave_rows["condition_id"].isin(grid_gen_ids)
        ]
    else:
        wave_num = 0
        scoped = solutions_df[
            solutions_df["item_id"].isin(effective_ids)
            & (solutions_df["epoch"].astype(int) <= prep.plan.replications)
            & solutions_df["condition_id"].isin(grid_gen_ids)
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

    from itemeval._driftcheck import endpoint_drift_warnings, grade_drift_warnings

    drift_warnings = grade_drift_warnings(
        prep.grid, _gradings.read_gradings(prep.paths)
    ) + endpoint_drift_warnings(
        [c.grader_model for c in selected if c.grader_model], prep.paths.manifests_dir
    )

    manifest = build_manifest(
        prep,
        "grade",
        ident.experiment_id,
        ident.attempt,
        [c.id for c in selected],
        estimate_usd,
        estimate_full_usd,
        wave=wave_num,
        wave_label=wave,
        epoch_offset=wave_num * prep.plan.replications,
    )
    manifest_path = write_manifest(manifest, prep.paths)
    update_experiment_index(prep.paths, manifest)  # attempt rollup (recovery-run-identity W3)

    reports_by_cond: dict[str, ConditionRunReport] = {}
    endpoints_effective: dict[str, Any] = {}
    rows_written = 0
    parse_failures = 0
    total_usd = 0.0
    judge_models: list[str] = []  # grader models of judge conditions that ran
    unpinned_cached: list[str] = []  # openrouter/anthropic judges cached without routing
    repeated_prefix_calls = 0  # judge calls beyond each same-item group's leader
    # One truth value for "cache scheduling active" (matches generate's).
    scheduled = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None
    factory = model_factory or resolve_model
    # Planning snapshot: pending is filtered per cond.id, so a single read is
    # correct even though verifiable conditions upsert gradings during Phase 1.
    gradings_df = _gradings.read_gradings(prep.paths)

    # Stage-1 pre-pass: freeze a per-item rubric for every materializing rubric
    # before grading. Its spend rides the single grade money gate (estimated in
    # budget/_estimator.py); reuse from the artifact store is free.
    rubric_texts_by_rubric, mat_stats = _materialize_rubrics(
        prep, selected, ident.experiment_id, ident.attempt, factory, display, prep.plan.batch
    )
    total_usd += float(mat_stats["usd"])

    def finalize(cond, rows, *, n, items_run, log_file, cond_usd, local_rows) -> None:
        """Shared report tail for verifiable + judge (the write already happened:
        verifiable upserts inline, judge via persist_grade_condition)."""
        nonlocal rows_written, total_usd, parse_failures
        rows_written += n
        total_usd += cond_usd or 0.0
        parse_failures += sum(1 for r in rows if not r["parse_ok"] and r["error"] is None)
        reports_by_cond[cond.id] = ConditionRunReport(
            condition_id=cond.id,
            slug=cond.slug,
            status="run",
            items_run=items_run,
            rows_written=n,
            errors=sum(1 for r in rows if r["error"] is not None),
            usd=cond_usd,
            log_file=log_file,
            local_cache_rows=local_rows,
            **cache_columns(rows),
        )

    # Phase 1: verifiable conditions score in-process now (no model call); judge
    # conditions get a task with its own model for the shared parallel eval.
    planned: list[tuple[GradeCondition, Any, str, Any]] = []
    for cond in selected:
        pending = _gradings.pending_solutions(
            scoped, gradings_df, cond.id, force, include_empty=include_empty
        )
        if pending.empty:
            reports_by_cond[cond.id] = ConditionRunReport(
                condition_id=cond.id,
                slug=cond.slug,
                status="skipped",
                items_run=0,
                rows_written=0,
                errors=0,
                usd=None,
                log_file=None,
            )
            continue
        if cond.kind == "verifiable":
            rows = _verifiable_rows(prep, cond, pending, ident.experiment_id, ident.attempt)
            n = _gradings.upsert_gradings(prep.paths, rows)
            _ledger.upsert_ledger(
                prep.paths,
                [
                    ledger_row(
                        ident.experiment_id,
                        ident.attempt,
                        "grade",
                        cond.id,
                        "(verifiable)",
                        rows,
                        None,
                    )
                ],
            )
            finalize(
                cond, rows, n=n, items_run=len(pending), log_file=None, cond_usd=0.0, local_rows=0
            )
            continue
        # Judge: native batch routing on the grader id (sampled id stays
        # recorded); the model rides on the Task so all judges run in one eval.
        grader_routing = prep.config.grader_spec(cond.grader_name).provider_routing
        exec_grader = prep.native_routes.get(cond.grader_model, cond.grader_model)
        task = build_judge_task(
            pending,
            prep.items_by_id,
            cond,
            prep.rubric_templates[cond.rubric_name],
            prep.config.study,
            prep.config.cache,
            batch=prep.plan.batch,
            cache_schedule=scheduled,
            rubric_texts=rubric_texts_by_rubric.get(cond.rubric_name),
            attempt_timeout=prep.config.grader_spec(cond.grader_name).attempt_timeout,
        )
        try:
            task.model = factory(
                exec_grader,
                "grade",
                model_args_for(
                    exec_grader,
                    provider_routing=grader_routing,
                    cache_scheduling=scheduled,
                    study=prep.config.study,
                    condition_id=cond.id,
                ),
            )
        except Exception as e:  # model construction failure: isolate per cond
            reports_by_cond[cond.id] = ConditionRunReport(
                condition_id=cond.id,
                slug=cond.slug,
                status="error",
                items_run=len(pending),
                rows_written=0,
                errors=0,
                usd=None,
                log_file=None,
                message=labeled(f"{type(e).__name__}: {e}", exc=e),
            )
            continue
        planned.append((cond, pending, exec_grader, task))

    # Phase 2: one eval over all judge tasks — graders run concurrently.
    log_by_cond, fatal = run_condition_evals(
        [task for _, _, _, task in planned],
        stage="grade",
        experiment_id=ident.experiment_id,
        attempt=ident.attempt,
        study=prep.config.study,
        display=display,
        log_dir=str(prep.paths.logs_stage_dir("grade")),
        max_tasks=max_tasks_for([ex for _, _, ex, _ in planned]),
    )

    # Phase 3: harvest each judge condition from its log (mapped by metadata).
    for cond, pending, exec_grader, _task in planned:
        log = log_by_cond.get(cond.id)
        if fatal is not None or log is None or log.status != "success":
            reports_by_cond[cond.id] = ConditionRunReport(
                condition_id=cond.id,
                slug=cond.slug,
                status="error",
                items_run=len(pending),
                rows_written=0,
                errors=0,
                usd=None,
                log_file=None,
                message=eval_error_message(log, fatal),
            )
            continue
        rows, n, cond_usd = persist_grade_condition(
            prep, cond, pending, log, ident.experiment_id, ident.attempt
        )
        local_rows = local_cache_rows(rows)
        judge_models.append(cond.grader_model)
        # Judge tasks always request cache_prompt="auto" (markers on), so an
        # unpinned openrouter/anthropic judge is a cached-but-routable run —
        # unless it was routed to its native API, where the call never touches
        # OpenRouter and the OpenRouter-cache caveat does not apply.
        grader_routing = prep.config.grader_spec(cond.grader_name).provider_routing
        if (
            grader_routing is None
            and cond.grader_model not in prep.native_routes
            and provider_of(cond.grader_model) == "openrouter"
            and cache_provider_of(cond.grader_model) == "anthropic"
        ):
            unpinned_cached.append(cond.grader_model)
        repeated_prefix_calls += int(len(pending) - pending["item_id"].nunique())
        endpoints_effective[cond.id] = endpoint_info(log, cond.grader_model, exec_grader)
        finalize(
            cond,
            rows,
            n=n,
            items_run=len(pending),
            log_file=rel_to_study(prep.paths, log.location),
            cond_usd=cond_usd,
            local_rows=local_rows,
        )

    # Reassemble in selected order (skips + verifiable + judge) for the summary.
    reports = [reports_by_cond[c.id] for c in selected]

    if endpoints_effective:
        finalize_manifest(manifest_path, endpoints_effective=endpoints_effective)

    run_reports = [r for r in reports if r.status == "run"]
    hints = [
        h
        for h in (
            detect_cache_zero_reads(
                scheduled=scheduled,
                repeated_prefix_calls=repeated_prefix_calls,
                cache_read_tokens=sum(r.cache_read_tokens for r in run_reports),
                real_provider=any(not is_mock_model(m) for m in judge_models),
            ),
            detect_openrouter_unpinned_cache(sorted(set(unpinned_cached))),
            detect_empty_solutions(empty_total, empty_skipped, on_empty, empty_stop_reasons),
            detect_empty_materialized_rubrics(int(mat_stats["empty"]), mat_stats["model"]),
            detect_unpriced_models(
                sorted({m for m in judge_models if lookup_price(prep.pricing, m) is None})
            ),
        )
        if h is not None
    ]
    return GradeResult(
        experiment_id=ident.experiment_id,
        attempt=ident.attempt,
        run_kind=ident.run_kind,
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
        materialized_rubrics=int(mat_stats["materialized"]),
        materialized_reused=int(mat_stats["reused"]),
        materialize_usd=float(mat_stats["usd"]),
        materialize_empty=int(mat_stats["empty"]),
        materialize_model=mat_stats["model"],
        hints=hints,
        warnings=drift_warnings,
        datasets=dataset_provenance(prep.datasets),
        model_sample=prep.model_sample,
        local_cache_rows=sum(r.local_cache_rows for r in reports),
        local_cache_dir=(local_cache_dir() if any(r.local_cache_rows for r in reports) else None),
        batch=prep.plan.batch is not None,
        batch_providers=(
            batch_providers_used([prep.native_routes.get(m, m) for m in judge_models])
            if prep.plan.batch is not None
            else []
        ),
        routed_models=[
            NativeRoute(
                sampled=m,
                execution=prep.native_routes[m],
                provider=provider_of(prep.native_routes[m]),
            )
            for m in dict.fromkeys(judge_models)
            if m in prep.native_routes
        ],
        wave=wave_num,
        wave_label=wave,
        epoch_offset=wave_num * prep.plan.replications,
    )
