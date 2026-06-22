"""Strict judge-output parsing. Parse failures are results, never errors."""

import json
import math
import re

from pydantic import BaseModel, ConfigDict

_FENCED_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

# JSON permits only these string escapes: \" \\ \/ \b \f \n \r \t and \uXXXX (4 hex).
# Judges grading math routinely write LaTeX (\ge, \perp, \underbrace, \(...\)) as single
# backslashes inside the JSON "reasoning" string, which is invalid JSON. `_fix_json_escapes`
# walks the text and doubles every backslash that does NOT begin a valid escape — counting
# runs correctly (e.g. \\\perp), which a stateless regex cannot — producing parseable JSON
# without touching legitimate escapes. Used only as a fallback after a strict parse fails,
# so well-formed JSON is never altered and the strict result always wins.
_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")


def _fix_json_escapes(s: str) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] != "\\":
            out.append(s[i])
            i += 1
        elif i + 1 < n and s[i + 1] in '"\\/bfnrt':
            out.append(s[i : i + 2])  # valid two-char escape
            i += 2
        elif s[i + 1 : i + 2] == "u" and _HEX4_RE.match(s[i + 2 : i + 6]):
            out.append(s[i : i + 6])  # valid \uXXXX
            i += 6
        else:
            out.append("\\\\")  # stray backslash -> escape it
            i += 1
    return "".join(out)


class ParsedGrade(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float | None
    reasoning: str | None
    score_raw: str | None  # repr of the raw 'score' JSON value
    parse_ok: bool
    parse_error: str | None  # no_json_object | no_score_in_json |
    #                          score_not_numeric | score_not_finite


def _repair_variants(text: str):
    """`text`, then an escape-repaired copy if it differs (skips a redundant retry)."""
    yield text
    fixed = _fix_json_escapes(text)
    if fixed != text:
        yield fixed


def _loads_dict(text: str):
    """Decode `text` to a JSON object: strict first, then with stray-backslash repair."""
    for candidate in _repair_variants(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _raw_decode_dict(decoder: json.JSONDecoder, text: str):
    """Like `_loads_dict` but via raw_decode, for an object embedded in trailing text."""
    for candidate in _repair_variants(text):
        try:
            obj, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _candidates(completion: str):
    """JSON-object candidates, best-first: fenced blocks last->first, then raw braces.
    Each candidate is decoded strict-first, then with a stray-backslash repair, so a
    math judge's unescaped LaTeX in the reasoning string doesn't sink the parse."""
    for match in reversed(_FENCED_RE.findall(completion)):
        obj = _loads_dict(match.strip())
        if obj is not None:
            yield obj
    decoder = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r"\{", completion)]):
        obj = _raw_decode_dict(decoder, completion[i:])
        if obj is not None:
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
