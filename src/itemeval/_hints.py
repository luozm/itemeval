"""Hint framework (docs/UX-PATTERNS.md): data-derived, never blocking.

A hint is one observed fact from *this run* plus a doc pointer. Hints never
change behavior (Law 2). The text rendering prints at most 2 per command
(priority = catalog order), dim, on stderr, after the summary block;
`ITEMEVAL_HINTS=off` silences the text rendering. In `--json`, hints ride as
structured data on the result models — never suppressed, never capped.
Hint codes are stable and append-only (Law 7).
"""

import os
import sys

from pydantic import BaseModel, ConfigDict


class Hint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str  # stable, append-only (UX-PATTERNS hint catalog)
    message: str  # the observed fact, plain words, self-contained
    learn_more: str  # wiki-page#anchor owning the explanation


# Priority = catalog order in docs/UX-PATTERNS.md (the table is normative).
CATALOG_ORDER = [
    "cache-zero-reads",
    "split-head-below-min",
    "anthropic-openrouter-no-split",
    "empty-solutions",
    "dev-policy-at-scale",
    "unpriced-models",
    "pilot-available",
]

MAX_HINTS_PER_COMMAND = 2


def hints_enabled() -> bool:
    return os.environ.get("ITEMEVAL_HINTS", "").strip().lower() != "off"


def emit_hints(hints: "list[Hint]", stream=None) -> None:
    """Text rendering: at most 2 hints by catalog priority, dim on a TTY, stderr."""
    if not hints or not hints_enabled():
        return
    stream = stream if stream is not None else sys.stderr

    def rank(h: Hint) -> int:
        return CATALOG_ORDER.index(h.code) if h.code in CATALOG_ORDER else len(CATALOG_ORDER)

    dim, reset = ("\x1b[2m", "\x1b[0m") if stream.isatty() else ("", "")
    for h in sorted(hints, key=rank)[:MAX_HINTS_PER_COMMAND]:
        print(f"{dim}hint: {h.message} — learn more: {h.learn_more}{reset}", file=stream)


# --- Detectors: pure functions over run data; return None when nothing fires ---


def detect_cache_zero_reads(
    *,
    scheduled: bool,
    repeated_prefix_calls: int,
    cache_read_tokens: int,
    real_provider: bool,
) -> "Hint | None":
    """Same-prefix calls were scheduled for provider cache reuse but none engaged.

    `repeated_prefix_calls` counts the calls beyond each group's leader (the
    ones that *should* have read the cache). Mock models never engage provider
    caches, so runs without a real provider are excluded.
    """
    if not (scheduled and real_provider and repeated_prefix_calls > 0):
        return None
    if cache_read_tokens > 0:
        return None
    return Hint(
        code="cache-zero-reads",
        message=(
            f"{repeated_prefix_calls} calls repeated a shared prompt prefix "
            "but no provider cache discount engaged"
        ),
        learn_more="Cost-Savings#two-gotchas",
    )


def detect_empty_solutions(
    empty_total: int, empty_skipped: int, on_empty: str, stop_reasons: "dict[str, int]"
) -> "Hint | None":
    if empty_total <= 0:
        return None
    breakdown = ", ".join(f"{k}×{v}" for k, v in stop_reasons.items())
    return Hint(
        code="empty-solutions",
        message=(
            f"{empty_total} solutions are empty — completed without an API error "
            f"but produced no gradable text [{breakdown}]"
        ),
        learn_more="Error-Handling#empty-completions",
    )


def detect_unpriced_models(unpriced_models: "list[str]") -> "Hint | None":
    if not unpriced_models:
        return None
    n = len(unpriced_models)
    return Hint(
        code="unpriced-models",
        message=(
            f"{n} model{'s' if n != 1 else ''} unpriced "
            f"({', '.join(unpriced_models)}) — dollars missing, run unaffected"
        ),
        learn_more="Budget-and-Costs#pricing-table",
    )
