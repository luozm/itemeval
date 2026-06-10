"""The one network test: free download of a public HF dataset, revision pinned."""

import pytest

from itemeval._config import DatasetSpec, MappingSpec
from itemeval.adapters._hf import HFAdapter

PINNED_REVISION = "0a2c60f2249e07b8ee76c942bca4f5f87aa959df"


@pytest.mark.network
def test_usamo_2025_pinned_load():
    adapter = HFAdapter()
    spec = DatasetSpec(id="MathArena/usamo_2025", revision=PINNED_REVISION)
    mapping = MappingSpec(
        id="problem_idx",
        input="problem",
        target="sample_solution",
        grading_scheme="grading_scheme",
        metadata=["points"],
    )
    loaded = adapter.load(spec, mapping, PINNED_REVISION)
    assert loaded.revision == PINNED_REVISION
    assert len(loaded.items) == 6
    ids = [item.id for item in loaded.items]
    assert len(set(ids)) == 6
    for item in loaded.items:
        assert item.input.strip()
        assert isinstance(item.grading_scheme, str) and item.grading_scheme
        assert "points" in item.metadata
