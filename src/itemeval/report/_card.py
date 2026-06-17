"""STUDY_CARD.md: a self-describing record written into every snapshot.

HF-dataset-card analog: YAML front-matter (machine-readable, versioned via
`itemeval_study_card`) followed by Markdown sections whose every number is
derived from existing stores — no new data, no interpretation, no plots.
"""

import json
from typing import TYPE_CHECKING

import pandas as pd
import yaml

if TYPE_CHECKING:
    from itemeval._config import ExperimentConfig
    from itemeval._prepare import PreparedStudy

CARD_SCHEMA_VERSION = 1


def _table(headers: "list[str]", rows: "list[list[str]]") -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _usd(value) -> str:
    return f"${float(value):.2f}"


def _read_manifests(paths, run_ids: "list[str]") -> "list[dict]":
    out = []
    for run_id in sorted(run_ids):
        p = paths.manifests_dir / f"{run_id}.json"
        if p.is_file():
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(out, key=lambda m: m.get("created_at") or "")


def build_study_card(
    config: "ExperimentConfig",
    prep: "PreparedStudy",
    status,
    long_df: "pd.DataFrame",
    ledger_df: "pd.DataFrame",
    cost,
    *,
    snapshot_name: str,
    created_at: str,
    run_ids: "list[str]",
    spend_usd: float,
    itemeval_version: str,
) -> str:
    manifests = _read_manifests(prep.paths, run_ids)

    front = {
        "itemeval_study_card": CARD_SCHEMA_VERSION,
        "study": config.study,
        "snapshot": snapshot_name,
        "created": created_at[:10],
        "itemeval_version": itemeval_version,
        "datasets": [
            {"id": ds.dataset_id, "revision": ds.revision[:8], "items": len(ds.items)}
            for ds in prep.datasets
        ],
        "models": list(config.solvers.models),
        "replications": config.facets.replications,
        "graders": [
            {"name": name, "model": config.grader_spec(name).model} for name in config.facets.grader
        ],
        "rows": int(len(long_df)),
        "spend_usd": round(float(spend_usd), 4),
    }
    if prep.model_sample is not None:
        ms = prep.model_sample
        front["model_sample"] = {
            "source": ms.source,
            "n": ms.n,
            "seed": ms.seed,
            "universe_size": ms.universe_size,
            "universe_hash": ms.universe_hash,
            "stratify_by": ms.stratify_by,
        }
    front_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()

    # 1. Design — the facet grid with content hashes and template sources.
    gen_rows = [
        [c.slug, c.model, f"{c.prompt_name} ({c.prompt_hash})", c.model_config_name]
        for c in prep.grid.generate
    ]
    grade_rows = [
        [
            c.slug,
            c.grader_name or "(verifiable)",
            c.grader_model or c.scorer or "",
            f"{c.rubric_name} ({c.rubric_hash})" if c.rubric_name else "",
        ]
        for c in prep.grid.grade
    ]
    template_rows = [
        [t.name, kind, t.source, t.hash12]
        for kind, templates in (
            ("prompt", prep.solver_templates.values()),
            ("rubric", prep.rubric_templates.values()),
        )
        for t in templates
    ]
    sample_note = ""
    if prep.model_sample is not None:
        ms = prep.model_sample
        strat = f", stratified by {ms.stratify_by}" if ms.stratify_by else ""
        src = {
            "pricing-table": "the OpenRouter roster",
            "explicit": "an inline list",
            "file": "a model-id file",
        }.get(ms.source, ms.source)
        sample_note = (
            f"Models: sampled {ms.n} of {ms.universe_size} from {src} "
            f"(seed {ms.seed}{strat}); pinned in model_locks.json.\n\n"
        )
    design = (
        sample_note
        + f"Crossing: `{config.crossing}` — models × prompts × model-configs × graders × "
        f"rubrics × replications ({config.facets.replications}).\n\n"
        + _table(["generate condition", "model", "prompt (hash)", "model_config"], gen_rows)
        + "\n\n"
        + _table(["grade condition", "grader", "model/scorer", "rubric (hash)"], grade_rows)
        + "\n\nTemplates:\n\n"
        + _table(["name", "kind", "source", "content hash"], template_rows)
    )

    # 2. Execution — one row per run; the reproducibility gold is served_model.
    ledger = ledger_df if not ledger_df.empty else pd.DataFrame(columns=["run_id", "calls", "usd"])
    exec_rows = []
    served: "dict[str, dict]" = {}
    for m in manifests:
        rid = m.get("run_id", "")
        lrows = ledger[ledger["run_id"] == rid] if not ledger.empty else ledger
        calls = int(pd.to_numeric(lrows["calls"], errors="coerce").fillna(0).sum())
        usd = float(pd.to_numeric(lrows["usd"], errors="coerce").fillna(0.0).sum())
        exec_rows.append(
            [
                rid,
                m.get("stage", ""),
                (m.get("created_at") or "")[:10],
                str(calls),
                _usd(usd),
                m.get("policy", ""),
            ]
        )
        # latest manifest per condition wins (manifests sorted by created_at)
        for cond_id, info in (m.get("endpoints_effective") or {}).items():
            served[cond_id] = info or {}
    served_rows = [
        [cond_id, info.get("provider", ""), info.get("served_model") or "(unknown)"]
        for cond_id, info in sorted(served.items())
    ]
    execution = _table(["run_id", "stage", "date", "calls", "spend", "policy"], exec_rows)
    if served_rows:
        execution += "\n\nResolved endpoints (which provider snapshot actually answered):\n\n"
        execution += _table(["condition", "provider", "served_model"], served_rows)

    # 3. Results — completion matrix + per-condition means (descriptive only).
    completion_rows = [
        [c.condition_id, f"{c.completed}/{c.expected}", str(c.errors), str(c.incomplete)]
        for c in status.generate
    ]
    results = _table(["generate condition", "done", "errors", "empty"], completion_rows)
    if not long_df.empty:
        means = (
            long_df.groupby(["grade_condition_slug", "gen_condition_slug"])["score"]
            .mean()
            .reset_index()
        )
        mean_rows = [
            [r.grade_condition_slug, r.gen_condition_slug, f"{r.score:.3f}"]
            for r in means.itertuples()
        ]
        parse_fail = int((~long_df["parse_ok"].astype(bool)).sum())
        empties = int(long_df["solution"].isna().sum())
        results += (
            "\n\nMean score per condition (**descriptive, not analysis**):\n\n"
            + _table(["grade condition", "generate condition", "mean score"], mean_rows)
            + f"\n\nParse failures: {parse_fail} · rows without solution text: {empties}"
        )

    # 4. Costs — per-stage and per-provider spend, savings decomposition.
    stage_spend = []
    if not ledger.empty and "stage" in ledger.columns:
        for stage in ("generate", "grade"):
            rows = ledger[ledger["stage"] == stage]
            stage_spend.append(
                [stage, _usd(pd.to_numeric(rows["usd"], errors="coerce").fillna(0.0).sum())]
            )
    costs = _table(["stage", "spend"], stage_spend)
    if cost.by_provider:
        costs += "\n\n" + _table(
            ["provider", "calls", "spend", "list price", "saved"],
            [
                [p.provider, str(p.calls), _usd(p.usd), _usd(p.baseline_usd), _usd(p.savings_usd)]
                for p in cost.by_provider
            ],
        )
    costs += (
        f"\n\nSavings vs list price: {_usd(cost.total_savings_usd)} "
        f"({cost.savings_pct:.0f}%) — cache {_usd(cost.cache_savings_usd)}, "
        f"batch {_usd(cost.batch_savings_usd)}."
    )

    # 5. Reproduce — the config inline + pins + the command.
    config_text = (
        config.config_path.read_text(encoding="utf-8")
        if config.config_path and config.config_path.is_file()
        else yaml.safe_dump(json.loads(json.dumps(front)), sort_keys=False)
    )
    pins = "\n".join(
        f"- `{ds.dataset_id}` @ `{ds.revision}` ({len(ds.items)} items)" for ds in prep.datasets
    )
    reproduce = (
        "Dataset pins (from `dataset_locks.json`, copied into this snapshot):\n\n"
        f"{pins}\n\n"
        "Config (verbatim):\n\n"
        f"```yaml\n{config_text.rstrip()}\n```\n\n"
        "Run `itemeval generate config.yaml` then `itemeval grade config.yaml` with "
        "these pins to reproduce the design (provider snapshots may differ — compare "
        "`served_model` above)."
    )

    return (
        f"---\n{front_text}\n---\n\n"
        f"# Study card — {config.study} / {snapshot_name}\n\n"
        f"Frozen snapshot created {created_at}. Files in this directory are an "
        "immutable copy; no compute path reads them.\n\n"
        f"## Design\n\n{design}\n\n"
        f"## Execution\n\n{execution}\n\n"
        f"## Results (descriptive)\n\n{results}\n\n"
        f"## Costs\n\n{costs}\n\n"
        f"## Reproduce\n\n{reproduce}\n"
    )
