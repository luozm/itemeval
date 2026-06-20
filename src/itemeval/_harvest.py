"""Project crashed-run `.eval` logs back into the durable stores (recoverable-harvest).

Durable parquet is written all-or-nothing after a clean `eval()` return, so a hard
mid-run death (SIGKILL/OOM, or a force-killed stuck SSL read) leaves a run's tail
only in inspect's on-disk `.eval`. inspect writes `.eval` incrementally — it *is*
the write-ahead log — but our orchestrator never reads it back, so every store
surface (`status`/`export`/`report`) goes blind to the killed run. This module
reads those `.eval` files from disk and runs the **existing** row builders
(`persist_generate_condition`/`persist_grade_condition`) into the stores, making a
killed run's progress readable without re-running.

Boundary (DEVELOPMENT.md): inspect's log readers (`read_eval_log`/`list_eval_logs`)
are inspect imports → confined here (an orchestrator-tier module). Rows out are
itemeval dicts/parquet; nothing inspect crosses back to config/store/CLI.

Idempotent two ways: the classifier skips logs whose rel path is already in the
stores (honest "recovered N" counts), and the store upserts dedup on the content
key regardless — so harvesting the same `.eval` twice can never duplicate rows.
"""

import json
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import BaseModel, ConfigDict

# Store/inspect imports are deferred into the functions so `HarvestReport` stays a
# light import the result models (Generate/Grade/Status/Export) can carry without
# pulling pandas/inspect at module load.

if TYPE_CHECKING:
    import pandas as pd
    from inspect_ai.log import EvalLog, EvalLogInfo

    from itemeval._prepare import PreparedStudy


class HarvestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate_rows: int = 0  # solutions rows projected from disk this call
    grade_rows: int = 0  # judge gradings rows projected from disk this call
    logs: list[str] = []  # relative `.eval` paths harvested this call

    @property
    def rows(self) -> int:
        return self.generate_rows + self.grade_rows

    @property
    def recovered(self) -> bool:
        return bool(self.logs)


def _local_path(loc: str) -> str:
    """A `file://` log location → the plain local path the stores key on.

    inspect's readers return `file://` URIs for `log.location`/`info.name`, but the
    live run stored plain relative paths (`logs/<stage>/x.eval`). Normalizing here
    keeps a harvested row's `log_file` byte-identical to the live form, so the
    classifier match and the content-key dedup both hold. Plain paths pass through.
    """
    if loc.startswith("file://"):
        return url2pathname(urlparse(loc).path)
    return loc


def _harvested_log_files(prep: "PreparedStudy") -> "set[str]":
    """Rel `.eval` paths already projected into the stores — `solutions.log_file`
    (generate) ∪ `gradings.log_file` (judge). Reads *rows*, not `log_index`:
    `log_index` records which logs were *indexed*, not *harvested into rows*."""
    from itemeval.store import _gradings, _solutions

    files: set[str] = set()
    sol = _solutions.read_solutions(prep.paths)
    if not sol.empty and "log_file" in sol.columns:
        files |= {f for f in sol["log_file"].dropna()}
    gr = _gradings.read_gradings(prep.paths)
    if not gr.empty and "log_file" in gr.columns:
        files |= {f for f in gr["log_file"].dropna()}
    return files


def classify_logs(
    prep: "PreparedStudy", stage: str
) -> "tuple[list[EvalLogInfo], list[EvalLogInfo]]":
    """On-disk `.eval` for `stage` split into `(harvested, unharvested)` by whether
    the rel path is already present in the stores. This is the `.eval` lifecycle's
    one home — `recovery-run-identity`'s per-experiment index consumes it and adds
    the `superseded` dimension on top, rather than owning supersession itself."""
    from inspect_ai.log import list_eval_logs

    from itemeval.store._base import rel_to_study

    log_dir = prep.paths.logs_stage_dir(stage)
    infos = list_eval_logs(str(log_dir)) if log_dir.is_dir() else []
    harvested_files = _harvested_log_files(prep)
    harvested: list = []
    unharvested: list = []
    for info in infos:
        rel = rel_to_study(prep.paths, _local_path(info.name))
        (harvested if rel in harvested_files else unharvested).append(info)
    return harvested, unharvested


