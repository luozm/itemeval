"""Hermetic coverage for HFAdapter.load / resolve_revision.

The network test (test_adapter_hf.py) exercises these against the real Hub; here
we mock `datasets.load_dataset` and `huggingface_hub.HfApi` so the orchestration
logic (arg forwarding, limit clamping, error wrapping, missing-sha guard) is
covered without any network access.
"""

import datasets
import huggingface_hub
import pytest

from itemeval._config import DatasetSpec, MappingSpec
from itemeval._errors import AdapterError
from itemeval.adapters._hf import HFAdapter

MAPPING = MappingSpec(id="problem_idx", input="problem", target="answer", metadata=["points"])
RECORDS = [
    {"problem_idx": i, "problem": f"Q{i}?", "answer": str(i), "points": 7} for i in (1, 2, 3)
]


class _FakeDS:
    """List-like stand-in for a datasets.Dataset (len / iter / select only)."""

    def __init__(self, records):
        self._records = list(records)

    def __len__(self):
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def select(self, indices):
        return _FakeDS([self._records[i] for i in indices])


def test_load_forwards_args_and_maps_records(monkeypatch):
    seen = {}

    def fake_load_dataset(dataset_id, *, name, split, revision):
        seen.update(id=dataset_id, name=name, split=split, revision=revision)
        return _FakeDS(RECORDS)

    monkeypatch.setattr(datasets, "load_dataset", fake_load_dataset)
    spec = DatasetSpec(id="fake/ds", split="test", name="subset")
    loaded = HFAdapter().load(spec, MAPPING, "rev123")

    assert seen == {"id": "fake/ds", "name": "subset", "split": "test", "revision": "rev123"}
    assert loaded.dataset_id == "fake/ds"
    assert loaded.adapter == "hf"
    assert loaded.split == "test"
    assert loaded.name == "subset"
    assert loaded.revision == "rev123"
    assert [item.id for item in loaded.items] == ["1", "2", "3"]
    assert loaded.items[0].input == "Q1?"
    assert loaded.items[0].metadata == {"points": 7}


def test_load_clamps_limit(monkeypatch):
    monkeypatch.setattr(datasets, "load_dataset", lambda *a, **k: _FakeDS(RECORDS))
    # limit below the dataset size selects a prefix...
    assert len(HFAdapter().load(DatasetSpec(id="d/s", limit=2), MAPPING, "r").items) == 2
    # ...and a limit beyond the size clamps to the available rows (no IndexError).
    assert len(HFAdapter().load(DatasetSpec(id="d/s", limit=99), MAPPING, "r").items) == 3


def test_load_wraps_underlying_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(datasets, "load_dataset", boom)
    with pytest.raises(AdapterError, match=r"failed to load 'd/s'.*revision='r'.*hub exploded"):
        HFAdapter().load(DatasetSpec(id="d/s"), MAPPING, "r")


class _FakeInfo:
    def __init__(self, sha):
        self.sha = sha


def _fake_hfapi(sha=None, raises=None):
    class _FakeHfApi:
        def dataset_info(self, dataset_id, revision=None):
            if raises is not None:
                raise raises
            return _FakeInfo(sha)

    return _FakeHfApi


def test_resolve_revision_returns_sha(monkeypatch):
    monkeypatch.setattr(huggingface_hub, "HfApi", _fake_hfapi(sha="deadbeef"))
    assert HFAdapter().resolve_revision(DatasetSpec(id="d/s")) == "deadbeef"


def test_resolve_revision_missing_sha(monkeypatch):
    monkeypatch.setattr(huggingface_hub, "HfApi", _fake_hfapi(sha=None))
    with pytest.raises(AdapterError, match="no commit SHA"):
        HFAdapter().resolve_revision(DatasetSpec(id="d/s"))


def test_resolve_revision_wraps_errors(monkeypatch):
    monkeypatch.setattr(huggingface_hub, "HfApi", _fake_hfapi(raises=RuntimeError("offline")))
    with pytest.raises(AdapterError, match="failed to resolve revision for 'd/s'.*offline"):
        HFAdapter().resolve_revision(DatasetSpec(id="d/s"))
