import pytest

from itemeval import Item
from itemeval.grade._verifiable import (
    VERIFIABLE_SCORERS,
    exact_match,
    extract_answer_segment,
    multiple_choice,
    numeric,
)


def _item(target: str) -> Item:
    return Item(id="1", input="q", target=target)


def test_extract_answer_segment_last_wins():
    text = "ANSWER: first\nmore\nANSWER: second\n"
    assert extract_answer_segment(text) == "second"


def test_extract_answer_segment_fallback_full_text():
    assert extract_answer_segment("  just text  ") == "just text"


@pytest.mark.parametrize(
    "solution,target,score",
    [
        ("ANSWER: Paris", "Paris", 1.0),
        ("ANSWER:  paris .", "Paris", 1.0),  # whitespace/case/trailing-dot normalized
        ("ANSWER: London", "Paris", 0.0),
        ("The answer is Paris", "the answer is paris", 1.0),  # no ANSWER line
    ],
)
def test_exact_match(solution, target, score):
    result = exact_match(solution, _item(target))
    assert result.parse_ok
    assert result.score == score


def test_exact_match_empty_target_flagged():
    result = exact_match("ANSWER: x", _item("  "))
    assert not result.parse_ok
    assert result.parse_error == "empty_target"
    assert result.score is None


@pytest.mark.parametrize(
    "solution,target,score",
    [
        ("ANSWER: B", "B", 1.0),
        ("ANSWER: (c)", "C", 1.0),
        ("ANSWER: A", "B", 0.0),
    ],
)
def test_multiple_choice(solution, target, score):
    result = multiple_choice(solution, _item(target))
    assert result.parse_ok
    assert result.score == score


def test_multiple_choice_failures():
    assert multiple_choice("ANSWER: B", _item("BB")).parse_error == "target_not_letter"
    result = multiple_choice("ANSWER: 42", _item("B"))
    assert result.parse_error == "no_letter_found"
    assert result.score is None


@pytest.mark.parametrize(
    "solution,target,score",
    [
        ("ANSWER: 42", "42", 1.0),
        ("ANSWER: 42.0", "42", 1.0),
        ("ANSWER: $1,234.5", "1234.5", 1.0),
        ("ANSWER: 1e3", "1000", 1.0),
        ("ANSWER: -7", "7", 0.0),
        ("first 3 then ANSWER: total is 12", "12", 1.0),  # last number in segment
    ],
)
def test_numeric(solution, target, score):
    result = numeric(solution, _item(target))
    assert result.parse_ok
    assert result.score == score


def test_numeric_failures():
    assert numeric("ANSWER: 5", _item("five")).parse_error == "target_not_numeric"
    assert numeric("ANSWER: none", _item("5")).parse_error == "no_number_found"


def test_invariant_all_scorers():
    cases = [
        (exact_match, "ANSWER: x", ""),
        (multiple_choice, "ANSWER: ?", "B"),
        (numeric, "ANSWER: none", "5"),
        (exact_match, "ANSWER: x", "x"),
    ]
    for scorer, solution, target in cases:
        r = scorer(solution, _item(target))
        assert r.parse_ok == (r.parse_error is None) == (r.score is not None)


def test_registry_complete():
    assert set(VERIFIABLE_SCORERS) == {"exact_match", "multiple_choice", "numeric"}
