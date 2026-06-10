"""HuggingFace dataset adapter: pinned revision + field mapping -> canonical Items."""

from typing import Any

from itemeval._config import DatasetSpec, MappingSpec
from itemeval._errors import AdapterError
from itemeval._item import Item
from itemeval._util import canonical_json
from itemeval.adapters._base import LoadedDataset


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

    item_id = str(require(mapping.id)) if mapping.id else str(index)

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
        )
