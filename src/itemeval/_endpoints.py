"""Endpoint request shaping: pure mapping from config to inspect model_args.

No inspect imports — fully unit-testable. The impure chokepoint that consumes
this mapping is ``resolve_model()`` (``_mockmodels.py``), which turns a
non-empty dict into ``get_model(model, **args)``.
"""

from typing import TYPE_CHECKING, Any

from itemeval.budget._pricing import provider_of

if TYPE_CHECKING:
    from itemeval._config import ExperimentConfig


def cache_provider_of(model: str) -> str:
    """Provider whose caching rules govern `model` (the upstream for openrouter/*)."""
    segments = model.split("/")
    if segments[0] == "openrouter" and len(segments) > 1:
        return segments[1]
    return segments[0]


def model_args_for(
    model: str,
    *,
    provider_routing: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """inspect `model_args` for one condition's model; {} for the common case.

    `provider_routing` is a verbatim OpenRouter provider-routing object
    (pass through, don't rename); it only applies to openrouter/* models —
    direct-API models ignore it (the inert case is warned about at estimate
    time, see routing_warnings).
    """
    args: dict[str, Any] = {}
    if provider_routing and provider_of(model) == "openrouter":
        args["provider"] = provider_routing
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
