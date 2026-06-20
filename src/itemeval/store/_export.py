"""Long-format export: one row per grading event, parquet + CSV mirrors.

Also home of `--snapshot`: an immutable, named copy of the just-written
export plus everything needed to interpret it (locks, manifests,
snapshot.json, STUDY_CARD.md). Snapshots are never read by any compute path —
purely an analysis/sharing artifact; consumption = read the parquet like any
export, zip the folder to share.
"""

import json
import os
import re
import shutil

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from itemeval._config import ExperimentConfig
from itemeval._errors import ConfigError, StoreError
from itemeval._harvest import HarvestReport
from itemeval._hints import Hint, detect_unpriced_models
from itemeval._util import utc_now_iso
from itemeval.budget._pricing import PricingProvenance, describe_pricing, load_pricing
from itemeval.budget._report import CostReport, cost_report
from itemeval.store._base import _coerce_to_schema, rel_to_study
from itemeval.store._gradings import read_gradings
from itemeval.store._layout import StudyPaths
from itemeval.store._ledger import read_ledger
from itemeval.store._solutions import read_solutions, truncated_mask

EXPORT_SCHEMA = pa.schema(
    [
        pa.field("study", pa.string()),
        pa.field("item_id", pa.string()),
        pa.field("dataset_id", pa.string()),
        pa.field("dataset_revision", pa.string()),
        pa.field("model", pa.string()),
        pa.field("prompt_name", pa.string()),
        pa.field("prompt_hash", pa.string()),
        pa.field("model_config_name", pa.string()),
        pa.field("replication", pa.int32()),
        pa.field("gen_condition_id", pa.string()),
        pa.field("gen_condition_slug", pa.string()),
        pa.field("grade_condition_id", pa.string()),
        pa.field("grade_condition_slug", pa.string()),
        pa.field("grade_kind", pa.string()),
        pa.field("grader_name", pa.string()),
        pa.field("grader_model", pa.string()),
        pa.field("rubric_name", pa.string()),
        pa.field("rubric_hash", pa.string()),
        pa.field("scorer_name", pa.string()),
        pa.field("score", pa.float64()),
        pa.field("score_raw", pa.string()),
        pa.field("parse_ok", pa.bool_()),
        pa.field("parse_error", pa.string()),
        pa.field("reasoning", pa.string()),
        pa.field("solution", pa.string()),
        # True when the solution was cut at a length cap (max_tokens/model_length)
        # with non-empty text — graded as finished, but a budget cut, not content
        # (truncation-signal). Filter it out of a content-validity analysis.
        pa.field("truncated", pa.bool_()),
        pa.field("judge_completion", pa.string()),
        pa.field("temperature_requested", pa.float64()),
        pa.field("temperature_effective", pa.float64()),
        pa.field("reasoning_effort", pa.string()),
        pa.field("gen_input_tokens", pa.int64()),
        pa.field("gen_output_tokens", pa.int64()),
        pa.field("gen_total_tokens", pa.int64()),
        pa.field("gen_reasoning_tokens", pa.int64()),
        pa.field("gen_usd", pa.float64()),
        pa.field("gen_latency_s", pa.float64()),
        pa.field("grade_input_tokens", pa.int64()),
        pa.field("grade_output_tokens", pa.int64()),
        pa.field("grade_total_tokens", pa.int64()),
        pa.field("grade_usd", pa.float64()),
        pa.field("grade_latency_s", pa.float64()),
        # Run identity (recovery-run-identity): experiment_id + attempt per stage,
        # replacing the old gen_run_id/grade_run_id columns.
        pa.field("gen_experiment_id", pa.string()),
        pa.field("gen_attempt", pa.int32()),
        pa.field("grade_experiment_id", pa.string()),
        pa.field("grade_attempt", pa.int32()),
        pa.field("gen_log_file", pa.string()),
        pa.field("grade_log_file", pa.string()),
        pa.field("created_at", pa.string()),
        # Wave provenance (default 0/null; analysis: df.groupby("wave")).
        pa.field("wave", pa.int32()),
        pa.field("wave_label", pa.string()),
    ]
)


SNAPSHOT_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class SnapshotInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str  # relative to the study dir
    card_path: str  # the STUDY_CARD.md inside the snapshot
    created_at: str
    rows: int
    run_ids: list[str]
    spend_usd: float


class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int
    gradings_parquet: str
    gradings_csv: str
    ledger_csv: str
    generation_usd: float
    grading_usd: float
    # Ledger-vs-row-sums self-consistency (tolerance 1e-6). Reconciliation
    # against provider dashboards is a separate, manual step.
    internally_reconciled: bool
    # Per-provider spend + savings vs plain-API list price (current pricing).
    cost: CostReport
    # Which pricing table the cost figures were computed against.
    pricing: PricingProvenance
    hints: list[Hint] = Field(default_factory=list)
    # Set when export ran with snapshot=NAME / --snapshot NAME:
    snapshot: "SnapshotInfo | None" = None
    snapshot_path: "str | None" = None  # == snapshot.path, the documented return
    # Crash recovery (recoverable-harvest): rows the CLI auto-harvested from a prior
    # run's `.eval` into the store before this export (None = nothing/opt-out). Set
    # by the CLI; the Python `export_study` stays a pure read (use `harvest_study`).
    harvested: "HarvestReport | None" = None


