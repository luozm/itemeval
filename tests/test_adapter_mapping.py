import pytest
import yaml

from itemeval import ExperimentConfig
from itemeval._config import MappingSpec
from itemeval._errors import AdapterError
from itemeval.adapters._base import get_adapter, load_items, read_locks, write_locks
from itemeval.adapters._hf import _record_to_item

MAPPING = MappingSpec(
    id="problem_idx",
    input="problem",
    target="sample_solution",
    grading_scheme="grading_scheme",
    metadata=["points"],
)


def test_record_to_item_full_mapping():
    record = {
        "problem_idx": 3,
        "problem": "Q?",
        "sample_solution": "A",
        "grading_scheme": [{"desc": "d"}],
        "points": 7,
    }
    item = _record_to_item(record, 0, MAPPING, "d/s")
    assert item.id == "3"
    assert item.input == "Q?"
    assert item.target == "A"
    assert item.grading_scheme == '[{"desc":"d"}]'  # canonical JSON for non-str
    assert item.metadata == {"points": 7}


def test_record_to_item_id_falls_back_to_index():
    item = _record_to_item({"q": "Q?"}, 5, MappingSpec(input="q"), "d/s")
    assert item.id == "5"
    assert item.target == ""


def test_record_to_item_missing_column():
    with pytest.raises(AdapterError, match="'problem'"):
        _record_to_item({"problem_idx": 1}, 0, MAPPING, "d/s")


def test_record_to_item_empty_input():
    with pytest.raises(AdapterError, match="empty input"):
        _record_to_item({"q": "   "}, 0, MappingSpec(input="q"), "d/s")


def test_record_to_item_string_grading_scheme_passthrough():
    item = _record_to_item(
        {"q": "Q?", "gs": "text scheme"},
        0,
        MappingSpec(input="q", grading_scheme="gs"),
        "d/s",
    )
    assert item.grading_scheme == "text scheme"


def test_record_to_item_missing_metadata_column_is_none():
    item = _record_to_item({"q": "Q?"}, 0, MappingSpec(input="q", metadata=["absent"]), "d/s")
    assert item.metadata == {"absent": None}


def test_record_to_item_id_list_joins_columns():
    item = _record_to_item(
        {"q": "Q?", "a": "x", "b": "y"}, 0, MappingSpec(input="q", id=["a", "b"]), "org/ds"
    )
    assert item.id == "x:y"


def test_record_to_item_id_dataset_token():
    # BACKLOG example: org/set_2026 + problem_idx 6 -> "set_2026:6".
    item = _record_to_item(
        {"q": "Q?", "problem_idx": 6},
        0,
        MappingSpec(input="q", id=["{dataset}", "problem_idx"]),
        "org/set_2026",
    )
    assert item.id == "set_2026:6"


def test_record_to_item_id_template_string_same_result():
    item = _record_to_item(
        {"q": "Q?", "problem_idx": 6},
        0,
        MappingSpec(input="q", id="{dataset}:{problem_idx}"),
        "org/set_2026",
    )
    assert item.id == "set_2026:6"


def test_record_to_item_id_unknown_placeholder():
    with pytest.raises(AdapterError, match="unknown mapping.id placeholder"):
        _record_to_item({"q": "Q?"}, 0, MappingSpec(input="q", id="{nope}"), "org/ds")


def test_record_to_item_id_stray_brace():
    with pytest.raises(AdapterError, match="malformed mapping.id"):
        _record_to_item(
            {"q": "Q?", "dataset": "d"}, 0, MappingSpec(input="q", id="{dataset"), "org/ds"
        )


def test_record_to_item_id_missing_list_column():
    with pytest.raises(AdapterError, match="mapping.id column 'absent'"):
        _record_to_item({"q": "Q?"}, 0, MappingSpec(input="q", id=["absent"]), "org/ds")


def test_get_adapter_unknown():
    with pytest.raises(AdapterError, match="unknown adapter"):
        get_adapter("github")


def test_locks_roundtrip(tmp_path):
    path = tmp_path / "locks.json"
    assert read_locks(path) == {}
    write_locks(path, {"d/s": "abc123"})
    assert read_locks(path) == {"d/s": "abc123"}


def test_locks_corrupt(tmp_path):
    path = tmp_path / "locks.json"
    path.write_text("{not json")
    with pytest.raises(AdapterError, match="corrupt"):
        read_locks(path)


CONFIG = """\
study: a
benchmark:
  adapter: hf
  datasets: [{id: fake/ds}]
  mapping: {id: problem_idx, input: problem}
solvers: {models: [mockllm/m]}
facets: {prompt: [p], scorer: exact_match}
"""


def test_load_items_pins_revision_at_first_run(tmp_path, offline_adapter):
    cfg = ExperimentConfig.model_validate(yaml.safe_load(CONFIG))
    locks_path = tmp_path / "locks.json"
    loaded = load_items(cfg, locks_path)
    assert len(loaded) == 1
    assert loaded[0].revision == offline_adapter.revision
    assert read_locks(locks_path) == {"fake/ds": offline_adapter.revision}
    # Second load uses the lock (resolve_revision not consulted again).
    offline_adapter.revision = "changed"
    loaded2 = load_items(cfg, locks_path)
    assert loaded2[0].revision != "changed"


def test_load_items_spec_revision_wins(tmp_path, offline_adapter):
    data = yaml.safe_load(CONFIG)
    data["benchmark"]["datasets"][0]["revision"] = "pinned123"
    cfg = ExperimentConfig.model_validate(data)
    locks_path = tmp_path / "locks.json"
    write_locks(locks_path, {"fake/ds": "older456"})
    loaded = load_items(cfg, locks_path)
    assert loaded[0].revision == "pinned123"
    assert read_locks(locks_path) == {"fake/ds": "pinned123"}


def test_load_items_duplicate_ids_across_datasets(tmp_path, offline_adapter):
    data = yaml.safe_load(CONFIG)
    data["benchmark"]["datasets"] = [{"id": "fake/ds"}, {"id": "fake/ds2"}]
    cfg = ExperimentConfig.model_validate(data)
    with pytest.raises(AdapterError, match="duplicate item id") as exc:
        load_items(cfg, tmp_path / "locks.json")
    assert "composite mapping.id" in str(exc.value)  # guard points at the fix


def test_load_items_composite_id_resolves_duplicate(tmp_path, offline_adapter):
    data = yaml.safe_load(CONFIG)
    data["benchmark"]["datasets"] = [{"id": "fake/ds"}, {"id": "fake/ds2"}]
    data["benchmark"]["mapping"]["id"] = ["{dataset}", "problem_idx"]
    cfg = ExperimentConfig.model_validate(data)
    loaded = load_items(cfg, tmp_path / "locks.json")
    ids = [it.id for ds in loaded for it in ds.items]
    assert ids == ["ds:1", "ds:2", "ds:3", "ds2:1", "ds2:2", "ds2:3"]
