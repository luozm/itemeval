"""Local-file dataset adapter: load by path, pin by content hash (no network)."""

import json

import pandas as pd
import pytest

from itemeval._config import DatasetSpec, MappingSpec
from itemeval._errors import AdapterError
from itemeval.adapters._base import get_adapter
from itemeval.adapters._local import LocalAdapter

MAPPING = MappingSpec(
    id="problem_idx",
    input="problem",
    target="sol",
    grading_scheme="scheme",
    metadata=["proofbench_scheme"],
)


def _write_parquet(tmp_path):
    df = pd.DataFrame(
        [
            {
                "problem_idx": 1,
                "problem": "P1",
                "sol": "S1",
                "scheme": "H1",
                "proofbench_scheme": "PB1",
            },
            {
                "problem_idx": 2,
                "problem": "P2",
                "sol": "S2",
                "scheme": "H2",
                "proofbench_scheme": "PB2",
            },
        ]
    )
    path = tmp_path / "ds.parquet"
    df.to_parquet(path)
    return path


def test_registry_resolves_local():
    assert isinstance(get_adapter("local"), LocalAdapter)


def test_local_load_parquet_and_pin(tmp_path):
    path = _write_parquet(tmp_path)
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path))
    rev = adapter.resolve_revision(spec)
    assert rev and len(rev) == 40
    loaded = adapter.load(spec, MAPPING, rev)
    assert loaded.adapter == "local"
    assert loaded.revision == rev
    assert len(loaded.items) == 2
    assert [it.id for it in loaded.items] == ["1", "2"]
    assert loaded.items[0].input == "P1"
    assert loaded.items[0].grading_scheme == "H1"
    assert loaded.items[0].metadata["proofbench_scheme"] == "PB1"


def test_local_hash_mismatch_refused(tmp_path):
    path = _write_parquet(tmp_path)
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path))
    with pytest.raises(AdapterError, match="content hash"):
        adapter.load(spec, MAPPING, "deadbeef")


def test_local_changed_file_detected(tmp_path):
    path = _write_parquet(tmp_path)
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path))
    rev = adapter.resolve_revision(spec)
    # mutate the file -> hash changes -> the old pin is refused
    pd.DataFrame(
        [{"problem_idx": 1, "problem": "X", "sol": "Y", "scheme": "Z", "proofbench_scheme": "Q"}]
    ).to_parquet(path)
    with pytest.raises(AdapterError, match="changed since it was locked"):
        adapter.load(spec, MAPPING, rev)


def test_local_json_list(tmp_path):
    path = tmp_path / "ds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "problem_idx": 1,
                    "problem": "P",
                    "sol": "S",
                    "scheme": "H",
                    "proofbench_scheme": "PB",
                }
            ]
        ),
        encoding="utf-8",
    )
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path))
    loaded = adapter.load(spec, MAPPING, adapter.resolve_revision(spec))
    assert loaded.items[0].metadata["proofbench_scheme"] == "PB"


def test_local_limit(tmp_path):
    path = _write_parquet(tmp_path)
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path), limit=1)
    loaded = adapter.load(spec, MAPPING, adapter.resolve_revision(spec))
    assert len(loaded.items) == 1


def test_local_missing_file(tmp_path):
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(tmp_path / "nope.parquet"))
    with pytest.raises(AdapterError, match="not found"):
        adapter.resolve_revision(spec)


def test_local_unsupported_format(tmp_path):
    path = tmp_path / "ds.txt"
    path.write_text("nope", encoding="utf-8")
    adapter = LocalAdapter()
    spec = DatasetSpec(id=str(path))
    with pytest.raises(AdapterError, match="unsupported local dataset format"):
        adapter.load(spec, MAPPING, adapter.resolve_revision(spec))