_SOLUTION_COLS = {
    "condition_id": "gen_condition_id",
    "condition_slug": "gen_condition_slug",
    "experiment_id": "gen_experiment_id",
    "attempt": "gen_attempt",
    "input_tokens": "gen_input_tokens",
    "output_tokens": "gen_output_tokens",
    "total_tokens": "gen_total_tokens",
    "reasoning_tokens": "gen_reasoning_tokens",
    "usd": "gen_usd",
    "latency_s": "gen_latency_s",
    "log_file": "gen_log_file",
}

_GRADING_COLS = {
    "experiment_id": "grade_experiment_id",
    "attempt": "grade_attempt",
    "input_tokens": "grade_input_tokens",
    "output_tokens": "grade_output_tokens",
    "total_tokens": "grade_total_tokens",
    "usd": "grade_usd",
    "latency_s": "grade_latency_s",
    "log_file": "grade_log_file",
}


def _write_snapshot(
    config: ExperimentConfig,
    paths: StudyPaths,
    name: str,
    long: "pd.DataFrame",
    ledger: "pd.DataFrame",
    cost,
) -> SnapshotInfo:
    """Materialize an immutable copy of the just-written export (atomic rename).

    Copy, not reference: the current-state layer is mutable (upserts replace),
    so "the table as of pub-1" cannot be reconstructed later — history must be
    materialized at freeze time.
    """
    from importlib.metadata import PackageNotFoundError, version

    from itemeval._prepare import prepare_study
    from itemeval._status import build_status
    from itemeval.report._card import build_study_card

    if not re.match(SNAPSHOT_NAME_RE, name):
        raise ConfigError(f"invalid snapshot name {name!r} (must match {SNAPSHOT_NAME_RE})")
    snap_dir = paths.export_dir / "snapshots" / name
    if snap_dir.exists():
        raise ConfigError(f"snapshot '{name}' exists — choose a new name")

    from itemeval._identity import invocation_handle

    created_at = utc_now_iso()
    try:
        itemeval_version = version("itemeval")
    except PackageNotFoundError:
        itemeval_version = "unknown"

    def _handles(eid_col: str, att_col: str) -> "set[str]":
        # Reconstruct each attempt's manifest basename (the invocation handle) from
        # its (experiment_id, attempt) pair — the snapshot copies manifests by name.
        pairs = long[[eid_col, att_col]].dropna()
        return {invocation_handle(str(e), int(a)) for e, a in zip(pairs[eid_col], pairs[att_col])}

    run_ids = sorted(
        _handles("gen_experiment_id", "gen_attempt")
        | _handles("grade_experiment_id", "grade_attempt")
    )
    spend_usd = float(pd.to_numeric(ledger["usd"], errors="coerce").fillna(0.0).sum())

    tmp = paths.export_dir / "snapshots" / f".tmp-{name}"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "manifests").mkdir(parents=True)
    for filename in ("gradings_long.parquet", "gradings_long.csv", "ledger.csv"):
        shutil.copy2(paths.export_dir / filename, tmp / filename)
    if paths.dataset_locks.is_file():
        shutil.copy2(paths.dataset_locks, tmp / "dataset_locks.json")
    if paths.model_locks.is_file():
        shutil.copy2(paths.model_locks, tmp / "model_locks.json")
    if paths.materialized_rubrics.is_file():
        # The frozen per-item rubrics are the reproducibility record (the
        # condition id only pins the materialize spec, not the generated text).
        shutil.copy2(paths.materialized_rubrics, tmp / "materialized_rubrics.parquet")
    for run_id in run_ids:  # every manifest covering included rows
        src = paths.manifests_dir / f"{run_id}.json"
        if src.is_file():
            shutil.copy2(src, tmp / "manifests" / src.name)

    meta = {
        "name": name,
        "created_at": created_at,
        "itemeval_version": itemeval_version,
        "config_sha256": config.config_sha256 or "",
        "run_ids": run_ids,
        "rows": int(len(long)),
        "gen_conditions": int(long["gen_condition_id"].nunique()),
        "grade_conditions": int(long["grade_condition_id"].nunique()),
        "spend_usd": spend_usd,
    }
    (tmp / "snapshot.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    prep = prepare_study(
        config, allow_spec_drift=True
    )  # archiving the pinned panel, not re-drawing
    card = build_study_card(
        config,
        prep,
        build_status(config, prep),
        long,
        ledger,
        cost,
        snapshot_name=name,
        created_at=created_at,
        run_ids=run_ids,
        spend_usd=spend_usd,
        itemeval_version=itemeval_version,
    )
    (tmp / "STUDY_CARD.md").write_text(card, encoding="utf-8")

    os.replace(tmp, snap_dir)
    return SnapshotInfo(
        name=name,
        path=rel_to_study(paths, snap_dir),
        card_path=rel_to_study(paths, snap_dir / "STUDY_CARD.md"),
        created_at=created_at,
        rows=int(len(long)),
        run_ids=run_ids,
        spend_usd=spend_usd,
    )


