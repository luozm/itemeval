"""Local-file dataset adapter: a parquet/json/jsonl on disk → canonical Items.

Mirrors the HF adapter but for a file you build yourself (e.g. a join of two
public datasets that no single Hub repo carries). There is no Hub revision to
pin, so the "revision" is the file's **content hash**: a changed file is detected
and refused rather than silently used, preserving the study's everything-pinned
rule. `spec.id` is the file path — absolute, or relative to the current working
directory (the same base as the `studies/` output tree).
"""

from pathlib import Path
from typing import Any

from itemeval._config import DatasetSpec, MappingSpec
from itemeval._errors import AdapterError
from itemeval._util import sha256_hex
from itemeval.adapters._base import LoadedDataset
from itemeval.adapters._hf import _record_to_item

# content-hash length: matches the 40-hex feel of a git SHA, plenty of collision room
_REV_LEN = 40


def _resolve_path(spec_id: str) -> Path:
    path = Path(spec_id).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path)


def _hash_file(path: Path) -> str:
    if not path.is_file():
        raise AdapterError(f"local dataset file not found: {path}")
    return sha256_hex(path.read_bytes())[:_REV_LEN]


def _read_records(path: Path) -> "list[dict[str, Any]]":
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        import json

        import pandas as pd

        # via to_json so numpy scalars become native python (no numpy leakage into
        # Item.metadata / manifests); force_ascii=False keeps LaTeX/unicode verbatim.
        df = pd.read_parquet(path)
        return json.loads(df.to_json(orient="records", force_ascii=False))
    if suffix in (".json", ".jsonl"):
        import json

        text = path.read_text(encoding="utf-8")
        if suffix == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("data", data.get("rows", []))
        if not isinstance(data, list):
            raise AdapterError(f"local dataset {path}: top-level JSON must be a list of records")
        return data
    raise AdapterError(
        f"unsupported local dataset format {suffix!r} for {path} (use .parquet/.json/.jsonl)"
    )


class LocalAdapter:
    """Loads a local parquet/json/jsonl file, pinned by content hash."""

    def resolve_revision(self, spec: DatasetSpec) -> str:
        return _hash_file(_resolve_path(spec.id))

    def load(self, spec: DatasetSpec, mapping: MappingSpec, revision: str) -> LoadedDataset:
        path = _resolve_path(spec.id)
        actual = _hash_file(path)
        if actual != revision:
            raise AdapterError(
                f"local dataset {spec.id!r} content hash {actual!r} != pinned {revision!r} "
                "— the file changed since it was locked. Restore the original file, or delete "
                "the dataset's entry in the study's dataset_locks.json to re-pin the new content."
            )
        records = _read_records(path)
        if spec.limit is not None:
            records = records[: spec.limit]
        items = [_record_to_item(rec, idx, mapping, spec.id) for idx, rec in enumerate(records)]
        return LoadedDataset(
            dataset_id=spec.id,
            adapter="local",
            split=spec.split,
            name=spec.name,
            revision_requested=spec.revision,
            revision=revision,
            items=items,
            cache="local",
            cache_dir=str(path.parent),
            download_bytes=path.stat().st_size,
        )
