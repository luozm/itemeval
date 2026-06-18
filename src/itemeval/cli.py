"""itemeval CLI: init | estimate | generate | grade | export | status."""

import argparse
import re
import sys
from pathlib import Path

from itemeval._errors import (
    AdapterError,
    ConfigError,
    ItemevalError,
    TemplateError,
)

_USAGE_ERRORS = (ConfigError, TemplateError, AdapterError)


def _fmt_table(headers: "list[str]", rows: "list[list[str]]") -> str:
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def line(cells: "list[str]") -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def _fmt_usd(value: "float | None") -> str:
    if value is None:
        return "—"
    if 0 < abs(value) < 0.01:
        return f"${value:.4f}"  # sub-cent amounts must not display as $0.00
    return f"${value:.2f}"


def _load(args) -> "tuple":
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    cfg = load_config(args.config, work_dir=getattr(args, "base_dir", None))
    refresh = getattr(args, "refresh_pricing", False)
    prep = prepare_study(cfg, refresh_pricing_table=refresh, policy=getattr(args, "policy", None))
    return cfg, prep


def _store_is_empty(prep, stage: str, condition_filter: "list[str] | None") -> bool:
    """Zero completed rows for the selected conditions of this stage's store."""
    from itemeval.generate._run import matches_filter

    if stage == "generate":
        from itemeval.store._solutions import read_solutions

        df = read_solutions(prep.paths)
        conds = prep.grid.generate
        col = "condition_id"
    else:
        from itemeval.store._gradings import read_gradings

        df = read_gradings(prep.paths)
        conds = prep.grid.grade
        col = "grade_condition_id"
    if df.empty:
        return True
    selected = {c.id for c in conds if matches_filter(c.id, c.slug, condition_filter)}
    return df[df[col].isin(selected)].empty


def _pct_complete(st) -> str:
    pct = 100.0 * st.completed_cells / max(st.total_cells, 1)
    return f"{pct:.0f}% complete"


def _cache_discount_clause(discount: float) -> str:
    """`includes −$X provider prompt-cache discount; ` (or the surcharge form
    when projected writes exceed reads); empty when no cache split applies."""
    if discount > 0:
        return f"includes −{_fmt_usd(discount)} provider prompt-cache discount; "
    if discount < 0:
        return f"includes +{_fmt_usd(-discount)} provider prompt-cache write surcharge; "
    return ""


def _ceiling_note(stage: str) -> str:
    """Always-on clause naming what makes the projection an upper bound."""
    if stage == "generate":
        return "ceiling: output at max_tokens"
    return "ceiling: output at max_tokens; ungenerated solutions stubbed at max"


def _expected_clause(st, stage: str) -> str:
    """Provenance for the expected (calibrated) line; '' at cold start."""
    cal = st.calibration
    if cal.observed_rows == 0:
        return ""
    noun = "generations" if stage == "generate" else "gradings"
    parts = []
    if cal.mean_output_tokens is not None:
        label = "mean output" if stage == "generate" else "mean judge output"
        parts.append(f"{label} {cal.mean_output_tokens:.0f} tok")
    if cal.mean_solution_chars is not None:  # grade only
        parts.append(f"mean solution {cal.mean_solution_chars / 4:.0f} tok")
    detail = f": {', '.join(parts)}" if parts else ""
    return f"calibrated from {cal.observed_rows} observed {noun}{detail}"


