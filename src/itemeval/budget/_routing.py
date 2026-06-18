"""Native-provider batch routing: map an OpenRouter-sampled model id to its
native API id when running natively captures the ~50% batch discount.

Pure decision layer — config + ``os.environ`` only, **no inspect import**
(DEVELOPMENT.md keeps the routing decision engine-free; the orchestrators own
the inspect boundary). The native id is an *execution* identity only: the
sampled ``openrouter/*`` id stays the model's scientific identity (condition
ids, ``model_locks``, the ``model`` column). See
``docs/plans/native-batch-routing.md``.

Costs are always read under the **sampled** id (the roster id the pricing table
carries) — the pricing table keys models under OpenRouter's spelling, so the
native id is not reliably priceable, and the same model costs the same either
way. Routing changes only (a) which provider serves the call and (b) batch-
discount eligibility (``provider_of(native) in BATCH_PROVIDERS``).
"""

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from itemeval.budget._pricing import BATCH_PROVIDERS, provider_of

if TYPE_CHECKING:
    from itemeval._config import ExperimentConfig
    from itemeval.budget._policies import EffectivePlan

# OpenRouter inner-provider segment -> native inspect provider prefix, for the
# providers whose native API offers a batch endpoint (BATCH_PROVIDERS). Only the
# segment differs (x-ai -> grok); the rest match. Providers change far more
# slowly than models, so this small curated map is not the hand-maintained
# model-list anti-pattern flagship-selection rejects. [verify] inspect provider
# slugs — checked 2026-06-17 against inspect_ai/model/_providers/providers.py
# (@modelapi names: anthropic, openai, google, grok, together).
OPENROUTER_TO_NATIVE_PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "x-ai": "grok",
    "together": "together",
}

# Native provider -> accepted API-key env var(s); routing requires one set. A
# pure os.environ check, no inspect. [verify] checked 2026-06-17 against the
# inspect_ai provider sources (grok reads XAI_API_KEY or GROK_API_KEY).
NATIVE_API_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY",),
    "grok": ("XAI_API_KEY", "GROK_API_KEY"),
    "together": ("TOGETHER_API_KEY",),
}


class NativeRoute(BaseModel):
    """One sampled->native routing decision (append-only result/manifest field).

    ``sampled`` stays the scientific identity; ``execution`` is the native id the
    calls actually run on; ``provider`` is the native (billing) provider. The
    W2 dual-projection fields are filled by the estimator (defaults elsewhere).
    """

    model_config = ConfigDict(extra="forbid")

    sampled: str
    execution: str
    provider: str
    # W2 dual projection (expected, remaining scope); set by the estimator.
    batch_usd: float = 0.0  # expected native-batch cost
    cache_usd: float | None = None  # expected openrouter-cache cost (None = can't cache)
    cheaper: str | None = None  # "batch" | "cache" verdict


def _native_name(inner: str, name: str) -> str:
    """Native model-name spelling for an OpenRouter name segment.

    OpenRouter writes dotted minor versions (``claude-haiku-4.5``); native
    Anthropic ids use dashes (``claude-haiku-4-5``). OpenAI/Google keep dots and
    grok names match, so the dots->dashes fix is Anthropic-only (a blanket rule
    would corrupt ``openai/gpt-5.1``). The native id only needs to *resolve* in
    inspect, not be priceable. [verify] the exact native spelling with a live
    resolve smoke before any paid run.
    """
    if inner == "anthropic":
        return name.replace(".", "-")
    return name


def native_id(sampled: str) -> "str | None":
    """Native execution id for an ``openrouter/<inner>/<name>`` model, or ``None``
    when the inner provider has no native batch endpoint or the id is not an
    OpenRouter triple. Pure string mapping (no env checks — those gate routing in
    ``eligible_native_routes``)."""
    if provider_of(sampled) != "openrouter":
        return None
    parts = sampled.split("/", 2)
    if len(parts) < 3 or not parts[2]:
        return None
    inner, name = parts[1], parts[2]
    native_provider = OPENROUTER_TO_NATIVE_PROVIDER.get(inner)
    if native_provider is None or native_provider not in BATCH_PROVIDERS:
        return None
    return f"{native_provider}/{_native_name(inner, name)}"


def native_key_present(native_provider: str) -> bool:
    """Whether an API key for ``native_provider`` is set (pure os.environ check)."""
    return any(os.environ.get(v) for v in NATIVE_API_KEY_ENV.get(native_provider, ()))


def _run_models(config: "ExperimentConfig") -> "list[str]":
    """Every distinct model the run will execute: solver models + grader models,
    in order, de-duplicated."""
    models = list(config.solvers.models)
    for name in config.facets.grader:
        models.append(config.grader_spec(name).model)
    return list(dict.fromkeys(models))


def eligible_native_routes(
    config: "ExperimentConfig",
) -> "tuple[dict[str, str], list[str]]":
    """``({sampled: native}, eligible-but-keyless)`` over the run's models,
    independent of the batch/knob gate.

    A model is *eligible* when its inner provider maps to a BATCH_PROVIDERS
    native provider (``native_id`` is not None). Eligible **and** key-present ->
    the first dict (routable); eligible but no native API key -> the second list
    (the inert/why-not note — never silently dropped). This is the substrate the
    estimator uses for the savings hint and the W2 comparison even when routing
    is not active; ``active_native_routes`` applies the batch/knob gate on top.
    """
    routes: dict[str, str] = {}
    unavailable: list[str] = []
    for m in _run_models(config):
        nid = native_id(m)
        if nid is None:
            continue
        if native_key_present(provider_of(nid)):
            routes[m] = nid
        else:
            unavailable.append(m)
    return routes, unavailable


def active_native_routes(config: "ExperimentConfig", plan: "EffectivePlan") -> "dict[str, str]":
    """The routes actually applied this run: ``eligible_native_routes`` gated by
    a batch plan (``plan.batch is not None`` — routing only buys the batch
    discount) and the opt-in ``budget.prefer_native_batch``. Empty otherwise.
    Deterministic given (config, env, plan), so resume is stable."""
    if plan.batch is None or not config.budget.prefer_native_batch:
        return {}
    routes, _ = eligible_native_routes(config)
    return routes
