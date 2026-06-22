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


def test_malformed_fenced_block_is_skipped():
    # An unparseable fenced block is skipped; parsing falls through to raw braces.
    completion = '```json\n{bad json,,,}\n```\nFinal: {"score": 5}'
    assert parse_judge_output(completion).score == 5.0


def test_unparseable_brace_is_skipped():
    # A stray '{' that doesn't begin a JSON object is skipped; an earlier object wins.
    completion = '{"score": 5} then a stray { not-json'
    assert parse_judge_output(completion).score == 5.0


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


@pytest.mark.parametrize(
    "completion,score",
    [
        # LaTeX with single backslashes inside the JSON string is invalid JSON; the
        # stray-backslash repair recovers it (the real USAMO math-grading failure mode,
        # where a judge wrote `$k \ge 3$` in the reasoning -> "Invalid \escape").
        ('```json\n{"score": 0, "reasoning": "false for $k \\ge 3$"}\n```', 0.0),
        ('{"score": 4, "reasoning": "uses \\frac{1}{2} and \\perp here"}', 4.0),
        ('blah\n```json\n{"score": 7, "reasoning": "let \\alpha = \\sum x_i"}\n```', 7.0),
        # \u-prefixed LaTeX (\underbrace, \uparrow) is NOT a valid \uXXXX escape
        ('{"score": 2, "reasoning": "see \\underbrace{x} and \\uparrow"}', 2.0),
    ],
)
def test_parse_latex_backslash_recovered(completion, score):
    parsed = parse_judge_output(completion)
    assert parsed.parse_ok
    assert parsed.score == score
    assert parsed.parse_error is None


def test_parse_recovers_backslash_runs():
    # mixed/over-escaped LaTeX runs (\(...\) delimiters + \\\perp) — the real j1 failure
    # mode that a stateless doubling regex cannot fix, but the stateful walker can.
    completion = '```json\n{"score": 0, "reasoning": "assert \\(CF\\\\\\perp BC\\) holds"}\n```'
    parsed = parse_judge_output(completion)
    assert parsed.parse_ok
    assert parsed.score == 0.0


def test_latex_reasoning_preserved_after_repair():
    # the repaired decode must keep the LaTeX (one literal backslash) in the reasoning
    parsed = parse_judge_output('{"score": 1, "reasoning": "$k \\ge 3$"}')
    assert parsed.parse_ok and parsed.score == 1.0
    assert "\\ge" in parsed.reasoning


def test_repair_does_not_rescue_structural_garbage():
    # the repair only fixes invalid escapes, never structural JSON errors
    parsed = parse_judge_output("```json\n{bad json,,,}\n```")
    assert not parsed.parse_ok
    assert parsed.parse_error == "no_json_object"


def test_valid_escapes_unchanged_by_repair():
    # legitimate \n / \" survive: strict parse wins, repair never runs
    parsed = parse_judge_output('{"score": 2, "reasoning": "line1\\nline2 \\"q\\""}')
    assert parsed.parse_ok and parsed.score == 2.0
    assert parsed.reasoning == 'line1\nline2 "q"'
