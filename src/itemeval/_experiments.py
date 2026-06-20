"""Per-experiment index: the attempt rollup, on top of recoverable-harvest (S).

One file per (stage, experiment_id) at
`manifests/experiments/<stage>.<experiment_id>.json`, listing every attempt and
the current (latest) one. So `status` reads run state as *experiments and
attempts* instead of guessing from the newest manifest, and the future mid-run
tracker (C) has a durable rollup to read.

This adds only the **attempt grouping** on top of S's `.eval` lifecycle and the
content-keyed stores (which already converge per cell, last-write-wins). It does
not own `.eval` harvest or supersession-by-deletion — physical pruning of
superseded logs is deliberately out of scope (see docs/KNOWN-ISSUES.md), because
a prior attempt is the only log for the good cells it alone produced.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from itemeval._util import atomic_write_bytes

if TYPE_CHECKING:
    from itemeval._manifest import Manifest
    from itemeval.store._layout import StudyPaths

INDEX_VERSION = 1


class AttemptEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int
    manifest_file: str  # the invocation handle's manifest basename, e.g. "a1b2c3d4e5f6.a2.json"
    created_at: str
    run_kind: str  # "recovery" | "new" (derived: attempt 1 is new, >1 recovers)


class ExperimentIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index_version: int = INDEX_VERSION
    experiment_id: str
    study: str
    stage: str
    config_sha256: str  # the semantic config digest this experiment_id derives from
    attempts: list[AttemptEntry]
    current_attempt: int  # the latest attempt — the live result set


def _index_path(paths: "StudyPaths", stage: str, experiment_id: str) -> Path:
    return paths.manifests_dir / "experiments" / f"{stage}.{experiment_id}.json"


def update_experiment_index(paths: "StudyPaths", manifest: "Manifest") -> Path:
    """Record this run's attempt in its experiment index (create or append).
    Idempotent on (experiment_id, attempt) — re-recording one attempt replaces it,
    so a re-run never double-counts."""
    from itemeval._identity import invocation_handle

    path = _index_path(paths, manifest.stage, manifest.experiment_id)
    if path.is_file():
        existing = ExperimentIndex.model_validate_json(path.read_text(encoding="utf-8"))
        attempts = [a for a in existing.attempts if a.attempt != manifest.attempt]
    else:
        attempts = []
    attempts.append(
        AttemptEntry(
            attempt=manifest.attempt,
            manifest_file=f"{invocation_handle(manifest.experiment_id, manifest.attempt)}.json",
            created_at=manifest.created_at,
            run_kind="new" if manifest.attempt == 1 else "recovery",
        )
    )
    attempts.sort(key=lambda a: a.attempt)
    index = ExperimentIndex(
        experiment_id=manifest.experiment_id,
        study=manifest.study,
        stage=manifest.stage,
        config_sha256=manifest.config_sha256,
        attempts=attempts,
        current_attempt=max(a.attempt for a in attempts),
    )
    atomic_write_bytes(path, (index.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return path


def read_experiments(paths: "StudyPaths") -> "list[ExperimentIndex]":
    """Every experiment index for this study, sorted by (stage, experiment_id).
    Tolerant of a corrupt/half-written index file (skips it)."""
    root = paths.manifests_dir / "experiments"
    if not root.is_dir():
        return []
    out: "list[ExperimentIndex]" = []
    for p in sorted(root.glob("*.json")):
        try:
            out.append(ExperimentIndex.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
