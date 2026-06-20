"""Drift warnings (growth-ux 1.4): config drift and endpoint drift.

Warnings, not gates (UX-PATTERNS Law 2): each is one self-contained line in
the summary block of generate/grade and in the result's `warnings` list.

- Config drift: a facet name matches stored rows but its content hash
  differs (edited template), or an unchanged slug maps to a new condition id
  (changed sampling param) — existing rows stay under the old condition and
  the run starts a fresh one.
- Endpoint drift: past manifests recorded inconsistent `served_model`
  snapshots for a model id this run uses, or the last run is older than
  ENDPOINT_GAP_DAYS (a cheap staleness proxy) — best-effort, since
  served_model is only known after a run.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT_GAP_DAYS = 30


def _distinct(df, cols: "list[str]"):
    return df[cols].dropna().drop_duplicates().itertuples(index=False)


def _id_hash(condition_id: str) -> str:
    """The digest part of '<slug>--<digest12>' — the slug prefix is identical
    on both sides of a drift, so only the hash makes the change visible."""
    return condition_id.rsplit("--", 1)[-1]


def generate_drift_warnings(grid, solutions_df) -> "list[str]":
    """Config drift for the generate stage: prompt edits and sampling changes."""
    if solutions_df is None or solutions_df.empty:
        return []
    warnings = []
    drifted_prompts = set()
    grid_prompts = {c.prompt_name: c.prompt_hash for c in grid.generate}
    for row in _distinct(solutions_df, ["prompt_name", "prompt_hash"]):
        new = grid_prompts.get(row.prompt_name)
        if new is not None and new != row.prompt_hash:
            drifted_prompts.add(row.prompt_name)
            n = int((solutions_df["prompt_hash"] == row.prompt_hash).sum())
            warnings.append(
                f"prompt '{row.prompt_name}' changed since last run "
                f"(hash {row.prompt_hash[:4]}→{new[:4]}): its {n} existing rows stay "
                "under the old condition; this run starts a fresh condition"
            )
    # Same slug, new id, prompt unchanged: a sampling/layout change moved the cell.
    grid_slugs = {c.slug: c.id for c in grid.generate}
    cols = ["condition_slug", "condition_id", "prompt_name"]
    for row in _distinct(solutions_df, cols):
        new_id = grid_slugs.get(row.condition_slug)
        if (
            new_id is not None
            and new_id != row.condition_id
            and row.prompt_name not in drifted_prompts
        ):
            n = int((solutions_df["condition_id"] == row.condition_id).sum())
            warnings.append(
                f"condition '{row.condition_slug}' changed since last run "
                f"(id {_id_hash(row.condition_id)[:8]}→{_id_hash(new_id)[:8]}, "
                f"e.g. a sampling param): its {n} "
                "existing rows stay under the old condition; this run starts a fresh condition"
            )
    return warnings


def grade_drift_warnings(grid, gradings_df) -> "list[str]":
    """Config drift for the grade stage: rubric edits and grader changes."""
    if gradings_df is None or gradings_df.empty:
        return []
    warnings = []
    drifted_rubrics = set()
    grid_rubrics = {c.rubric_name: c.rubric_hash for c in grid.grade if c.rubric_name}
    for row in _distinct(gradings_df, ["rubric_name", "rubric_hash"]):
        new = grid_rubrics.get(row.rubric_name)
        if new is not None and new != row.rubric_hash:
            drifted_rubrics.add(row.rubric_name)
            n = int((gradings_df["rubric_hash"] == row.rubric_hash).sum())
            warnings.append(
                f"rubric '{row.rubric_name}' changed since last run "
                f"(hash {row.rubric_hash[:4]}→{new[:4]}): its {n} existing rows stay "
                "under the old condition; this run starts a fresh condition"
            )
    grid_slugs = {c.slug: c.id for c in grid.grade}
    cols = ["grade_condition_slug", "grade_condition_id", "rubric_name"]
    distinct = gradings_df[cols].drop_duplicates()
    for row in distinct.itertuples(index=False):
        new_id = grid_slugs.get(row.grade_condition_slug)
        if (
            new_id is not None
            and new_id != row.grade_condition_id
            and row.rubric_name not in drifted_rubrics
        ):
            n = int((gradings_df["grade_condition_id"] == row.grade_condition_id).sum())
            warnings.append(
                f"grade condition '{row.grade_condition_slug}' changed since last run "
                f"(id {_id_hash(row.grade_condition_id)[:8]}→{_id_hash(new_id)[:8]}): "
                f"its {n} existing rows stay "
                "under the old condition; this run starts a fresh condition"
            )
    return warnings


def _manifest_served_history(
    manifests_dir: Path,
) -> "tuple[dict[str, list], dict[str, list], str | None]":
    """Per model id: [(created_at, served_model), ...] and, for openrouter
    models, [(created_at, upstream), ...] from immutable manifests."""
    history: dict[str, list] = {}
    upstreams: dict[str, list] = {}
    latest: "str | None" = None
    if not manifests_dir.is_dir():
        return history, upstreams, latest
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        created = data.get("created_at") or ""
        latest = created if latest is None else max(latest, created)
        cond_models: dict[str, str] = {}
        for c in data.get("grid_generate") or []:
            model = (c.get("payload") or {}).get("model")
            if model:
                cond_models[c["id"]] = model
        for c in data.get("grid_grade") or []:
            model = ((c.get("payload") or {}).get("grader") or {}).get("model")
            if model:
                cond_models[c["id"]] = model
        for cond_id, info in (data.get("endpoints_effective") or {}).items():
            served = (info or {}).get("served_model")
            up = (info or {}).get("upstream")
            model = cond_models.get(cond_id)
            if served and model:
                history.setdefault(model, []).append((created, served))
            if up and model:
                upstreams.setdefault(model, []).append((created, up))
    return history, upstreams, latest


def endpoint_drift_warnings(
    models: "list[str]", manifests_dir: Path, *, gap_days: int = ENDPOINT_GAP_DAYS
) -> "list[str]":
    """Best-effort: compares past runs to past runs (served_model is only
    known after a run) and flags a long gap since the last run as a proxy."""
    history, upstreams, latest = _manifest_served_history(manifests_dir)
    warnings = []
    for model in models:
        entries = sorted(history.get(model) or [])
        served = sorted({s for _, s in entries})
        if len(served) > 1:
            warnings.append(
                f"{model} previously answered as {entries[-2][1]} and now-latest "
                f"{entries[-1][1]} across past runs ({', '.join(served)}); provider "
                "may serve a newer snapshot — rows are distinguishable by experiment_id/attempt"
            )
        hosts_seen = sorted(upstreams.get(model) or [])
        hosts = sorted({u for _, u in hosts_seen})
        if len(hosts) > 1:
            warnings.append(
                f"{model} previously answered from upstream {hosts_seen[-2][1]} and "
                f"now-latest {hosts_seen[-1][1]} across past runs ({', '.join(hosts)}); "
                "caching and pricing differ per host (e.g. Bedrock ignores cache "
                "markers) — pin with provider_routing"
            )
    if latest:
        try:
            stamped = datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - stamped).total_seconds() / 86400.0
        except ValueError:
            age_days = None
        if age_days is not None and age_days > gap_days:
            warnings.append(
                f"last run was {age_days:.0f} days ago (>{gap_days}d): the provider may "
                "now serve a newer snapshot — rows are distinguishable by experiment_id/attempt"
            )
    return warnings
