"""Per-item metadata columns are exposed to rubric/build templates as {colname}."""

from itemeval._item import Item
from itemeval.grade._judge import _render_values as judge_values
from itemeval.grade._materialize import _render_values as materialize_values


def _item() -> Item:
    return Item(
        id="1",
        input="P",
        target="REF",
        grading_scheme="HUMAN",
        metadata={"proofbench_scheme": "PB", "points": 7, "missing": None},
    )


def test_judge_render_exposes_metadata():
    v = judge_values(_item(), "SOL")
    assert v["proofbench_scheme"] == "PB"
    assert v["points"] == "7"  # stringified (render_template needs str)
    assert v["missing"] == ""  # None -> ""
    # canonical fields still present
    assert v["input"] == "P"
    assert v["solution"] == "SOL"
    assert v["target"] == "REF"
    assert v["grading_scheme"] == "HUMAN"


def test_canonical_fields_win_over_colliding_metadata():
    it = Item(
        id="1",
        input="P",
        target="REF",
        grading_scheme="HUMAN",
        metadata={"input": "EVIL", "grading_scheme": "EVIL", "solution": "EVIL"},
    )
    v = judge_values(it, "SOL")
    assert v["input"] == "P"
    assert v["grading_scheme"] == "HUMAN"
    assert v["solution"] == "SOL"


def test_materialize_render_exposes_metadata_without_solution():
    v = materialize_values(_item())
    assert v["proofbench_scheme"] == "PB"
    assert v["input"] == "P"
    assert "solution" not in v  # no candidate solution at materialize time


def test_no_metadata_is_noop():
    it = Item(id="1", input="P", target="REF", grading_scheme="H")
    v = judge_values(it, "SOL")
    assert set(v) == {"input", "solution", "target", "grading_scheme", "id"}