def _print_estimate(est, stage: str) -> None:
    stages = {"generate": est.generate, "grade": est.grade}
    selected = list(stages.items()) if stage == "all" else [(stage, stages[stage])]
    for name, st in selected:
        delta = ""
        if st.completed_cells > 0:
            delta = f" — {_fmt_usd(st.remaining_usd)} remaining ({_pct_complete(st)})"
        discount = ""
        if st.cache_discount_usd:
            discount = f" ({_cache_discount_clause(st.cache_discount_usd).rstrip('; ')})"
        print(
            f"{name.upper()} — {st.calls} calls, "
            f"{st.input_tokens:,} input tok, {st.output_tokens:,} output tok, "
            f"projected {_fmt_usd(st.usd)}{discount}{delta} — {_ceiling_note(name)}"
        )
        exp_clause = _expected_clause(st, name)
        if exp_clause:
            print(f"  expected ~{_fmt_usd(st.expected_usd)} ({exp_clause})")
        rows = [
            [
                c.slug,
                c.model,
                str(c.calls),
                f"{c.input_tokens:,}",
                f"{c.output_tokens:,}",
                _fmt_usd(c.usd) + (" (batch)" if c.batch_discount else ""),
            ]
            for c in st.conditions
        ]
        print(_fmt_table(["condition", "model", "calls", "in_tok", "out_tok", "usd"], rows))
        print()
    if stage == "all":
        print(f"total projected: {_fmt_usd(est.total_usd)}")
    for w in est.warnings:
        print(f"warning: {w}")
    print(
        "(projected figures cover the full policy-effective grid; the gate "
        "applies to the remaining figure — completed work is never re-paid. "
        "The expected figure is informational; the gate always uses the ceiling)"
    )


def _print_pricing(prov) -> None:
    age = f", {prov.age_days:.0f}d old" if prov.age_days is not None else ""
    suffix = " — just refreshed from OpenRouter" if prov.refreshed else ""
    print(f"pricing: {prov.source} (updated {prov.updated_at}{age}){suffix}")


def _print_datasets(prep) -> None:
    """One provenance line per dataset (Law 1) — unconditional in text mode."""
    for ds in prep.datasets:
        if ds.cache == "downloaded":
            size = f"{ds.download_bytes / 1e6:.0f} MB " if ds.download_bytes else ""
            clause = f"downloaded {size}to HF cache (first use)"
        else:
            pinned = " (pinned)" if ds.revision_source in ("lock", "config") else ""
            clause = f"reused from HF cache{pinned}"
        line = f"dataset: {ds.dataset_id} (split {ds.split}) @ {ds.revision[:7]} — {clause}"
        if ds.pinned_now:
            line += "; revision pinned in dataset_locks.json"
        print(line)


def _print_model_sample(prep) -> None:
    """One provenance line when solvers.sample drew the models (Law 1)."""
    ms = prep.model_sample
    if ms is None:
        return
    source = {
        "pricing-table": "the OpenRouter roster",
        "explicit": "an inline list",
        "file": "a model-id file",
    }.get(ms.source, ms.source)
    strat = ""
    if ms.stratify_by:
        strat = f", stratified by {ms.stratify_by}"
        if ms.allocation == "equal":
            strat += " (equal)"
    incl = f", {len(ms.include)} via include" if ms.include else ""
    excl = f", {len(ms.exclude)} excluded" if ms.exclude else ""
    if ms.pinned_now:
        print(
            f"models: sampled {ms.n} of {ms.universe_size} (seed {ms.seed}{strat}{incl}{excl}) "
            f"from {source} — pinned in model_locks.json"
        )
    else:
        drift = (
            f"; universe changed since the pin (now {ms.universe_size}) — draw unchanged"
            if ms.universe_drift
            else ""
        )
        print(f"models: {ms.n} sampled models reused from model_locks.json (seed {ms.seed}){drift}")


def _print_native_routes(prep) -> None:
    """One provenance line when native batch routing is active (Law 1): a side
    effect — the serving endpoint changes — so it is announced unconditionally."""
    from itemeval.budget._pricing import provider_of

    routes = prep.native_routes
    if not routes:
        return
    providers = ", ".join(sorted({provider_of(n) for n in routes.values()}))
    print(
        f"native batch routing: {len(routes)} model(s) → native API ({providers}) — "
        "sampled ids stay the scientific identity; native id recorded as execution_model"
    )


def _print_route_comparison(est) -> None:
    """W2: per eligible model, native-batch vs OpenRouter-cache (expected cost,
    remaining scope) so the cheaper lever is visible. Estimate surface only."""
    if not est.routes:
        return
    print(f"native routing comparison ({len(est.routes)} model(s) eligible; expected, remaining):")
    for r in est.routes:
        cache = "n/a" if r.cache_usd is None else _fmt_usd(r.cache_usd)
        print(
            f"  {r.sampled}  native batch {_fmt_usd(r.batch_usd)}  ·  "
            f"openrouter cache {cache}  → {r.cheaper} cheaper"
        )


