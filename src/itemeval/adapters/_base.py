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
            revision = spec.revision
        elif spec.id in locks:
            revision = locks[spec.id]
        else:
            revision = adapter.resolve_revision(spec)
        if locks.get(spec.id) != revision:
            locks[spec.id] = revision
            locks_changed = True
        loaded.append(adapter.load(spec, config.benchmark.mapping, revision))
    if locks_changed:
        write_locks(locks_path, locks)

    seen: dict[str, str] = {}
    for ds in loaded:
        for item in ds.items:
            if item.id in seen:
                raise AdapterError(
                    f"duplicate item id {item.id!r} in datasets "
                    f"{seen[item.id]!r} and {ds.dataset_id!r}"
                )
            seen[item.id] = ds.dataset_id
    return loaded
