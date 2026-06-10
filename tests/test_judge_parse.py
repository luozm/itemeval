import pytest

from itemeval.grade._parse import parse_judge_output


@pytest.mark.parametrize(
    "completion,score",
    [
        ('```json\n{"score": 6.5, "reasoning": "ok"}\n```', 6.5),
        ('```\n{"score": 7, "reasoning": "ok"}\n```', 7.0),
        ('no fence {"score": 3.25, "reasoning": "r"} trailing', 3.25),
        ('{"score": "4.5"}', 4.5),  # numeric string accepted
        ('{"score": 0}', 0.0),
    ],
)
def test_parse_success(completion, score):
    parsed = parse_judge_output(completion)
    assert parsed.parse_ok
    assert parsed.score == score
    assert parsed.parse_error is None


def test_last_fenced_block_wins():
    completion = (
        'Draft:\n```json\n{"score": 1, "reasoning": "draft"}\n```\n'
        'Final:\n```json\n{"score": 9, "reasoning": "final"}\n```\n'
    )
    parsed = parse_judge_output(completion)
    assert parsed.score == 9.0
    assert parsed.reasoning == "final"


def test_fenced_preferred_over_raw():
    completion = '{"score": 2} then ```json\n{"score": 8}\n```'
    assert parse_judge_output(completion).score == 8.0


@pytest.mark.parametrize(
    "completion,code",
    [
        ("no json here at all", "no_json_object"),
        ('{"grade": 5}', "no_score_in_json"),
        ('{"score": true}', "score_not_numeric"),
        ('{"score": [1]}', "score_not_numeric"),
        ('{"score": "high"}', "score_not_numeric"),
        ('{"score": null}', "score_not_numeric"),
        ('{"score": Infinity}', "score_not_finite"),
        ('{"score": NaN}', "score_not_finite"),
    ],
)
def test_parse_failures_flagged(completion, code):
    parsed = parse_judge_output(completion)
    assert not parsed.parse_ok
    assert parsed.score is None
    assert parsed.parse_error == code


def test_failure_carries_reasoning_when_extractable():
    parsed = parse_judge_output('{"score": true, "reasoning": "why"}')
    assert parsed.reasoning == "why"
    assert parsed.score_raw == "True"


def test_invariant_parse_ok_iff_no_error():
    for completion in ['{"score": 5}', "garbage", '{"score": "x"}']:
        parsed = parse_judge_output(completion)
        assert parsed.parse_ok == (parsed.parse_error is None)
        assert parsed.parse_ok == (parsed.score is not None)