def _print_cost_report(rep) -> None:
    print(
        f"savings vs list price: {_fmt_usd(rep.total_savings_usd)} "
        f"({rep.savings_pct:.0f}%) — cache {_fmt_usd(rep.cache_savings_usd)}, "
        f"batch {_fmt_usd(rep.batch_savings_usd)} "
        f"(estimated; excludes resume / response-cache reuse)"
    )
    if rep.by_provider:
        rows = [
            [
                p.provider,
                str(p.calls),
                _fmt_usd(p.usd),
                _fmt_usd(p.baseline_usd),
                _fmt_usd(p.savings_usd),
            ]
            for p in rep.by_provider
        ]
        print(_fmt_table(["provider", "calls", "spend", "list_price", "saved"], rows))


def _cmd_estimate(args) -> int:
    from itemeval._hints import emit_hints
    from itemeval.budget._estimator import estimate_study

    _, prep = _load(args)
    est = estimate_study(prep)
    if args.json:
        print(est.model_dump_json(indent=2))
        return 0
    print(f"study: {est.study}  (policy: {est.policy})")
    _print_datasets(prep)
    _print_model_sample(prep)
    _print_native_routes(prep)
    _print_pricing(est.pricing)
    _print_estimate(est, args.stage)
    _print_route_comparison(est)
    emit_hints(est.hints)
    return 0


def _check_gate(est_usd: float, cfg, assume_yes: bool, machine: bool = False):
    from itemeval.budget._gate import check_gate

    return check_gate(est_usd, cfg.budget, assume_yes, machine=machine)


def _gate_stop_doc(stage: str, study: str, st, est, gate, config_arg: str, hints=()) -> str:
    """JSON document emitted on a gate stop under --json (exit 3/4): an agent
    still gets the projected cost, the gate reason, and the rerun command."""
    import json

    doc = {
        "study": study,
        "stage": stage,
        "estimate_usd": st.remaining_usd,  # what this run would spend (gate input)
        "estimate_full_usd": st.usd,
        "expected_estimate_usd": st.expected_remaining_usd,  # calibrated, informational
        "completed_cells": st.completed_cells,
        "total_cells": st.total_cells,
        "rows_replaced": st.rows_replaced,
        "pricing": est.pricing.model_dump(mode="json"),
        "gate": gate.model_dump(mode="json"),
        "rerun": f"itemeval {stage} {config_arg} --yes",
        "hints": [h.model_dump(mode="json") for h in hints],
    }
    return json.dumps(doc, indent=2)


def _pilot_hint(prep, cfg, stage: str, est_usd: float, condition_filter):
    """The pilot-available hint: gate engaged on a study with no completed rows."""
    from itemeval._hints import detect_pilot_available

    if est_usd <= cfg.budget.confirm_above_usd:
        return None
    return detect_pilot_available(
        store_is_empty=_store_is_empty(prep, stage, condition_filter),
        dev_items=cfg.budget.dev_items,
    )


def _print_wave_summary(result, prep) -> None:
    if result.wave_label is None:
        return
    lo = result.epoch_offset + 1
    hi = result.epoch_offset + prep.plan.replications
    print(
        f"wave {result.wave_label}: epochs {lo}–{hi} · "
        f"{result.rows_written} rows · {_fmt_usd(result.total_usd)}"
    )


def _print_local_cache(result) -> None:
    """Reuse and provider-side job creation announced (Law 1)."""
    if result.local_cache_rows:
        print(
            f"{result.local_cache_rows} calls answered from local cache ($0) — "
            f"cache dir: {result.local_cache_dir}"
        )
    if result.batch and result.batch_providers:
        # best-effort: inspect manages provider batch jobs internally and does
        # not surface job ids — never fake one.
        print(
            f"batch: enabled ({', '.join(result.batch_providers)}) — "
            "provider-side jobs created; resume with the same command"
        )


