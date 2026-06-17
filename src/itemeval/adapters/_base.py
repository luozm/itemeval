"""Adapter protocol, registry, dataset lock file, and load_items orchestration."""

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from itemeval._config import DatasetSpec, ExperimentConfig, MappingSpec
from itemeval._errors import AdapterError
from itemeval._item import Item
from itemeval._util import atomic_write_bytes, utc_now_iso

LOCKS_VERSION = 1


class LoadedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    adapter: str
    split: str
    name: str | None = None
    revision_requested: str | None = None
    revision: str  # resolved commit SHA actually loaded
    items: list[Item]
    # Provenance of this load (Law 1: announced, append-only). The adapter
    # fills cache facts; load_items fills the revision-precedence facts.
    cache: str = "reused"  # "downloaded" (first use) | "reused"
    cache_dir: str = ""  # the global cache the data lives in
    download_bytes: int | None = None  # best-effort; None when unavailable/reused
    revision_source: str = "resolved"  # "config" | "lock" | "resolved"
    pinned_now: bool = False  # this load wrote/changed the lock entry


class DatasetProvenance(BaseModel):
    """JSON rendering of a dataset announcement line (same numbers, Law 6)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    split: str
    name: str | None = None
    revision: str
    revision_source: str
    cache: str
    cache_dir: str
    download_bytes: int | None = None
    pinned_now: bool


def dataset_provenance(datasets: "list[LoadedDataset]") -> "list[DatasetProvenance]":
    return [
        DatasetProvenance(
            id=ds.dataset_id,
            split=ds.split,
            name=ds.name,
            revision=ds.revision,
            revision_source=ds.revision_source,
            cache=ds.cache,
            cache_dir=ds.cache_dir,
            download_bytes=ds.download_bytes,
            pinned_now=ds.pinned_now,
        )
        for ds in datasets
    ]


class Adapter(Protocol):
    def resolve_revision(self, spec: DatasetSpec) -> str: ...

    def load(self, spec: DatasetSpec, mapping: MappingSpec, revision: str) -> LoadedDataset: ...


def get_adapter(name: str) -> Adapter:
    if name == "hf":
        from itemeval.adapters._hf import HFAdapter

        return HFAdapter()
    raise AdapterError(f"unknown adapter: {name!r} (available: hf)")


def read_locks(path: Path) -> "dict[str, str]":
    """Returns {dataset_id: revision}; {} if the lock file is missing."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {ds_id: entry["revision"] for ds_id, entry in data.get("datasets", {}).items()}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        raise AdapterError(f"corrupt dataset lock file {path}: {e}") from e


def write_locks(path: Path, locks: "dict[str, str]") -> None:
    data = {
        "version": LOCKS_VERSION,
        "datasets": {
            ds_id: {"revision": rev, "resolved_at": utc_now_iso()}
            for ds_id, rev in sorted(locks.items())
        },
    }
    atomic_write_bytes(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def load_items(config: ExperimentConfig, locks_path: Path) -> list[LoadedDataset]:
    """Load every configured dataset, pinning revisions at first run via the lock file.

    Revision precedence: spec.revision -> lock entry -> adapter.resolve_revision
    (which then writes the lock). Item ids must be unique across all datasets.
    """
    adapter = get_adapter(config.benchmark.adapter)
    locks = read_locks(locks_path)
    loaded: list[LoadedDataset] = []
    locks_changed = False
    for spec in config.benchmark.datasets:
        if spec.revision is not None:
            revision, source = spec.revision, "config"
        elif spec.id in locks:
            revision, source = locks[spec.id], "lock"
        else:
            revision, source = adapter.resolve_revision(spec), "resolved"
        pinned_now = locks.get(spec.id) != revision
        if pinned_now:
            locks[spec.id] = revision
            locks_changed = True
        ds = adapter.load(spec, config.benchmark.mapping, revision)
        loaded.append(ds.model_copy(update={"revision_source": source, "pinned_now": pinned_now}))
    if locks_changed:
        write_locks(locks_path, locks)

    seen: dict[str, str] = {}
    for ds in loaded:
        for item in ds.items:
            if item.id in seen:
                raise AdapterError(
                    f"duplicate item id {item.id!r} in datasets "
                    f"{seen[item.id]!r} and {ds.dataset_id!r} — if the same natural key "
                    "repeats across datasets, make ids unique with a composite mapping.id "
                    '(e.g. ["{dataset}", <col>]); see Configuration#composite-item-ids'
                )
            seen[item.id] = ds.dataset_id
    return loaded
