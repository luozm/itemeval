"""Grid-completion status report (no model API calls)."""

import pandas as pd
from pydantic import BaseModel, ConfigDict

from itemeval._config import ExperimentConfig
from itemeval._modelsample import ModelSampleResult
from itemeval._prepare import PreparedStudy, prepare_study
from itemeval.store import _gradings, _ledger, _solutions
from itemeval.store._solutions import empty_solution_mask


class DatasetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    revision: str
    n_items: int
    # Provenance (append-only, mirrors the dataset announcement line):
    split: str = ""
    revision_source: str = "resolved"  # "config" | "lock" | "resolved"
    cache: str = "reused"  # "downloaded" | "reused"
    cache_dir: str = ""
    download_bytes: int | None = None
    pinned_now: bool = False


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


class SnapshotStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    created_at: str
    rows: int


class WaveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave: int
    label: str | None = None  # None for wave 0 (never explicitly labeled)
    completed: int  # error-free generate rows in this wave (effective items)
    expected: int  # gen conditions x effective items x replications
    graded: int = 0  # error-free grading rows over this wave's solutions
    grade_expected: int = 0  # grade conditions x this wave's gradable solutions


class StatusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str
    policy: str  # effective policy for this invocation
    policy_source: str = "config"  # "config" | "override"
    config_path: str
    datasets: list[DatasetStatus]
    model_sample: "ModelSampleResult | None" = None  # set when solvers.sample drew the models
    n_items_total: int
    n_items_effective: int
    replications_requested: int
    replications_effective: int
    generate: list[ConditionStatus]
    grade: list[ConditionStatus]
    spend_generate_usd: float
    spend_grade_usd: float
    manifests: list[str]  # sorted filenames
    snapshots: list[SnapshotStatus] = []  # frozen export snapshots, sorted by name
    waves: list[WaveStatus] = []  # per-wave completion; single entry when no waves used


def _usd(series) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0.0).sum())


def build_status(config: ExperimentConfig, prep: "PreparedStudy | None" = None) -> StatusReport:
    # status is read-only: inspect the pinned panel even if the sample spec drifted
    # from the lock (the CLI passes a prep already prepared with this; the Python
    # API path builds one here).
    prep = prep or prepare_study(config, allow_spec_drift=True)
    solutions = _solutions.read_solutions(prep.paths)
    gradings = _gradings.read_gradings(prep.paths)
    ledger = _ledger.read_ledger(prep.paths)

    effective_ids = {it.id for it in prep.items_effective}
    grid_gen_ids = {c.id for c in prep.grid.generate}
    reps = prep.plan.replications
    expected_gen = len(prep.items_effective) * reps
    # rerun policy re-attempts empty completions, so they don't count as done.
    rerun_empty = config.solvers.on_empty == "rerun"

    def gradable_count(df) -> int:
        # gradable = produced a non-empty completion (error null, not blank). With
        # on_empty=grade the empties are gradable too (judged as-is).
        if config.solvers.on_empty == "grade":
            return int(df["error"].isna().sum())
        return int((df["error"].isna() & ~empty_solution_mask(df)).sum())

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
            solutions["item_id"].isin(effective_ids)
            & (solutions["epoch"].astype(int) <= reps)
            & solutions["condition_id"].isin(grid_gen_ids)
        ]
        gradable = gradable_count(scoped)

    # Both sides of done/expected describe the same population: current grid,
    # effective items, epoch <= replications. Gradings of wave epochs or of
    # solutions stranded under drifted conditions are excluded (they showed as
    # >100%); waves get their own per-wave graded counts below.
    grade_scoped = gradings
    if not gradings.empty:
        grade_scoped = gradings[
            gradings["item_id"].isin(effective_ids)
            & (gradings["epoch"].astype(int) <= reps)
            & gradings["gen_condition_id"].isin(grid_gen_ids)
        ]

    grade_status = []
    for cond in prep.grid.grade:
        rows = (
            grade_scoped[grade_scoped["grade_condition_id"] == cond.id]
            if not grade_scoped.empty
            else grade_scoped
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

    waves: list[WaveStatus] = []
    if not solutions.empty and "wave" in solutions.columns:
        # expected comes from the current grid, so exclude rows stranded under
        # drifted (abandoned) conditions — counting them can show >100%.
        in_scope = solutions[
            solutions["item_id"].isin(effective_ids) & solutions["condition_id"].isin(grid_gen_ids)
        ]
        g_waves = gradings
        if not gradings.empty and "wave" in gradings.columns:
            g_waves = gradings[
                gradings["item_id"].isin(effective_ids)
                & gradings["gen_condition_id"].isin(grid_gen_ids)
            ]
        for wave_num, group in in_scope.groupby(in_scope["wave"].astype(int)):
            labels = [v for v in group["wave_label"].dropna().unique() if isinstance(v, str)]
            graded = 0
            if not g_waves.empty and "wave" in g_waves.columns:
                in_wave = g_waves[g_waves["wave"].astype(int) == int(wave_num)]
                graded = int(in_wave["error"].isna().sum())
            waves.append(
                WaveStatus(
                    wave=int(wave_num),
                    label=labels[0] if labels else None,
                    completed=int(group["error"].isna().sum()),
                    expected=len(prep.grid.generate) * len(prep.items_effective) * reps,
                    graded=graded,
                    grade_expected=len(prep.grid.grade) * gradable_count(group),
                )
            )

    from itemeval.store._export import read_snapshots

    snapshots = [
        SnapshotStatus(
            name=meta.get("name", "?"),
            created_at=meta.get("created_at", ""),
            rows=int(meta.get("rows", 0)),
        )
        for meta in read_snapshots(prep.paths)
    ]

    return StatusReport(
        study=config.study,
        policy=prep.plan.policy,
        policy_source=prep.policy_source,
        config_path=str(config.config_path) if config.config_path else "(in-memory)",
        datasets=[
            DatasetStatus(
                id=ds.dataset_id,
                revision=ds.revision,
                n_items=len(ds.items),
                split=ds.split,
                revision_source=ds.revision_source,
                cache=ds.cache,
                cache_dir=ds.cache_dir,
                download_bytes=ds.download_bytes,
                pinned_now=ds.pinned_now,
            )
            for ds in prep.datasets
        ],
        model_sample=prep.model_sample,
        n_items_total=len(prep.items_all),
        n_items_effective=len(prep.items_effective),
        replications_requested=config.facets.replications,
        replications_effective=reps,
        generate=gen_status,
        grade=grade_status,
        spend_generate_usd=spend_gen,
        spend_grade_usd=spend_grade,
        manifests=manifests,
        snapshots=snapshots,
        waves=waves,
    )
