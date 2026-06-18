"""Generate-stage orchestrator: per-condition inspect evals -> solutions store."""

import os
from functools import reduce
from typing import TYPE_CHECKING, Any, Callable, Literal

import inspect_ai
from pydantic import BaseModel, ConfigDict, Field

from itemeval._endpoints import cache_provider_of, model_args_for
from itemeval._hints import (
    Hint,
    detect_cache_zero_reads,
    detect_openrouter_unpinned_cache,
    detect_unpriced_models,
)
from itemeval._manifest import build_manifest, finalize_manifest, write_manifest
from itemeval._mockmodels import is_mock_model, resolve_model
from itemeval._modelsample import ModelSampleResult
from itemeval.adapters._base import DatasetProvenance, dataset_provenance
from itemeval._util import new_run_id, utc_now_iso
from itemeval.budget._gate import GateResult
from itemeval.budget._pricing import (
    BATCH_PROVIDERS,
    PricingProvenance,
    batch_providers_used,
    cost_usd,
    lookup_price,
    provider_of,
)
from itemeval.budget._routing import NativeRoute
from itemeval.design._grid import GenCondition
from itemeval.generate._params import extract_effective_params
from itemeval.generate._task import build_generate_task
from itemeval.store import _ledger, _logs, _solutions
from itemeval.store._base import rel_to_study
from itemeval.store._items import upsert_items

if TYPE_CHECKING:
    from inspect_ai.log import EvalLog, EvalSample
    from inspect_ai.model import ModelUsage

    from itemeval._prepare import PreparedStudy

# (model_id, stage, model_args) -> str | Model; model_args carries per-condition
# request extras (provider routing, cache keys) built by _endpoints.model_args_for.
ModelFactory = Callable[[str, str, "dict[str, Any]"], Any]


def enforce_budget_cap(
    prep: "PreparedStudy",
    stage: str,
    max_usd: "float | None",
    force: bool,
    wave: "str | None" = None,
) -> None:
    """Raise BudgetExceededError before any API call when a cap is exceeded.

    The cap is min(max_usd argument, config budget.max_usd); the projection
    compared is the stage's *remaining* figure (what this run can spend) —
    matching the CLI gate's semantics. Never prompts (Law 3).
    """
    from itemeval._errors import BudgetExceededError

    caps = [c for c in (max_usd, prep.config.budget.max_usd) if c is not None]
    if not caps:
        return
    from itemeval.budget._estimator import estimate_study

    est = estimate_study(prep, force=force, wave=wave)
    stage_est = est.generate if stage == "generate" else est.grade
    cap = min(caps)
    if stage_est.remaining_usd > cap:
        source = "max_usd argument" if cap == max_usd else "budget.max_usd"
        raise BudgetExceededError(
            f"projected {stage} cost ${stage_est.remaining_usd:.2f} (remaining; "
            f"full grid ${stage_est.usd:.2f}) exceeds {source} ${cap:.2f} — "
            "no API calls were made"
        )


class ConditionRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str
    slug: str
    status: Literal["run", "skipped", "error"]
    items_run: int
    rows_written: int
    errors: int  # samples that errored in this run
    usd: float | None  # None when model unpriced
    log_file: str | None  # relative to study_dir
    message: str | None = None  # eval-level error detail
    # Provider prompt-cache activity (0 when the provider reported none):
    cache_read_tokens: int = 0  # input tokens served from the provider cache
    cache_write_tokens: int = 0  # input tokens written to the provider cache
    cache_hit_rows: int = 0  # rows with cache_read_tokens > 0
    # Error-free rows answered from inspect's local response cache ($0, no usage):
    local_cache_rows: int = 0


class GenerateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    study: str
    conditions: list[ConditionRunReport]
    rows_written: int
    total_usd: float
    manifest_path: str
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
    # Native batch routing (Law 1: serving-endpoint change is announced): the
    # routes applied this run (empty unless budget.prefer_native_batch engaged).
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


def resolve_display(display: "str | None") -> str:
    """Resolve the inspect display mode for a generate/grade eval.

    itemeval defaults to inspect's "rich" live progress — inline progress bars,
    no full-screen takeover repeated across the per-condition loop. Precedence:
    an explicit value wins, then the INSPECT_DISPLAY env var, then "rich".
    inspect itself degrades the chosen mode off-TTY/Jupyter/background-thread.
    """
    return display or os.environ.get("INSPECT_DISPLAY") or "rich"