def _print_reports(reports) -> None:
    total = len(reports)
    for i, rep in enumerate(reports, 1):
        if rep.status == "skipped":
            print(f"[{i}/{total}] {rep.condition_id}  skipped: complete")
        elif rep.status == "error":
            print(f"[{i}/{total}] {rep.condition_id}  ERROR: {rep.message}")
        else:
            cache = ""
            if rep.cache_read_tokens or rep.cache_write_tokens:
                hit_pct = 100.0 * rep.cache_hit_rows / max(rep.rows_written, 1)
                cache = (
                    f" cache_read={rep.cache_read_tokens} "
                    f"cache_write={rep.cache_write_tokens} hit_rows={hit_pct:.0f}%"
                )
            print(
                f"[{i}/{total}] {rep.condition_id}  items={rep.items_run} "
                f"rows=+{rep.rows_written} errors={rep.errors} usd={_fmt_usd(rep.usd)}{cache}"
            )


def _run_stage(args, stage, runner) -> int:
    """Shared estimate → gate → run → report skeleton for generate and grade."""
    from itemeval._hints import emit_hints
    from itemeval.budget._estimator import estimate_study

    cfg, prep = _load(args)
    est = estimate_study(prep, force=args.force, wave=args.wave)
    st = est.generate if stage == "generate" else est.grade
    if not args.json:
        _print_datasets(prep)
        _print_model_sample(prep)
        _print_native_routes(prep)
        _print_pricing(est.pricing)
        if stage == "generate" and args.wave:
            print(
                f"wave {args.wave}: local response cache off — re-observations must be fresh draws"
            )
        delta = ""
        if st.completed_cells > 0:
            delta = f" remaining of {_fmt_usd(st.usd)} full grid ({_pct_complete(st)})"
        print(
            f"projected {stage} cost: {_fmt_usd(st.remaining_usd)}{delta} "
            f"({_cache_discount_clause(st.remaining_cache_discount_usd)}"
            f"confirm_above_usd: ${cfg.budget.confirm_above_usd:.2f}) — {_ceiling_note(stage)}"
        )
        exp_clause = _expected_clause(st, stage)
        if exp_clause:
            print(f"  expected ~{_fmt_usd(st.expected_remaining_usd)} ({exp_clause})")
        if st.rows_replaced:
            print(
                f"this run replaces {st.rows_replaced} existing rows "
                "(re-runs replay byte-identically from the local response cache "
                "at $0 where available)"
            )
        # Estimator warnings carry their stage, so each command relays only
        # what concerns it (e.g. uncapped generation caps, inert routing).
        for w in st.warnings:
            print(f"warning: {w}")
    gate = _check_gate(st.remaining_usd, cfg, args.yes, machine=args.json)
    pilot = _pilot_hint(prep, cfg, stage, st.remaining_usd, args.condition)
    # Estimate-time hints for this stage (e.g. split-head-below-min) surface
    # on the run commands too — merged into the run's hints, or emitted with
    # the stop document on a gate stop.
    pre_hints = [*st.hints, *([pilot] if pilot else [])]
    if not gate.proceed:
        # The ceiling hint is pre-spend advice — raise it only at a gate stop
        # (you are blocked before any spend), never on a proceeding run.
        from itemeval._hints import detect_estimate_is_ceiling

        ceiling = detect_estimate_is_ceiling(
            observed_rows=st.calibration.observed_rows, projected_usd=st.remaining_usd
        )
        stop_hints = [*pre_hints, *([ceiling] if ceiling else [])]
        if args.json:
            print(_gate_stop_doc(stage, cfg.study, st, est, gate, args.config, hints=stop_hints))
        else:
            print(f"itemeval: {gate.reason}", file=sys.stderr)
            emit_hints(stop_hints)
        return gate.exit_code
    # --json declares a machine consumer: silence inspect's live display unless
    # the operator explicitly chose one, so stdout stays pure JSON.
    display = args.display if args.display is not None else ("none" if args.json else None)
    result = runner(prep, display, st.remaining_usd, st.usd)
    result.pricing = est.pricing
    result.estimate_usd = st.remaining_usd
    result.expected_estimate_usd = st.expected_remaining_usd
    result.rows_replaced = st.rows_replaced
    result.gate = gate
    if pre_hints:
        result.hints = [*result.hints, *pre_hints]
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        _print_reports(result.conditions)
        _print_local_cache(result)
        for w in result.warnings:
            print(f"warning: {w}")
        _print_wave_summary(result, prep)
        if stage == "generate":
            print(f"rows written: {result.rows_written}  spend: {_fmt_usd(result.total_usd)}")
        else:
            print(
                f"rows written: {result.rows_written}  parse_failures={result.parse_failures}  "
                f"spend: {_fmt_usd(result.total_usd)}"
            )
            if result.empty_total:
                # Self-contained fact only; the advice lives at
                # Error-Handling#empty-completions (relayed by the empty-solutions
                # hint — advice never rides the summary block).
                breakdown = ", ".join(f"{k}×{v}" for k, v in result.empty_stop_reasons.items())
                if result.empty_skipped:
                    print(
                        f"empty solutions: {result.empty_skipped} excluded from grading "
                        f"[{breakdown}] — on_empty={result.on_empty}"
                    )
                else:
                    print(
                        f"empty solutions: {result.empty_total} graded as-is "
                        f"[{breakdown}] — on_empty={result.on_empty}"
                    )
        print(f"manifest: {result.manifest_path}")
        emit_hints(result.hints)
    return 1 if any(r.status == "error" for r in result.conditions) else 0


