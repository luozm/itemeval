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
    "openrouter-unpinned-cache",
    "empty-solutions",
    "dev-policy-at-scale",
    "unpriced-models",
    "pilot-available",
    "estimate-is-ceiling",
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


def detect_split_head_below_min(
    *,
    stage: str,
    heads_below: int,
    heads_total: int,
    min_tokens: int,
    model: str,
    head_tokens: "int | None" = None,
) -> "Hint | None":
    """A split layout's shared head falls below the provider's cache minimum.

    Estimate-time: split_prompt/split_rubric is on but the shared head (per
    condition when static, per item otherwise) estimates below the provider's
    minimum cacheable prefix — the provider cache silently does nothing for
    those groups. Token counts are the chars/4 heuristic; the caller only
    reports heads clearly below (est < min, no fudge factor).
    """
    if heads_below <= 0:
        return None
    option = "split_prompt" if stage == "generate" else "split_rubric"
    if heads_total == 1:
        message = (
            f"{option} is on but the shared head is ~{head_tokens} tokens, "
            f"under {model}'s ~{min_tokens}-token cache minimum (chars/4 "
            "estimate) — it will silently do nothing"
        )
    else:
        noun = "prompt" if stage == "generate" else "judge"
        message = (
            f"{option} is on but {heads_below}/{heads_total} {noun} heads are "
            f"under {model}'s ~{min_tokens}-token cache minimum (chars/4 "
            "estimate) — those groups will not engage the provider cache"
        )
    return Hint(
        code="split-head-below-min",
        message=message,
        learn_more="Cost-Savings#two-gotchas",
    )


def detect_anthropic_openrouter_no_split(*, stage: str, models: "list[str]") -> "Hint | None":
    """Anthropic-style models run monolithic through OpenRouter — known zero discount.

    A monolithic prompt through OpenRouter is a single string-content user
    message, which inspect's openrouter provider never marks with a
    cache_control breakpoint (verified live 2026-06-12 on inspect 0.3.239:
    cache_write=0 on every call). The caller passes only models whose
    discount would otherwise have been projected; the estimator suppresses
    it for these conditions, so the projection already shows full price.
    """
    if not models:
        return None
    option = "split_prompt" if stage == "generate" else "split_rubric"
    return Hint(
        code="anthropic-openrouter-no-split",
        message=(
            f"{', '.join(models)} won't get cache discounts via OpenRouter "
            f"without {option} — monolithic prompts get no cache marker "
            "(the projection shows full price)"
        ),
        learn_more="Cost-Savings#prompt-packaging",
    )


def detect_openrouter_unpinned_cache(models: "list[str]") -> "Hint | None":
    """Anthropic models ran cached through OpenRouter without provider_routing.

    OpenRouter may route to upstreams (Bedrock/Vertex) that ignore the cache
    markers — observed live as cache_read=0 at full price. The caller passes
    only models where caching was active and no routing object was configured.
    """
    if not models:
        return None
    return Hint(
        code="openrouter-unpinned-cache",
        message=(
            f"{', '.join(models)} ran cached via OpenRouter without "
            "solvers/graders provider_routing — routing may land on an "
            "upstream that ignores cache markers (silent full price)"
        ),
        learn_more="Cost-Savings#openrouter-or-direct",
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


def detect_pilot_available(*, store_is_empty: bool, dev_items: int) -> "Hint | None":
    """The money gate engaged with no completed rows behind it — a cheap pilot exists.

    The caller checks gate engagement (projection > confirm_above_usd); this
    fires only when the stage's store holds zero rows for the selected
    conditions. That can also happen on a study that already has rows under
    other conditions (e.g. after config drift), so the message must not claim
    "first run".
    """
    if not store_is_empty:
        return None
    return Hint(
        code="pilot-available",
        message=(
            "no completed rows yet for these conditions — you can pilot cheaply "
            f"first (--policy dev runs {dev_items} items), then re-run at full "
            "scope; completed work is never re-paid"
        ),
        learn_more="Cost-Savings#never-pay-twice",
    )


def detect_estimate_is_ceiling(*, observed_rows: int, projected_usd: float) -> "Hint | None":
    """A money-spending stage has no observations yet, so its projection is a
    pure upper bound (output assumed at max_tokens). A `--policy dev` pilot would
    calibrate an expected cost. Fires only at cold start (no rows to learn from)
    and only when the stage would actually spend."""
    if observed_rows > 0 or projected_usd <= 0:
        return None
    return Hint(
        code="estimate-is-ceiling",
        message=(
            "this is an upper bound (output assumed at max_tokens) — "
            "run --policy dev to calibrate an expected cost"
        ),
        learn_more="Budget-and-Costs#expected-cost",
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
