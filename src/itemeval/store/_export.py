"""Long-format export: one row per grading event, parquet + CSV mirrors."""

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from itemeval._config import ExperimentConfig
from itemeval._errors import StoreError
from itemeval._hints import Hint, detect_unpriced_models
from itemeval.budget._pricing import PricingProvenance, describe_pricing, load_pricing
from itemeval.budget._report import CostReport, cost_report
from itemeval.store._base import _coerce_to_schema, rel_to_study
from itemeval.store._gradings import read_gradings
from itemeval.store._layout import StudyPaths
from itemeval.store._ledger import read_ledger
from itemeval.store._solutions import read_solutions

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
        pa.field("gen_run_id", pa.string()),
        pa.field("grade_run_id", pa.string()),
        pa.field("gen_log_file", pa.string()),
        pa.field("grade_log_file", pa.string()),
        pa.field("created_at", pa.string()),
    ]
)


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


_SOLUTION_COLS = {
    "condition_id": "gen_condition_id",
    "condition_slug": "gen_condition_slug",
    "run_id": "gen_run_id",
    "input_tokens": "gen_input_tokens",
    "output_tokens": "gen_output_tokens",
    "total_tokens": "gen_total_tokens",
    "reasoning_tokens": "gen_reasoning_tokens",
    "usd": "gen_usd",
    "latency_s": "gen_latency_s",
    "log_file": "gen_log_file",
}

_GRADING_COLS = {
    "run_id": "grade_run_id",
    "input_tokens": "grade_input_tokens",
    "output_tokens": "grade_output_tokens",
    "total_tokens": "grade_total_tokens",
    "usd": "grade_usd",
    "latency_s": "grade_latency_s",
    "log_file": "grade_log_file",
}


def export_study(config: ExperimentConfig) -> ExportResult:
    paths = StudyPaths(config.study_dir)
    gradings = read_gradings(paths)
    if gradings.empty:
        raise StoreError("nothing to export: gradings store is empty")
    solutions = read_solutions(paths)
    ledger = read_ledger(paths)

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
        "gen_run_id",
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
    )