def _cmd_generate(args) -> int:
    from itemeval.generate._run import run_generate

    def runner(prep, display, estimate_usd, estimate_full_usd):
        return run_generate(
            prep,
            force=args.force,
            condition_filter=args.condition,
            display=display,
            estimate_usd=estimate_usd,
            estimate_full_usd=estimate_full_usd,
            wave=args.wave,
        )

    return _run_stage(args, "generate", runner)


def _cmd_grade(args) -> int:
    from itemeval.grade._run import run_grade

    def runner(prep, display, estimate_usd, estimate_full_usd):
        return run_grade(
            prep,
            force=args.force,
            condition_filter=args.condition,
            graders=args.grader,
            rubrics=args.rubric,
            display=display,
            estimate_usd=estimate_usd,
            estimate_full_usd=estimate_full_usd,
            wave=args.wave,
        )

    return _run_stage(args, "grade", runner)


def _cmd_export(args) -> int:
    from itemeval._config import load_config
    from itemeval._hints import emit_hints
    from itemeval.store._export import export_study

    result = export_study(
        load_config(args.config, work_dir=getattr(args, "base_dir", None)),
        snapshot=args.snapshot,
    )
    if args.json:
        print(result.model_dump_json(indent=2))
        return 0
    print("export: rewrote export/ — gradings_long.parquet + .csv, ledger.csv (disposable view)")
    if result.snapshot:
        snap = result.snapshot
        print(
            f"snapshot: {snap.name} written — {snap.rows:,} rows · "
            f"{len(snap.run_ids)} runs · {_fmt_usd(snap.spend_usd)} total · {snap.path}/"
        )
    print(f"rows: {result.rows}")
    print(f"gradings: {result.gradings_parquet} + {result.gradings_csv}")
    print(f"ledger:   {result.ledger_csv}")
    print(
        f"spend: generate {_fmt_usd(result.generation_usd)} | grade {_fmt_usd(result.grading_usd)}"
    )
    _print_pricing(result.pricing)
    _print_cost_report(result.cost)
    print(
        f"internally reconciled (ledger vs row sums): "
        f"{'yes' if result.internally_reconciled else 'NO'}"
    )
    if not result.internally_reconciled:
        print("warning: ledger does not match row sums; inspect ledger.csv", file=sys.stderr)
    print("(reconciliation against provider dashboards is a manual step)")
    emit_hints(result.hints)
    return 0