def read_snapshots(paths: StudyPaths) -> "list[dict]":
    """snapshot.json metadata for every materialized snapshot, sorted by name."""
    root = paths.export_dir / "snapshots"
    if not root.is_dir():
        return []
    out = []
    for meta_path in sorted(root.glob("*/snapshot.json")):
        try:
            out.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def export_study(config: ExperimentConfig, snapshot: "str | None" = None) -> ExportResult:
    paths = StudyPaths(config.study_dir)
    gradings = read_gradings(paths)
    if gradings.empty:
        raise StoreError("nothing to export: gradings store is empty")
    solutions = read_solutions(paths)
    ledger = read_ledger(paths)

    # Derive the truncated flag before the rename (truncated_mask reads the raw
    # solutions columns: error, stop_reason, solution).
    solutions = solutions.assign(truncated=truncated_mask(solutions))
    sol = solutions.rename(columns=_SOLUTION_COLS)
    sol_cols = [
        "gen_condition_id",
        "gen_condition_slug",
        "item_id",
        "epoch",
        "dataset_id",
        "dataset_revision",
        "model",
        "prompt_name",
        "prompt_hash",
        "model_config_name",
        "temperature_requested",
        "temperature_effective",
        "reasoning_effort",
        "solution",
        "truncated",
        "gen_experiment_id",
        "gen_attempt",
        "gen_input_tokens",
        "gen_output_tokens",
        "gen_total_tokens",
        "gen_reasoning_tokens",
        "gen_usd",
        "gen_latency_s",
        "gen_log_file",
    ]
    grad = gradings.rename(columns=_GRADING_COLS)
    long = grad.merge(
        sol[sol_cols],
        how="left",
        on=["gen_condition_id", "item_id", "epoch"],
    )
    long["replication"] = long["epoch"]
    # A grading with no matching solution row (left join) gets NaN truncated -> False.
    long["truncated"] = long["truncated"].fillna(False).astype(bool)
    long = long[list(EXPORT_SCHEMA.names)]

    paths.export_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = paths.export_dir / "gradings_long.parquet"
    csv_path = paths.export_dir / "gradings_long.csv"
    ledger_csv = paths.export_dir / "ledger.csv"

    long = _coerce_to_schema(long, EXPORT_SCHEMA)
    try:
        table = pa.Table.from_pandas(long, schema=EXPORT_SCHEMA, preserve_index=False)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise StoreError(f"export schema cast failed: {e}") from e
    pq.write_table(table, parquet_path)
    long.to_csv(csv_path, index=False)
    ledger.to_csv(ledger_csv, index=False)

    def _usd_sum(series: pd.Series) -> float:
        return float(pd.to_numeric(series, errors="coerce").fillna(0.0).sum())

    gen_rows_usd = _usd_sum(solutions["usd"]) if not solutions.empty else 0.0
    grade_rows_usd = _usd_sum(gradings["usd"])
    if ledger.empty:
        ledger_gen = ledger_grade = 0.0
    else:
        ledger_gen = _usd_sum(ledger[ledger["stage"] == "generate"]["usd"])
        ledger_grade = _usd_sum(ledger[ledger["stage"] == "grade"]["usd"])
    reconciled = (
        abs(ledger_gen - gen_rows_usd) <= 1e-6 and abs(ledger_grade - grade_rows_usd) <= 1e-6
    )

    pricing = load_pricing(config.budget.pricing_path, config._input_base)
    report = cost_report(ledger, pricing)
    provenance = describe_pricing(pricing, refreshed=False)
    unpriced = detect_unpriced_models(report.unpriced_models)

    snapshot_info = (
        _write_snapshot(config, paths, snapshot, long, ledger, report)
        if snapshot is not None
        else None
    )

    return ExportResult(
        rows=len(long),
        gradings_parquet=rel_to_study(paths, parquet_path),
        gradings_csv=rel_to_study(paths, csv_path),
        ledger_csv=rel_to_study(paths, ledger_csv),
        generation_usd=ledger_gen,
        grading_usd=ledger_grade,
        internally_reconciled=reconciled,
        cost=report,
        pricing=provenance,
        hints=[unpriced] if unpriced else [],
        snapshot=snapshot_info,
        snapshot_path=snapshot_info.path if snapshot_info else None,
    )
