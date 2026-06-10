"""Verifiable scorers: pure functions, no LLM, no inspect, $0.

Invariant shared with the judge parser: parse_ok is False iff parse_error is
set iff score is None.
"""

import math
import re
from typing import Callable

from pydantic import BaseModel, ConfigDict

from itemeval._item import Item

_ANSWER_RE = re.compile(r"(?im)^.*?ANSWER\s*:\s*(.*)$")
_LETTER_RE = re.compile(r"\b([A-Za-z])\b")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


class VerifiableResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float | None  # 1.0/0.0; None when parsing failed
    score_raw: str | None  # extracted answer segment (<=500 chars)
    parse_ok: bool
    parse_error: str | None


def _failure(code: str, raw: "str | None" = None) -> VerifiableResult:
    return VerifiableResult(score=None, score_raw=raw, parse_ok=False, parse_error=code)


def _success(score: float, raw: str) -> VerifiableResult:
    return VerifiableResult(score=score, score_raw=raw[:500], parse_ok=True, parse_error=None)


def extract_answer_segment(text: str) -> str:
    """Last 'ANSWER: ...' line's payload; the whole text if no such line."""
    matches = _ANSWER_RE.findall(text)
    return matches[-1].strip() if matches else text.strip()


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".").strip()


def exact_match(solution: str, item: Item) -> VerifiableResult:
    if not item.target.strip():
        return _failure("empty_target")
    segment = extract_answer_segment(solution)
    return _success(1.0 if _norm(segment) == _norm(item.target) else 0.0, segment)


def multiple_choice(solution: str, item: Item) -> VerifiableResult:
    target = item.target.strip().upper()
    if not re.fullmatch(r"[A-Z]", target):
        return _failure("target_not_letter")
    segment = extract_answer_segment(solution)
    m = _LETTER_RE.search(segment)
    if not m:
        return _failure("no_letter_found", segment[:500])
    return _success(1.0 if m.group(1).upper() == target else 0.0, segment)


def numeric(solution: str, item: Item) -> VerifiableResult:
    target_text = item.target.replace("$", "").replace(",", "")
    try:
        target = float(target_text)
    except ValueError:
        return _failure("target_not_numeric")
    segment = extract_answer_segment(solution).replace("$", "").replace(",", "")
    matches = _NUMBER_RE.findall(segment)
    if not matches:
        return _failure("no_number_found", segment[:500])
    candidate = float(matches[-1])
    score = 1.0 if math.isclose(candidate, target, rel_tol=1e-6, abs_tol=1e-9) else 0.0
    return _success(score, segment)


VERIFIABLE_SCORERS: "dict[str, Callable[[str, Item], VerifiableResult]]" = {
    "exact_match": exact_match,
    "multiple_choice": multiple_choice,
    "numeric": numeric,
}