def _cmd_status(args) -> int:
    from itemeval._status import build_status
    from itemeval.budget._pricing import describe_pricing

    cfg, prep = _load(args)
    report = build_status(cfg, prep)
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    print(f"study: {report.study}  (policy: {report.policy})")
    print(f"config: {report.config_path}")
    _print_datasets(prep)
    _print_model_sample(prep)
    _print_pricing(describe_pricing(prep.pricing, refreshed=prep.pricing_refreshed))
    ds_bits = ", ".join(f"{d.id}@{d.revision[:8]}: {d.n_items}" for d in report.datasets)
    print(
        f"items: {report.n_items_total} loaded ({ds_bits}) | "
        f"policy-effective: {report.n_items_effective}"
    )
    print(
        f"replications: {report.replications_requested} "
        f"(effective: {report.replications_effective})"
    )
    print()
    per_cond = report.generate[0].expected if report.generate else 0
    print(
        f"GENERATE — {len(report.generate)} conditions x "
        f"{report.n_items_effective} items x {report.replications_effective} "
        f"epochs = {len(report.generate) * per_cond} expected"
    )
    rows = [
        [
            c.condition_id,
            c.detail.get("model", ""),
            c.detail.get("prompt", ""),
            c.detail.get("model_config", ""),
            f"{c.completed}/{c.expected}",
            str(c.errors),
            str(c.incomplete),
        ]
        for c in report.generate
    ]
    print(_fmt_table(["condition", "model", "prompt", "config", "done", "err", "empty"], rows))
    print()
    grade_expected = report.grade[0].expected if report.grade else 0
    print(f"GRADE — {len(report.grade)} condition(s) x {grade_expected} solutions")
    rows = [
        [
            c.condition_id,
            c.detail.get("grader", c.detail.get("scorer", "")),
            c.detail.get("rubric", ""),
            f"{c.completed}/{c.expected}",
            str(c.errors),
            str(c.parse_failures),
        ]
        for c in report.grade
    ]
    print(_fmt_table(["condition", "grader", "rubric", "done", "err", "parse_fail"], rows))
    print()
    print(
        f"spend: generate {_fmt_usd(report.spend_generate_usd)} | "
        f"grade {_fmt_usd(report.spend_grade_usd)} | "
        f"total {_fmt_usd(report.spend_generate_usd + report.spend_grade_usd)}"
    )
    latest = f" (latest: manifests/{report.manifests[-1]})" if report.manifests else ""
    print(f"manifests: {len(report.manifests)}{latest}")
    if len(report.waves) > 1:  # zero noise for single-wave studies
        bits = ", ".join(
            f"{w.wave}{f' ({w.label})' if w.label else ''} — gen {w.completed}/{w.expected}"
            + (f" · graded {w.graded}/{w.grade_expected}" if report.grade else "")
            for w in report.waves
        )
        print(f"waves: {bits}")
    if report.snapshots:
        bits = ", ".join(
            f"{s.name} ({s.created_at[:10]}, {s.rows:,} rows)" for s in report.snapshots
        )
        print(f"snapshots: {bits}")
    return 0


