"""Effective sampling-param extraction from eval logs (requested vs effective)."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from itemeval.design._grid import GenParams

if TYPE_CHECKING:
    from inspect_ai.log import EvalSample


# Headroom (tokens) kept free between the estimated input + output and a model's
# context window when clamping max_tokens — absorbs the chars/4 token heuristic's
# slack and provider special tokens. The floor keeps a clamped model usable.
CONTEXT_FIT_MARGIN = 256
MIN_FIT_MAX_TOKENS = 256


def fit_max_tokens(
    requested: "int | None", context_length: "int | None", input_tokens: int
) -> "tuple[int | None, bool]":
    """Shrink a generate condition's max_tokens to fit the model's own context.

    A model with context window C rejects (HTTP 400) any call where
    ``input + max_tokens > C`` — before doing any work — so a global max_tokens
    larger than a small-context model's window guarantees failure for that model.
    When the roster knows the model's ``context_length``, clamp max_tokens to the
    largest value that still fits the estimated input plus a safety margin;
    otherwise (unknown context, or it already fits) leave it untouched. The clamp
    is applied to the runtime request only — never to the design value that keys
    the condition id. Returns ``(effective_max_tokens, clamped)``.
    """
    if requested is None or context_length is None:
        return requested, False
    budget = context_length - input_tokens - CONTEXT_FIT_MARGIN
    if budget >= requested:
        return requested, False
    return max(MIN_FIT_MAX_TOKENS, budget), True


def effective_context(model_context: "int | None", endpoint_context: "int | None") -> "int | None":
    """The clamp ceiling: the *smaller* of the model-level and endpoint windows.

    The pricing table's ``context_length`` is OpenRouter's model-level max (the
    largest provider's window); a request routed to a floor provider can hit a
    smaller window and 400. When the per-endpoint minimum is known
    (``endpoint-context-clamp``), clamp against it instead. Either input may be
    None (unknown); the result is the min of those that are known, or None when
    both are unknown — so ``fit_max_tokens`` falls back to today's behavior.
    """
    known = [c for c in (model_context, endpoint_context) if c is not None]
    return min(known) if known else None


def resolve_cache_prompt(cache_prompt: str, replications: int) -> "bool | None":
    """Resolve the tri-state ``solvers.cache_prompt`` to the bool inspect's
    ``GenerateConfig`` takes (``None`` = provider default).

    ``auto`` turns provider prompt caching on when the design replicates an item
    (``replications > 1``), where epochs re-send a byte-identical prefix worth
    caching. ``replications`` MUST be the **design** count (``facets.replications``),
    never the policy-adjusted plan value: ``cache_prompt`` rides in inspect's local
    response-cache key, so deriving it from the policy reps would give a dev pilot
    (capped to 1 rep) a different key than the full run — silently defeating the
    cross-run epoch replay a dev->full grow relies on (re-paying generation, then
    staling the pilot's grades against the overwritten solutions). Single home for
    the resolution so the generate task, the cache probe, and the hint never drift.
    """
    if cache_prompt == "on":
        return True
    if cache_prompt == "off":
        return False
    return True if replications > 1 else None  # auto


class EffectiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_tokens: int | None = None


def extract_effective_params(sample: "EvalSample", requested: GenParams) -> EffectiveParams:
    """Effective values from the sample's last model event; requested as fallback.

    Provider-forced values surface as a requested/effective mismatch. Never raises.
    """
    event_config = None
    try:
        for event in reversed(sample.events or []):
            if getattr(event, "event", None) == "model":
                event_config = event.config
                break
    except Exception:
        event_config = None

    def pick(field: str):
        if event_config is not None:
            value = getattr(event_config, field, None)
            if value is not None:
                return value
        return getattr(requested, field)

    return EffectiveParams(
        temperature=pick("temperature"),
        top_p=pick("top_p"),
        max_tokens=pick("max_tokens"),
        reasoning_effort=pick("reasoning_effort"),
        reasoning_tokens=pick("reasoning_tokens"),
    )