def matches_filter(cond_id: str, slug: str, filters: "list[str] | None") -> bool:
    if not filters:
        return True
    return any(cond_id == f or cond_id.startswith(f) or slug == f for f in filters)


def sum_usage(sample: "EvalSample") -> "ModelUsage | None":
    usages = list((sample.model_usage or {}).values())
    if not usages:
        return None
    return reduce(lambda a, b: a + b, usages)


def usd_for_usage(
    pricing,
    model: str,
    usage: "ModelUsage | None",
    batch: "bool | int | None",
    exec_model: "str | None" = None,
) -> "float | None":
    """None = model unpriced. Missing usage with a known price = $0 (cache hit).

    Price is read under the sampled `model` (the roster id the table carries);
    the batch discount applies when the *execution* provider (`exec_model` — the
    native id under native batch routing, else `model`) is a batch provider."""
    price = lookup_price(pricing, model)
    if price is None:
        return None
    if usage is None:
        return 0.0
    value = cost_usd(
        price,
        usage.input_tokens,
        usage.output_tokens,
        usage.input_tokens_cache_read,
        usage.input_tokens_cache_write,
        model=model,
    )
    if batch is not None and provider_of(exec_model or model) in BATCH_PROVIDERS:
        value *= 0.5  # documented approximation; provider invoices authoritative
    return value


def usage_columns(usage: "ModelUsage | None") -> "dict[str, Any]":
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cache_read_tokens": usage.input_tokens_cache_read,
        "cache_write_tokens": usage.input_tokens_cache_write,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def log_index_row(
    log: "EvalLog",
    paths,
    run_id: str,
    stage: str,
    condition_id: str,
    model: str,
    usd: "float | None",
) -> dict:
    stats_usage = list(log.stats.model_usage.values()) if log.stats else []
    total = reduce(lambda a, b: a + b, stats_usage) if stats_usage else None
    return {
        "log_file": rel_to_study(paths, log.location),
        "run_id": run_id,
        "stage": stage,
        "condition_id": condition_id,
        "task_name": log.eval.task,
        "model": model,
        "status": log.status,
        "started_at": log.stats.started_at if log.stats else None,
        "completed_at": log.stats.completed_at if log.stats else None,
        "total_samples": log.results.total_samples if log.results else None,
        "completed_samples": log.results.completed_samples if log.results else None,
        "input_tokens": total.input_tokens if total else None,
        "output_tokens": total.output_tokens if total else None,
        "total_tokens": total.total_tokens if total else None,
        "usd": usd,
        "created_at": utc_now_iso(),
    }


