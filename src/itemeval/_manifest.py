"""Run manifests: full reproducibility record, one JSON per generate/grade run."""

import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from itemeval._config import config_to_jsonable
from itemeval._util import atomic_write_bytes, canonical_json, sha256_hex, utc_now_iso

if TYPE_CHECKING:
    from itemeval._prepare import PreparedStudy
    from itemeval.store._layout import StudyPaths

MANIFEST_VERSION = 1
_TRACKED_PACKAGES = ("inspect-ai", "pandas", "pyarrow", "pydantic", "pyyaml", "datasets")


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    adapter: str
    split: str
    name: str | None = None
    revision_requested: str | None
    revision_resolved: str
    n_items: int
    items_hash: str  # 12 hex over (id, input-hash) pairs in loaded order


class TemplateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str  # relative to config.base_dir where possible
    sha256: str


class ConditionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    payload: dict


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: int = MANIFEST_VERSION
    run_id: str
    stage: Literal["generate", "grade"]
    study: str
    created_at: str
    itemeval_version: str
    python_version: str
    packages: dict[str, str]
    config_path: str
    config_sha256: str
    config: dict
    datasets: list[DatasetManifest]
    solver_templates: list[TemplateManifest]
    rubric_templates: list[TemplateManifest]
    models: list[str]
    graders: dict[str, dict]
    sampling_requested: dict
    sampling_effective: dict[str, Any] | None = None  # backfilled post-run, per condition
    seed: int | None
    policy: str
    replications_requested: int
    replications_effective: int
    items_limit: int | None
    batch: bool | int | None
    grid_generate: list[ConditionManifest]
    grid_grade: list[ConditionManifest]
    conditions_run: list[str]
    estimate_usd: float | None
    cache: bool


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _items_hash(items) -> str:
    pairs = [[it.id, sha256_hex(it.input.encode("utf-8"))[:12]] for it in items]
    return sha256_hex(canonical_json(pairs).encode("utf-8"))[:12]


def _rel_path(path: str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return path


def build_manifest(
    prep: "PreparedStudy",
    stage: str,
    run_id: str,
    conditions_run: "list[str]",
    estimate_usd: "float | None",
) -> Manifest:
    cfg = prep.config
    base = cfg.base_dir
    used_graders = {
        name: cfg.grader_spec(name).model_dump(mode="json") for name in cfg.facets.grader
    }
    sampling = cfg.solvers.model_dump(mode="json")
    sampling.pop("models", None)
    return Manifest(
        run_id=run_id,
        stage=stage,  # type: ignore[arg-type]
        study=cfg.study,
        created_at=utc_now_iso(),
        itemeval_version=_pkg_version("itemeval"),
        python_version=platform.python_version(),
        packages={p: _pkg_version(p) for p in _TRACKED_PACKAGES},
        config_path=str(cfg.config_path) if cfg.config_path else "(in-memory)",
        config_sha256=cfg.config_sha256 or "",
        config=config_to_jsonable(cfg),
        datasets=[
            DatasetManifest(
                id=ds.dataset_id,
                adapter=ds.adapter,
                split=ds.split,
                name=ds.name,
                revision_requested=ds.revision_requested,
                revision_resolved=ds.revision,
                n_items=len(ds.items),
                items_hash=_items_hash(ds.items),
            )
            for ds in prep.datasets
        ],
        solver_templates=[
            TemplateManifest(name=t.name, path=_rel_path(t.path, base), sha256=t.sha256)
            for t in prep.solver_templates.values()
        ],
        rubric_templates=[
            TemplateManifest(name=t.name, path=_rel_path(t.path, base), sha256=t.sha256)
            for t in prep.rubric_templates.values()
        ],
        models=list(cfg.solvers.models),
        graders=used_graders,
        sampling_requested=sampling,
        seed=cfg.solvers.seed,
        policy=prep.plan.policy,
        replications_requested=cfg.facets.replications,
        replications_effective=prep.plan.replications,
        items_limit=prep.plan.items_limit,
        batch=prep.plan.batch,
        grid_generate=[
            ConditionManifest(id=c.id, slug=c.slug, payload=c.payload) for c in prep.grid.generate
        ],
        grid_grade=[
            ConditionManifest(id=c.id, slug=c.slug, payload=c.payload) for c in prep.grid.grade
        ],
        conditions_run=conditions_run,
        estimate_usd=estimate_usd,
        cache=cfg.cache,
    )


def write_manifest(manifest: Manifest, paths: "StudyPaths") -> Path:
    path = paths.manifests_dir / f"{manifest.run_id}.json"
    payload = json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False)
    atomic_write_bytes(path, (payload + "\n").encode("utf-8"))
    return path


def finalize_manifest(manifest_path: Path, sampling_effective: "dict[str, Any]") -> None:
    """Backfill per-condition effective sampling params after the run completes."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["sampling_effective"] = sampling_effective
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    atomic_write_bytes(manifest_path, (payload + "\n").encode("utf-8"))
