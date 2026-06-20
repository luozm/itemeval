"""Endpoint request shaping: pure mapping from config to inspect model_args.

No inspect imports — fully unit-testable. The impure chokepoint that consumes
this mapping is ``resolve_model()`` (``_mockmodels.py``), which calls
``get_model(model, **args)`` (always a Model; ``args`` may be empty).
"""

from typing import TYPE_CHECKING, Any, Callable, Union

from itemeval.budget._pricing import provider_of

if TYPE_CHECKING:
    from itemeval._config import ExperimentConfig


def cache_provider_of(model: str) -> str:
    """Provider whose caching rules govern `model` (the upstream for openrouter/*)."""
    segments = model.split("/")
    if segments[0] == "openrouter" and len(segments) > 1:
        return segments[1]
    return segments[0]


def _model_name(model: str) -> str:
    """Bare model name, dot-normalized: openrouter ids use claude-haiku-4.5,
    direct ids claude-haiku-4-5 — compare one spelling."""
    return model.split("/")[-1].lower().replace(".", "-")


def _anthropic_min(model: str) -> int:
    # Anthropic per-model minimum cacheable prompt length, checked 2026-06-12
    # (platform.claude.com prompt-caching docs): 512 Fable 5 / Mythos 5;
    # 2048 Mythos Preview, Opus 4.7, Haiku 3.5; 4096 Haiku 4.5, Opus 4.6,
    # Opus 4.5; 1024 everything else (Opus 4.8, Sonnet 4.x, Opus 4/4.1).
    name = _model_name(model)
    if "fable" in name or "mythos" in name:
        return 2048 if "preview" in name else 512
    if any(m in name for m in ("haiku-4-5", "opus-4-6", "opus-4-5")):
        return 4096
    if any(m in name for m in ("opus-4-7", "haiku-3-5")):
        return 2048
    return 1024


def _google_min(model: str) -> "int | None":
    # Gemini implicit-caching minimums, checked 2026-06-12 (ai.google.dev):
    # 2048 for Gemini 2.5 Flash/Pro; 4096 for Gemini 3.x+ (3.5 Flash, 3.1 Pro).
    # Pre-2.5 Gemini and non-Gemini Google models have no implicit caching.
    name = _model_name(model)
    if "gemini-2-5" in name:
        return 2048
    if any(g in name for g in ("gemini-3", "gemini-4", "gemini-5")):
        return 4096
    return None


# Minimum cacheable prefix per cache provider (the upstream for openrouter/*
# ids). Providers absent here either don't cache through inspect or document
# no minimum — omitted, never guessed (grok: caching automatic but no minimum
# documented; together: none on serverless; mistral: opt-in via a request
# param inspect doesn't send; bedrock: inspect strips cache fields). Numbers
# checked 2026-06-12 against each provider's docs; see the per-provider
# helpers above and docs/COST-OPTIMIZATION.md.
MIN_CACHEABLE_PREFIX_TOKENS: "dict[str, Union[int, Callable[[str], int | None]]]" = {
    "openai": 1024,
    "anthropic": _anthropic_min,
    "google": _google_min,
    "deepseek": 64,
}


def min_cacheable_prefix(model: str) -> "int | None":
    """Provider minimum cacheable prefix for `model`'s prompts; None when the
    provider doesn't cache (through inspect) or documents no minimum."""
    entry = MIN_CACHEABLE_PREFIX_TOKENS.get(cache_provider_of(model))
    if entry is None:
        return None
    return entry(model) if callable(entry) else entry


def merge_provider_ignore(
    provider_routing: "dict[str, Any] | None", ignore_providers: "set[str] | list[str]"
) -> "dict[str, Any] | None":
    """`provider_routing` with `ignore_providers` unioned into its `ignore` list
    (output-validity-reroute). The verbatim OpenRouter routing object is otherwise
    untouched — we only ever *append* to `ignore`, never reorder or drop a key, so
    a study's pinned `order`/`allow_fallbacks` survive. Returns the input unchanged
    when there is nothing to ignore (so a no-op reroute round leaves routing as-is).
    """
    if not ignore_providers:
        return provider_routing
    routing = dict(provider_routing or {})
    existing = routing.get("ignore") or []
    routing["ignore"] = sorted({*existing, *ignore_providers})
    return routing


def model_args_for(
    model: str,
    *,
    provider_routing: "dict[str, Any] | None" = None,
    cache_scheduling: bool = False,
    study: "str | None" = None,
    condition_id: "str | None" = None,
) -> "dict[str, Any]":
    """inspect `model_args` for one condition's model; {} for the common case.

    `provider_routing` is a verbatim OpenRouter provider-routing object
    (pass through, don't rename); it only applies to openrouter/* models —
    direct-API models ignore it (the inert case is warned about at estimate
    time, see routing_warnings).

    With cache scheduling active, direct `openai/*` models get OpenAI's keyed
    caching (names kept verbatim): a `prompt_cache_key` stable across runs and
    phases of the same study+condition — deliberately excluding the run identity
    (experiment_id/attempt) and wave, so a pilot warms the full run — plus
    `prompt_cache_retention: "24h"`,
    which is surcharge-free on OpenAI pricing (checked 2026-06-12).
    Granularity is per-condition, not per-cache-group: model_args are
    per-Model and per-sample keys aren't reachable through GenerateConfig;
    per-condition suffices for routing affinity. OpenRouter does not document
    forwarding these fields, so `openrouter/openai/*` is excluded; batch runs
    are excluded by the caller's cache_scheduling flag (batch reorders calls).
    """
    args: dict[str, Any] = {}
    if provider_routing and provider_of(model) == "openrouter":
        args["provider"] = provider_routing
    if cache_scheduling and provider_of(model) == "openai" and study and condition_id:
        args["prompt_cache_key"] = f"itemeval/{study}/{condition_id}"
        args["prompt_cache_retention"] = "24h"
    return args


def routing_warnings(config: "ExperimentConfig") -> "tuple[list[str], list[str]]":
    """Inert provider_routing warnings per stage: (generate, grade).

    `provider_routing` only shapes openrouter/* requests; setting it in a
    section with no OpenRouter model would silently do nothing (Law: no
    silent no-ops) — warn, never block.
    """
    generate: list[str] = []
    grade: list[str] = []
    if config.solvers.provider_routing is not None and not any(
        provider_of(m) == "openrouter" for m in config.solvers.models
    ):
        generate.append(
            "solvers.provider_routing is set but no openrouter/* model is in "
            "solvers.models — the routing object applies to nothing (inert)"
        )
    for name in config.facets.grader:
        spec = config.grader_spec(name)
        if spec.provider_routing is not None and provider_of(spec.model) != "openrouter":
            grade.append(
                f"graders.{name}.provider_routing is set but its model "
                f"({spec.model}) is not openrouter/* — the routing object "
                "applies to nothing (inert)"
            )
    return generate, grade
