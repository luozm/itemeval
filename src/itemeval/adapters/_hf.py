"""HuggingFace dataset adapter: pinned revision + field mapping -> canonical Items."""

import re
from pathlib import Path
from typing import Any

from itemeval._config import DatasetSpec, MappingSpec
from itemeval._errors import AdapterError
from itemeval._item import Item
from itemeval._util import canonical_json
from itemeval.adapters._base import LoadedDataset


def _dataset_cache_dir(cache_root: "str | Path", dataset_id: str) -> Path:
    """The `datasets` library materializes a repo under <root>/<id with / -> ___>."""
    return Path(cache_root) / dataset_id.replace("/", "___")


def _dir_size_bytes(path: Path) -> "int | None":
    """Best-effort recursive size; None when the directory can't be walked."""
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return None


_ID_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _synthesize_id(
    record: "dict[str, Any]", index: int, mapping: MappingSpec, dataset_id: str
) -> str:
    """Build Item.id from mapping.id: a column, joined columns, or a template.

    Segments join with ":". A segment with "{" is a template over record columns
    plus a synthetic "{dataset}" token (the dataset basename); a plain segment is
    a column name (today's behavior — a single plain column is byte-for-byte
    unchanged). None falls back to the row index.
    """
    if mapping.id is None:
        return str(index)
    segments = [mapping.id] if isinstance(mapping.id, str) else mapping.id
    dataset_token = dataset_id.split("/")[-1]

    def render(seg: str) -> str:
        if "{" not in seg and "}" not in seg:  # plain column name
            if seg not in record:
                raise AdapterError(
                    f"dataset {dataset_id!r}: mapping.id column {seg!r} not in record "
                    f"(available: {sorted(record)})"
                )
            return str(record[seg])

        def sub(m: "re.Match[str]") -> str:
            key = m.group(1)
            if key == "dataset":
                return dataset_token
            if key in record:
                return str(record[key])
            raise AdapterError(
                f"dataset {dataset_id!r}: unknown mapping.id placeholder '{{{key}}}' "
                f"(available: dataset, {', '.join(sorted(record))})"
            )

        rendered = _ID_PLACEHOLDER.sub(sub, seg)
        if "{" in rendered or "}" in rendered:
            raise AdapterError(
                f"dataset {dataset_id!r}: malformed mapping.id segment {seg!r} "
                "(unbalanced '{' or '}')"
            )
        return rendered

    return ":".join(render(seg) for seg in segments)


def _record_to_item(
    record: "dict[str, Any]", index: int, mapping: MappingSpec, dataset_id: str
) -> Item:
    def require(column: str) -> Any:
        if column not in record:
            raise AdapterError(
                f"dataset {dataset_id!r}: mapped column {column!r} not in record "
                f"(available: {sorted(record)})"
            )
        return record[column]

    item_id = _synthesize_id(record, index, mapping, dataset_id)

    input_val = require(mapping.input)
    input_text = "" if input_val is None else str(input_val)
    if not input_text.strip():
        raise AdapterError(
            f"dataset {dataset_id!r}: empty input (column {mapping.input!r}) for item {item_id!r}"
        )

    target = ""
    if mapping.target:
        tv = require(mapping.target)
        target = "" if tv is None else str(tv)

    grading_scheme = None
    if mapping.grading_scheme:
        gv = require(mapping.grading_scheme)
        if gv is not None:
            grading_scheme = gv if isinstance(gv, str) else canonical_json(gv)

    metadata = {col: record.get(col) for col in mapping.metadata}
    return Item(
        id=item_id,
        input=input_text,
        target=target,
        grading_scheme=grading_scheme,
        metadata=metadata,
    )


class HFAdapter:
    def resolve_revision(self, spec: DatasetSpec) -> str:
        from huggingface_hub import HfApi

        try:
            info = HfApi().dataset_info(spec.id, revision=spec.revision)
        except Exception as e:
            raise AdapterError(f"failed to resolve revision for {spec.id!r}: {e}") from e
        if not info.sha:
            raise AdapterError(f"HF Hub returned no commit SHA for {spec.id!r}")
        return info.sha

    def load(self, spec: DatasetSpec, mapping: MappingSpec, revision: str) -> LoadedDataset:
        import datasets

        # Fresh-vs-reused detection (Law 1): the repo's materialization dir in
        # the global HF datasets cache, checked before load_dataset touches it.
        cache_root = Path(str(datasets.config.HF_DATASETS_CACHE))
        repo_cache = _dataset_cache_dir(cache_root, spec.id)
        cached_before = repo_cache.is_dir() and any(repo_cache.iterdir())
        try:
            ds = datasets.load_dataset(spec.id, name=spec.name, split=spec.split, revision=revision)
        except Exception as e:
            raise AdapterError(
                f"failed to load {spec.id!r} (split={spec.split!r}, revision={revision!r}): {e}"
            ) from e
        if spec.limit is not None:
            ds = ds.select(range(min(spec.limit, len(ds))))
        items = [_record_to_item(record, idx, mapping, spec.id) for idx, record in enumerate(ds)]
        return LoadedDataset(
            dataset_id=spec.id,
            adapter="hf",
            split=spec.split,
            name=spec.name,
            revision_requested=spec.revision,
            revision=revision,
            items=items,
            cache="reused" if cached_before else "downloaded",
            cache_dir=str(cache_root),
            download_bytes=None if cached_before else _dir_size_bytes(repo_cache),
        )
