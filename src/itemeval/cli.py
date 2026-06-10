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
    prep = prepare_study(cfg, refresh_pricing_table=refresh)
    return cfg, prep


def _print_estimate(est, stage: str) -> None:
    stages = {"generate": est.generate, "grade": est.grade}
    selected = list(stages.items()) if stage == "all" else [(stage, stages[stage])]
    for name, st in selected:
        print(
            f"{name.upper()} — {st.calls} calls, "
            f"{st.input_tokens:,} input tok, {st.output_tokens:,} output tok, "
            f"projected {_fmt_usd(st.usd)}"
        )
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
        if st.unpriced_models:
            print(f"unpriced models: {', '.join(st.unpriced_models)}")
        print()
    if stage == "all":
        print(f"total projected: {_fmt_usd(est.total_usd)}")
    for w in est.warnings:
        print(f"warning: {w}")
    print("(estimate projects the full policy-effective grid; completed work is not subtracted)")


def _cmd_estimate(args) -> int:
    from itemeval.budget._estimator import estimate_study

    _, prep = _load(args)
    est = estimate_study(prep)
    if args.json:
        print(est.model_dump_json(indent=2))
        return 0
    print(f"study: {est.study}  (policy: {est.policy})")
    _print_estimate(est, args.stage)
    return 0


def _run_gate(est_usd: float, cfg, assume_yes: bool) -> "int | None":
    from itemeval.budget._gate import check_gate

    gate = check_gate(est_usd, cfg.budget, assume_yes)
    if not gate.proceed:
        print(f"itemeval: {gate.reason}", file=sys.stderr)
        return gate.exit_code
    return None


def _print_reports(reports) -> None:
    total = len(reports)
    for i, rep in enumerate(reports, 1):
        if rep.status == "skipped":
            print(f"[{i}/{total}] {rep.condition_id}  skipped: complete")
        elif rep.status == "error":
            print(f"[{i}/{total}] {rep.condition_id}  ERROR: {rep.message}")
        else:
            print(
                f"[{i}/{total}] {rep.condition_id}  items={rep.items_run} "
                f"rows=+{rep.rows_written} errors={rep.errors} usd={_fmt_usd(rep.usd)}"
            )


def _cmd_generate(args) -> int:
    from itemeval.budget._estimator import estimate_study
    from itemeval.generate._run import run_generate

    cfg, prep = _load(args)
    est = estimate_study(prep)
    print(
        f"projected generate cost: {_fmt_usd(est.generate.usd)} "
        f"(confirm_above_usd: ${cfg.budget.confirm_above_usd:.2f})"
    )
    for w in est.warnings:
        print(f"warning: {w}")
    code = _run_gate(est.generate.usd, cfg, args.yes)
    if code is not None:
        return code
    result = run_generate(
        prep,
        force=args.force,
        condition_filter=args.condition,
        display=args.display,
        estimate_usd=est.generate.usd,
    )
    _print_reports(result.conditions)
    print(f"rows written: {result.rows_written}  spend: {_fmt_usd(result.total_usd)}")
    print(f"manifest: {result.manifest_path}")
    return 1 if any(r.status == "error" for r in result.conditions) else 0


def _cmd_grade(args) -> int:
    from itemeval.budget._estimator import estimate_study
    from itemeval.grade._run import run_grade

    cfg, prep = _load(args)
    est = estimate_study(prep)
    print(
        f"projected grade cost: {_fmt_usd(est.grade.usd)} "
        f"(confirm_above_usd: ${cfg.budget.confirm_above_usd:.2f})"
    )
    code = _run_gate(est.grade.usd, cfg, args.yes)
    if code is not None:
        return code
    result = run_grade(
        prep,
        force=args.force,
        condition_filter=args.condition,
        graders=args.grader,
        rubrics=args.rubric,
        display=args.display,
        estimate_usd=est.grade.usd,
    )
    _print_reports(result.conditions)
    print(
        f"rows written: {result.rows_written}  parse_failures={result.parse_failures}  "
        f"spend: {_fmt_usd(result.total_usd)}"
    )
    print(f"manifest: {result.manifest_path}")
    return 1 if any(r.status == "error" for r in result.conditions) else 0


def _cmd_export(args) -> int:
    from itemeval._config import load_config
    from itemeval.store._export import export_study

    result = export_study(load_config(args.config, work_dir=getattr(args, "base_dir", None)))
    if args.json:
        print(result.model_dump_json(indent=2))
        return 0
    print(f"rows: {result.rows}")
    print(f"gradings: {result.gradings_parquet} + {result.gradings_csv}")
    print(f"ledger:   {result.ledger_csv}")
    print(
        f"spend: generate {_fmt_usd(result.generation_usd)} | grade {_fmt_usd(result.grading_usd)}"
    )
    print(
        f"internally reconciled (ledger vs row sums): "
        f"{'yes' if result.internally_reconciled else 'NO'}"
    )
    if not result.internally_reconciled:
        print("warning: ledger does not match row sums; inspect ledger.csv", file=sys.stderr)
    print("(reconciliation against provider dashboards is a manual step)")
    return 0


def _cmd_status(args) -> int:
    from itemeval._status import build_status

    cfg, prep = _load(args)
    report = build_status(cfg, prep)
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    print(f"study: {report.study}  (policy: {report.policy})")
    print(f"config: {report.config_path}")
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
        ]
        for c in report.generate
    ]
    print(_fmt_table(["condition", "model", "prompt", "config", "done", "err"], rows))
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

    p = add("estimate", _cmd_estimate, "projected $ per stage; no model API calls")
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
        p.add_argument("-y", "--yes", action="store_true", help="skip the cost confirmation gate")
        p.add_argument("--force", action="store_true", help="re-run even completed work")
        p.add_argument(
            "--condition",
            action="append",
            help="only conditions matching this id/prefix/slug (repeatable)",
        )
        p.add_argument("--display", default="none", choices=["none", "plain", "rich", "full"])
        if name == "grade":
            p.add_argument("--grader", action="append", help="only this grader (repeatable)")
            p.add_argument("--rubric", action="append", help="only this rubric (repeatable)")

    p = add("export", _cmd_export, "long-format parquet + CSV + cost ledger")
    p.add_argument("--json", action="store_true")

    p = add("status", _cmd_status, "expanded grid + completion matrix; no model API calls")
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
