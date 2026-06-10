"""Strict judge-output parsing. Parse failures are results, never errors."""

import json
import math
import re

from pydantic import BaseModel, ConfigDict

_FENCED_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


class ParsedGrade(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float | None
    reasoning: str | None
    score_raw: str | None  # repr of the raw 'score' JSON value
    parse_ok: bool
    parse_error: str | None  # no_json_object | no_score_in_json |
    #                          score_not_numeric | score_not_finite


def _candidates(completion: str):
    """JSON-object candidates, best-first: fenced blocks last->first, then raw braces."""
    for match in reversed(_FENCED_RE.findall(completion)):
        try:
            obj = json.loads(match.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj
    decoder = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r"\{", completion)]):
        try:
            obj, _ = decoder.raw_decode(completion[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_judge_output(completion: str) -> ParsedGrade:
    accepted = None
    saw_dict = False
    for obj in _candidates(completion):
        saw_dict = True
        if "score" in obj:
            accepted = obj
            break

    if accepted is None:
        return ParsedGrade(
            score=None,
            reasoning=None,
            score_raw=None,
            parse_ok=False,
            parse_error="no_score_in_json" if saw_dict else "no_json_object",
        )

    reasoning = None
    if accepted.get("reasoning") is not None:
        reasoning = str(accepted["reasoning"])

    v = accepted["score"]
    score_raw = repr(v)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return ParsedGrade(
            score=None,
            reasoning=reasoning,
            score_raw=score_raw,
            parse_ok=False,
            parse_error="score_not_numeric",
        )
    try:
        score = float(v)
    except ValueError:
        return ParsedGrade(
            score=None,
            reasoning=reasoning,
            score_raw=score_raw,
            parse_ok=False,
            parse_error="score_not_numeric",
        )
    if not math.isfinite(score):
        return ParsedGrade(
            score=None,
            reasoning=reasoning,
            score_raw=score_raw,
            parse_ok=False,
            parse_error="score_not_finite",
        )
    return ParsedGrade(
        score=score, reasoning=reasoning, score_raw=score_raw, parse_ok=True, parse_error=None
    )