def ledger_row(
    run_id: str,
    stage: str,
    condition_id: str,
    model: str,
    rows: "list[dict]",
    batch: "bool | int | None",
    epoch_offset: int = 0,
    exec_model: "str | None" = None,
) -> dict:
    def total(col: str) -> "int | None":
        vals = [r[col] for r in rows if r.get(col) is not None]
        return sum(vals) if vals else None

    usd_vals = [r["usd"] for r in rows if r.get("usd") is not None]
    # `model` stays the sampled/scientific id; `provider` is the billing provider
    # — the native one under native batch routing (per-provider spend stays honest).
    return {
        "run_id": run_id,
        "stage": stage,
        "condition_id": condition_id,
        "model": model,
        "provider": provider_of(exec_model or model),
        "calls": len(rows),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "total_tokens": total("total_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "usd": sum(usd_vals) if usd_vals else 0.0,
        "priced": bool(usd_vals),
        "batch": batch is not None,
        "created_at": utc_now_iso(),
        "epoch_offset": epoch_offset,
    }


def cache_columns(rows: "list[dict]") -> "dict[str, int]":
    """Provider prompt-cache totals over a condition's rows (Phase 0 observability)."""
    return {
        "cache_read_tokens": sum(r["cache_read_tokens"] or 0 for r in rows),
        "cache_write_tokens": sum(r["cache_write_tokens"] or 0 for r in rows),
        "cache_hit_rows": sum(1 for r in rows if (r["cache_read_tokens"] or 0) > 0),
    }


def local_cache_rows(rows: "list[dict]") -> int:
    """Error-free rows with no usage object: answered from inspect's local
    response cache (the same signal usd_for_usage prices at $0)."""
    return sum(1 for r in rows if r["error"] is None and r["total_tokens"] is None)


def local_cache_dir() -> str:
    from inspect_ai.model import cache_path

    return str(cache_path())


def endpoint_info(log: "EvalLog", model: str, exec_model: "str | None" = None) -> "dict[str, Any]":
    """Resolved endpoint for a condition's eval: which provider/account/version
    actually answered. `base_url` is None on the provider's default endpoint;
    a non-null value means traffic was routed elsewhere (Azure/proxy/gateway).
    `served_model` is the provider-returned snapshot id (e.g. a dated version).

    `model` is the sampled/scientific id; `execution_model` is the id the calls
    actually ran on (the native id under native batch routing, else the same),
    and `routed` flags the difference. Upstream detection keys on the execution
    provider, so a routed-native call (no OpenRouter hop) is handled correctly.

    For openrouter/* models, `upstream` is the host OpenRouter routed to —
    the response's `provider` field ("Anthropic", "Amazon Bedrock", ...),
    i.e. the thing `provider_routing` pins and the host whose caching/pricing
    rules applied. Distinct values across the run's calls are comma-joined
    (mixed routing is itself worth seeing); None when no recorded response
    carried the field (e.g. mock models)."""
    exec_model = exec_model or model
    base_url = getattr(log.eval, "model_base_url", None)
    served_model = None
    for sample in log.samples or []:
        if sample.output and sample.output.model:
            served_model = sample.output.model
            break
    info: dict[str, Any] = {
        "provider": provider_of(model),
        "base_url": base_url,
        "served_model": served_model,
        "execution_model": exec_model,
        "routed": exec_model != model,
    }
    if provider_of(exec_model) == "openrouter":
        seen: set[str] = set()
        for sample in log.samples or []:
            for ev in sample.events or []:
                call = getattr(ev, "call", None)
                resp = getattr(call, "response", None) or {}
                up = resp.get("provider")
                if isinstance(up, str) and up:
                    seen.add(up)
        info["upstream"] = ", ".join(sorted(seen)) if seen else None
    return info


def rows_from_generate_log(
    log: "EvalLog",
    cond: GenCondition,
    prep: "PreparedStudy",
    run_id: str,
    epoch_offset: int = 0,
    wave: int = 0,
    wave_label: "str | None" = None,
) -> "list[dict]":
    rows = []
    now = utc_now_iso()
    log_file = rel_to_study(prep.paths, log.location)
    p = cond.gen_params
    for sample in log.samples or []:
        item_id = str(sample.id)
        origin = prep.origins[item_id]
        usage = sum_usage(sample)
        error = sample.error.message if sample.error else None
        solution = (
            sample.output.completion
            if (error is None and sample.output and sample.output.completion)
            else None
        )
        eff = extract_effective_params(sample, p)
        rows.append(
            {
                "study": prep.config.study,
                "run_id": run_id,
                "condition_id": cond.id,
                "condition_slug": cond.slug,
                "item_id": item_id,
                "dataset_id": origin.dataset_id,
                "dataset_revision": origin.revision,
                "epoch": int(sample.epoch) + epoch_offset,
                "wave": wave,
                "wave_label": wave_label,
                "model": cond.model,
                "prompt_name": cond.prompt_name,
                "prompt_hash": cond.prompt_hash,
                "model_config_name": cond.model_config_name,
                "temperature_requested": p.temperature,
                "temperature_effective": eff.temperature,
                "top_p_requested": p.top_p,
                "top_p_effective": eff.top_p,
                "max_tokens_requested": p.max_tokens,
                "max_tokens_effective": eff.max_tokens,
                "seed_requested": p.seed,
                "reasoning_effort": p.reasoning_effort,
                "reasoning_effort_effective": eff.reasoning_effort,
                "reasoning_tokens_requested": p.reasoning_tokens,
                "solution": solution,
                "stop_reason": (
                    sample.output.stop_reason if (sample.output and sample.output.choices) else None
                ),
                "error": error,
                **usage_columns(usage),
                "usd": usd_for_usage(
                    prep.pricing,
                    cond.model,
                    usage,
                    prep.plan.batch,
                    exec_model=prep.native_routes.get(cond.model, cond.model),
                ),
                "latency_s": sample.total_time,
                "log_file": log_file,
                "sample_uuid": sample.uuid,
                "created_at": now,
            }
        )
    return rows


def run_generate(
    prep: "PreparedStudy",
    *,
    run_id: "str | None" = None,
    force: bool = False,
    condition_filter: "list[str] | None" = None,
    display: "str | None" = None,
    model_factory: "ModelFactory | None" = None,
    estimate_usd: "float | None" = None,
    estimate_full_usd: "float | None" = None,
    max_usd: "float | None" = None,
    wave: "str | None" = None,
) -> GenerateResult:
    from itemeval._driftcheck import endpoint_drift_warnings, generate_drift_warnings

    enforce_budget_cap(prep, "generate", max_usd, force, wave=wave)
    run_id = run_id or new_run_id("generate")
    prep.paths.ensure()
    upsert_items(prep.paths, prep.datasets)

    reps = prep.plan.replications
    store = _solutions.read_solutions(prep.paths)
    if wave is not None:
        # A wave is an epoch block: new epoch numbers are new keys — re-observe
        # the same scope without touching wave-0 rows (resumable mid-wave).
        wave_num, epoch_offset = _solutions.resolve_wave(store, wave, reps)
    else:
        wave_num, epoch_offset = 0, 0
    epoch_block = (epoch_offset + 1, epoch_offset + reps)

    selected = [c for c in prep.grid.generate if matches_filter(c.id, c.slug, condition_filter)]
    drift_warnings = generate_drift_warnings(prep.grid, store) + endpoint_drift_warnings(
        [c.model for c in selected], prep.paths.manifests_dir
    )
    manifest = build_manifest(
        prep,
        "generate",
        run_id,
        [c.id for c in selected],
        estimate_usd,
        estimate_full_usd,
        wave=wave_num,
        wave_label=wave,
        epoch_offset=epoch_offset,
    )
    manifest_path = write_manifest(manifest, prep.paths)

    reports: list[ConditionRunReport] = []
    rows_written = 0
    total_usd = 0.0
    run_models: list[str] = []  # models of conditions that actually ran
    sampling_effective: dict[str, Any] = {}
    endpoints_effective: dict[str, Any] = {}
    factory = model_factory or resolve_model
    item_ids = [it.id for it in prep.items_effective]
    # One truth value for "cache scheduling active": gates both task building
    # (warm-then-fan-out) and the zero-reads hint below.
    cache_schedule = prep.config.budget.cache_schedule != "off" and prep.plan.batch is None
    # Provider prompt-cache markers (Anthropic-style): explicit when
    # cache_prompt resolves on; loop-invariant, also feeds the unpinned hint.
    cp = prep.config.solvers.cache_prompt
    cache_prompt = (
        True
        if cp == "on" or (cp == "auto" and prep.plan.replications > 1)
        else (False if cp == "off" else None)
    )

    for cond in selected:
        existing = _solutions.read_solutions(prep.paths)
        if force:
            to_run = list(item_ids)
        else:
            missing = _solutions.epochs_to_run(
                existing,
                cond.id,
                item_ids,
                epoch_block,
                require_solution=prep.config.solvers.on_empty == "rerun",
            )
            to_run = [iid for iid in item_ids if missing[iid]]
        if not to_run:
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
        items = [it for it in prep.items_effective if it.id in set(to_run)]
        task = build_generate_task(
            items,
            cond,
            prep.solver_templates[cond.prompt_name],
            prep.config.study,
            prep.plan.replications,
            prep.config.cache,
            prep.origins,
            batch=prep.plan.batch,
            cache_prompt=cache_prompt,
            cache_schedule=cache_schedule,
            epoch_offset=epoch_offset,
        )
        # Native batch routing: run the call on the native id when active; the
        # sampled cond.model stays the recorded scientific identity.
        exec_model = prep.native_routes.get(cond.model, cond.model)
        try:
            # Serial eval per condition: inspect's eval is one-at-a-time per process.
            logs = inspect_ai.eval(
                task,
                model=factory(
                    exec_model,
                    "generate",
                    model_args_for(
                        exec_model,
                        provider_routing=prep.config.solvers.provider_routing,
                        cache_scheduling=cache_schedule,
                        study=prep.config.study,
                        condition_id=cond.id,
                    ),
                ),
                display=resolve_display(display),
                log_dir=str(prep.paths.logs_dir("generate", cond.id)),
                log_format="eval",
                fail_on_error=False,
                retry_on_error=1,
                tags=["itemeval", "generate"],
                metadata={
                    "itemeval_run_id": run_id,
                    "itemeval_study": prep.config.study,
                    "itemeval_condition_id": cond.id,
                },
            )
            log = logs[0]
        except Exception as e:  # eval-level failure: report and continue
            reports.append(
                ConditionRunReport(
                    condition_id=cond.id,
                    slug=cond.slug,
                    status="error",
                    items_run=len(items),
                    rows_written=0,
                    errors=0,
                    usd=None,
                    log_file=None,
                    message=f"{type(e).__name__}: {e}",
                )
            )
            continue

        rows = rows_from_generate_log(
            log, cond, prep, run_id, epoch_offset=epoch_offset, wave=wave_num, wave_label=wave
        )
        run_models.append(cond.model)
        endpoints_effective[cond.id] = endpoint_info(log, cond.model, exec_model)
        n = _solutions.upsert_solutions(prep.paths, rows)
        rows_written += n

        usd_vals = [r["usd"] for r in rows if r["usd"] is not None]
        cond_usd = sum(usd_vals) if usd_vals else None
        total_usd += cond_usd or 0.0
        _logs.upsert_log_index(
            prep.paths,
            [log_index_row(log, prep.paths, run_id, "generate", cond.id, cond.model, cond_usd)],
        )
        _ledger.upsert_ledger(
            prep.paths,
            [
                ledger_row(
                    run_id,
                    "generate",
                    cond.id,
                    cond.model,
                    rows,
                    prep.plan.batch,
                    epoch_offset=epoch_offset,
                    exec_model=exec_model,
                )
            ],
        )
        ok_rows = [r for r in rows if r["error"] is None]
        if ok_rows:
            sampling_effective[cond.id] = {
                k.replace("_effective", ""): ok_rows[0][k]
                for k in (
                    "temperature_effective",
                    "top_p_effective",
                    "max_tokens_effective",
                    "reasoning_effort_effective",
                )
            }
        reports.append(
            ConditionRunReport(
                condition_id=cond.id,
                slug=cond.slug,
                status="run",
                items_run=len(items),
                rows_written=n,
                errors=sum(1 for r in rows if r["error"] is not None),
                usd=cond_usd,
                log_file=rel_to_study(prep.paths, log.location),
                local_cache_rows=local_cache_rows(rows),
                **cache_columns(rows),
            )
        )

    if sampling_effective or endpoints_effective:
        finalize_manifest(
            manifest_path,
            sampling_effective=sampling_effective or None,
            endpoints_effective=endpoints_effective or None,
        )
    run_reports = [r for r in reports if r.status == "run"]
    hints = [
        h
        for h in (
            detect_cache_zero_reads(
                scheduled=cache_schedule,
                # gated epochs beyond each item's leader should read the cache
                repeated_prefix_calls=sum(
                    max(0, r.rows_written - r.items_run) for r in run_reports
                ),
                cache_read_tokens=sum(r.cache_read_tokens for r in run_reports),
                real_provider=any(not is_mock_model(m) for m in run_models),
            ),
            detect_openrouter_unpinned_cache(
                sorted(
                    {
                        m
                        for m in run_models
                        if bool(cache_prompt)
                        # Routed -> ran on the native API, not OpenRouter; the
                        # OpenRouter-cache caveat does not apply.
                        and m not in prep.native_routes
                        and prep.config.solvers.provider_routing is None
                        and provider_of(m) == "openrouter"
                        and cache_provider_of(m) == "anthropic"
                    }
                )
            ),
            detect_unpriced_models(
                sorted({m for m in run_models if lookup_price(prep.pricing, m) is None})
            ),
        )
        if h is not None
    ]
    local_total = sum(r.local_cache_rows for r in reports)
    batch_on = prep.plan.batch is not None
    # Execution ids (native under routing) drive batch_providers; routed_models
    # records the sampled->native switch for the conditions that actually ran.
    exec_models = [prep.native_routes.get(m, m) for m in run_models]
    routed_models = [
        NativeRoute(
            sampled=m, execution=prep.native_routes[m], provider=provider_of(prep.native_routes[m])
        )
        for m in dict.fromkeys(run_models)
        if m in prep.native_routes
    ]
    return GenerateResult(
        run_id=run_id,
        study=prep.config.study,
        conditions=reports,
        rows_written=rows_written,
        total_usd=total_usd,
        manifest_path=rel_to_study(prep.paths, manifest_path),
        hints=hints,
        warnings=drift_warnings,
        datasets=dataset_provenance(prep.datasets),
        model_sample=prep.model_sample,
        local_cache_rows=local_total,
        local_cache_dir=local_cache_dir() if local_total else None,
        batch=batch_on,
        batch_providers=batch_providers_used(exec_models) if batch_on else [],
        routed_models=routed_models,
        wave=wave_num,
        wave_label=wave,
        epoch_offset=epoch_offset,
    )