def _wave_identity(prep: "PreparedStudy", run_id: str) -> "tuple[int, str | None, int]":
    """`(wave, wave_label, epoch_offset)` for a harvested run, read from its
    manifest `manifests/<run_id>.json` — written *before* the eval starts, so it
    survives a hard kill. `rows_from_generate_log` needs these to key the recovered
    rows into the right epoch block; `wave_label` lives only here (and on the rows),
    so the manifest is the one source covering all three. Defaults to wave 0."""
    path = prep.paths.manifests_dir / f"{run_id}.json"
    if not path.is_file():
        return 0, None, 0
    m = json.loads(path.read_text(encoding="utf-8"))
    label = m.get("wave_label")
    return (
        int(m.get("wave", 0) or 0),
        label if isinstance(label, str) else None,
        int(m.get("epoch_offset", 0) or 0),
    )


def _pending_for_log(prep: "PreparedStudy", log: "EvalLog") -> "pd.DataFrame | None":
    """Rebuild the `pending` solutions frame `_judge_rows` needs for a judge log:
    the solution rows its samples reference (each sample's metadata carries
    `gen_condition_id`/`item_id`/`epoch`). None when the log names no solutions or
    the store doesn't hold them yet (harvest generate first — `harvest_study` does).
    """
    from itemeval.store import _solutions

    keys = set()
    for sample in log.samples or []:
        m = sample.metadata or {}
        if "gen_condition_id" in m and "item_id" in m and "epoch" in m:
            keys.add((m["gen_condition_id"], str(m["item_id"]), int(m["epoch"])))
    if not keys:
        return None
    sol = _solutions.read_solutions(prep.paths)
    if sol.empty:
        return None
    have = {(r.condition_id, str(r.item_id), int(r.epoch)): True for r in sol.itertuples()}
    if not keys <= have.keys():
        # A referenced solution isn't in the store yet — can't build every judge
        # row (the builder is strict). Skip; a later harvest (after the generate
        # tail lands) recovers it. In practice harvest_study harvests generate
        # first, so this only guards the standalone grade-stage path.
        return None
    mask = sol.apply(
        lambda r: (r["condition_id"], str(r["item_id"]), int(r["epoch"])) in keys, axis=1
    )
    return sol[mask]


def harvest_stage(prep: "PreparedStudy", stage: str) -> HarvestReport:
    """Project every unharvested `.eval` for one stage into the stores, reusing the
    live row builders. Never raises on a single bad log — a corrupt/partial `.eval`
    (a hard kill can leave one) is skipped, not fatal: this runs on read commands,
    which must not be brought down by a recovery attempt."""
    from inspect_ai.log import read_eval_log

    from itemeval.generate._run import persist_generate_condition
    from itemeval.grade._run import persist_grade_condition
    from itemeval.store._base import rel_to_study

    report = HarvestReport()
    _, unharvested = classify_logs(prep, stage)
    for info in unharvested:
        try:
            log = read_eval_log(info)
            log.location = _local_path(log.location)
            meta = log.eval.metadata or {}
            run_id = meta.get("itemeval_run_id")
            cid = (meta.get("itemeval") or {}).get("condition_id")
            if cid is None or run_id is None or not log.samples:
                continue  # not an itemeval log, or nothing flushed yet
            if stage == "generate":
                cond = next((c for c in prep.grid.generate if c.id == cid), None)
                if cond is None:
                    continue  # condition left the current grid (config changed)
                wave, wave_label, epoch_offset = _wave_identity(prep, run_id)
                _, n, _ = persist_generate_condition(
                    prep,
                    cond,
                    log,
                    run_id,
                    epoch_offset=epoch_offset,
                    wave=wave,
                    wave_label=wave_label,
                )
                report.generate_rows += n
            else:
                cond = next((c for c in prep.grid.grade if c.id == cid and c.kind == "judge"), None)
                if cond is None:
                    continue
                pending = _pending_for_log(prep, log)
                if pending is None or pending.empty:
                    continue
                _, n, _ = persist_grade_condition(prep, cond, pending, log, run_id)
                report.grade_rows += n
        except Exception:
            # A half-written `.eval` (zip truncated by SIGKILL) can fail to read or
            # parse — skip it, never crash the read command that triggered harvest.
            continue
        report.logs.append(rel_to_study(prep.paths, log.location))
    return report


def harvest_study(prep: "PreparedStudy") -> HarvestReport:
    """Project every unharvested generate + grade `.eval` into the stores — the
    read-triggered freshness guarantee, so a crashed run's progress is in the store
    whenever you look. Generate first, then grade, so a judge log's referenced
    solutions are already recovered before its rows are built. Idempotent."""
    g = harvest_stage(prep, "generate")
    j = harvest_stage(prep, "grade")
    return HarvestReport(
        generate_rows=g.generate_rows,
        grade_rows=j.grade_rows,
        logs=[*g.logs, *j.logs],
    )
