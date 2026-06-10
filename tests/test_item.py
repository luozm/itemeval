import pytest
from pydantic import ValidationError

from itemeval import Item


def test_id_coerced_to_str():
    assert Item(id=7, input="x").id == "7"


def test_input_must_be_non_empty():
    with pytest.raises(ValidationError):
        Item(id="1", input="   ")


def test_defaults():
    item = Item(id="1", input="q")
    assert item.target == ""
    assert item.grading_scheme is None
    assert item.metadata == {}


def test_frozen():
    item = Item(id="1", input="q")
    with pytest.raises(ValidationError):
        item.input = "other"


def test_extra_forbidden():
    with pytest.raises(ValidationError):
        Item(id="1", input="q", bogus=True)