def _cmd_init(args) -> int:
    import yaml

    from itemeval._templates import BUILTIN_PREFIX, _builtin_root, read_builtin
    from itemeval.design._ids import slugify

    target = Path(args.dir).expanduser()
    config_path = target / "config.yaml"
    if config_path.exists() and not args.force:
        print(f"itemeval: {config_path} already exists (use --force to overwrite)", file=sys.stderr)
        return 2

    text = _builtin_root().joinpath("config.yaml").read_text(encoding="utf-8")
    study = slugify(target.resolve().name)
    text = re.sub(r"(?m)^study:.*$", f"study: {study}", text, count=1)

    copied = []
    if args.with_templates:
        spec = yaml.safe_load(text)
        for ref in spec["facets"].get("prompt", []):
            if ref.startswith(BUILTIN_PREFIX):
                name = ref[len(BUILTIN_PREFIX) :]
                dest = target / "prompts" / "solver" / f"{name}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(read_builtin("prompts/solver", name) or "", encoding="utf-8")
                copied.append(dest)
        for ref in spec["facets"].get("rubric", []):
            if ref.startswith(BUILTIN_PREFIX):
                name = ref[len(BUILTIN_PREFIX) :]
                dest = target / "rubrics" / f"{name}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(read_builtin("rubrics", name) or "", encoding="utf-8")
                copied.append(dest)
        # drop the `builtin:` prefix only on the facet ref lines, so refs now point
        # at the local copies; comments elsewhere are left untouched.
        text = "".join(
            line.replace(BUILTIN_PREFIX, "")
            if line.lstrip().startswith(("prompt:", "rubric:"))
            else line
            for line in text.splitlines(keepends=True)
        )

    target.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")

    print(f"created {config_path}")
    for dest in copied:
        print(f"  + {dest.relative_to(target)}")
    print("\nnext steps (runs free on the mock provider):")
    for cmd in ("status", "generate", "grade", "export"):
        suffix = " --yes" if cmd in ("generate", "grade") else ""
        print(f"  itemeval {cmd:<8} {config_path}{suffix}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="itemeval",
        description="Item-level LLM evaluation over any API, with built-in budget control.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="scaffold a new study (writes config.yaml)")
    init.add_argument("dir", help="target directory for the new study")
    init.add_argument(
        "--with-templates",
        action="store_true",
        help="also copy the referenced built-in prompts/rubrics as editable local files",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing config.yaml")
    init.set_defaults(fn=_cmd_init)

    def add(name: str, fn, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("config", help="experiment config YAML")
        p.add_argument(
            "-C",
            "--base-dir",
            default=None,
            metavar="DIR",
            help="work directory anchoring outputs (the studies/ tree); default: current directory",
        )
        p.set_defaults(fn=fn)
        return p

    def add_policy(p):
        p.add_argument(
            "--policy",
            choices=["dev", "full-interactive", "full-batch"],
            default=None,
            help="override budget.policy for this invocation only (config unchanged)",
        )

    p = add("estimate", _cmd_estimate, "projected $ per stage; no model API calls")
    add_policy(p)
    p.add_argument("--stage", choices=["generate", "grade", "all"], default="all")
    p.add_argument(
        "--refresh-pricing",
        action="store_true",
        help="refresh pricing from the OpenRouter API first",
    )
    p.add_argument("--json", action="store_true")

    for name, fn, help_text in (
        ("generate", _cmd_generate, "stage 1: generate solutions (resumable)"),
        ("grade", _cmd_grade, "stage 2: grade stored solutions (resumable)"),
    ):
        p = add(name, fn, help_text)
        add_policy(p)
        p.add_argument(
            "--wave",
            default=None,
            metavar="LABEL",
            help="re-observe the current scope as a new epoch block (generate), or "
            "grade that block's solutions (grade); existing waves resume by label",
        )
        p.add_argument("-y", "--yes", action="store_true", help="skip the cost confirmation gate")
        p.add_argument(
            "--json",
            action="store_true",
            help="emit the run result as JSON on stdout (silences live display)",
        )
        p.add_argument("--force", action="store_true", help="re-run even completed work")
        p.add_argument(
            "--condition",
            action="append",
            help="only conditions matching this id/prefix/slug (repeatable)",
        )
        p.add_argument(
            "--display",
            default=None,
            choices=["none", "plain", "rich", "full"],
            help="inspect progress display (default: rich live progress; honors "
            "INSPECT_DISPLAY; use 'none' to silence)",
        )
        if name == "grade":
            p.add_argument("--grader", action="append", help="only this grader (repeatable)")
            p.add_argument("--rubric", action="append", help="only this rubric (repeatable)")

    p = add("export", _cmd_export, "long-format parquet + CSV + cost ledger")
    p.add_argument(
        "--snapshot",
        default=None,
        metavar="NAME",
        help="also freeze an immutable named copy under export/snapshots/NAME/ "
        "(with manifests, locks, snapshot.json, STUDY_CARD.md); an existing "
        "name is refused",
    )
    p.add_argument("--json", action="store_true")

    p = add("status", _cmd_status, "expanded grid + completion matrix; no model API calls")
    add_policy(p)
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except _USAGE_ERRORS as e:
        print(f"itemeval: error: {e}", file=sys.stderr)
        return 2
    except ItemevalError as e:
        print(f"itemeval: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
