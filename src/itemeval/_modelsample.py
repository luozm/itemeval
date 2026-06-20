"""Resolve `solvers.sample` to a concrete, reproducible, pinned model list.

Engine-free (no inspect import). The draw is deterministic given
`(seed, sorted universe ids)`; the result is pinned in `model_locks.json` so
resume/status see a stable model set and a later run reuses the frozen draw.
A drifting universe (the roster grew, or a file/list was edited) only *warns*;
a changed sample spec (n/seed/stratify_by/where/source) *fails loudly*.
"""

import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from itemeval._config import (
    PRICING_TABLE_UNIVERSE,
    ExperimentConfig,
    ModelSample,
    ModelUniverseFilter,
)
from itemeval._errors import ConfigError
from itemeval._util import atomic_write_bytes, canonical_json, sha256_hex, utc_now_iso
from itemeval.budget._pricing import PricingTable

MODEL_LOCKS_VERSION = 1


class ModelSampleResult(BaseModel):
    """Provenance of a resolved model sample (append-only)."""

    model_config = ConfigDict(extra="forbid")

    source: str  # "pricing-table" | "explicit" | "file"
    universe_size: int
    universe_hash: str  # 12 hex over canonical-json of the sorted universe ids
    n: int
    seed: int
    stratify_by: str | None
    allocation: str = "proportional"  # per-stratum apportionment (proportional | equal)
    include: list[str] = Field(default_factory=list)  # pinned ids counted against n
    exclude: list[str] = Field(default_factory=list)  # ids removed from the universe before drawing
    models: list[str]  # the drawn ids, sorted
    pinned_now: bool = False  # this run wrote model_locks.json
    universe_drift: bool = False  # universe changed since the pin (frozen draw stands)


class _LockSpec(BaseModel):
    """Canonical, normalized form of the pinned `sample` spec used for lock
    comparison. Both the freshly-computed spec and the one read back from
    `model_locks.json` are passed through this, so an additive field absent from
    an *older* lock (a later top-level knob; a new optional `where` sub-field)
    defaults in and compares equal by construction — only a real change to a
    shared field still mismatches. This is what prevents the lock-spec brick:
    raw-dict inequality used to hard-fail (and brick every command, read-only
    included) the moment a package update grew the spec.

    Mirrors the identity-bearing fields of `ModelSample` (+ the resolved
    `source`); keep it in sync as fields are added — a field missing *here* is
    exactly how the historical brick happened. `extra="ignore"` so a *newer*
    lock's unknown field never bricks an older reader either.
    """

    model_config = ConfigDict(extra="ignore")

    source: str
    n: int
    seed: int
    stratify_by: str | None = None
    allocation: str = "proportional"
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    where: ModelUniverseFilter | None = None


def _normalized_spec(raw: "dict | None") -> "dict | None":
    """A stored spec re-parsed through the current schema (None if malformed or
    absent — treated as a mismatch, the original raw-compare behavior)."""
    if not isinstance(raw, dict):
        return None
    try:
        return _LockSpec.model_validate(raw).model_dump()
    except ValidationError:
        return None


def stratum(model: str) -> str:
    """Org segment used for provider stratification.

    Handles the `openrouter/<org>/<model>` shape (org is the second segment)
    and native `<org>/<model>` ids (org is the first).
    """
    parts = model.split("/")
    if parts[0] == "openrouter" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _is_routing_alias(model_id: str) -> bool:
    """True for OpenRouter ids that route to a *moving* target rather than a
    pinned snapshot — `-latest`/`:latest` aliases and `~`-prefixed variant
    routes. Such an id can't be reproducibly pinned in a draw, so it is dropped
    from the drawable pricing-table universe (like free models); name one
    directly in `solvers.models` to use it anyway.
    """
    return model_id.endswith(("-latest", ":latest")) or "/~" in model_id


def _price_tier(out_usd: "float | None") -> str:
    """Tier by output (completion) $/Mtok. Fixed edges (documented in the wiki)."""
    if out_usd is None:
        return "unknown"
    if out_usd <= 0:
        return "free"
    if out_usd <= 1:
        return "low"
    if out_usd <= 10:
        return "mid"
    return "high"


def _context_tier(ctx: "int | None") -> str:
    """Tier by context window (tokens). Fixed edges (documented in the wiki)."""
    if not ctx:
        return "unknown"
    if ctx <= 32_000:
        return "short"
    if ctx <= 128_000:
        return "medium"
    if ctx <= 400_000:
        return "long"
    return "xlong"


def _stratum_value(model: str, dim: str, pricing: PricingTable) -> str:
    """The stratum a model falls in for `stratify_by` dimension `dim`.

    `provider` is id-derived (works for any universe); the rest read roster
    metadata (config validation confines them to a pricing-table universe).
    """
    if dim == "provider":
        return stratum(model)
    p = pricing.models.get(model)
    if dim == "reasoning":
        return "reasoning" if (p and p.reasoning) else "non-reasoning"
    if dim == "multimodal":
        return "multimodal" if (p and p.multimodal) else "text-only"
    if dim == "price_tier":
        return _price_tier(p.output_usd_per_mtok if p else None)
    if dim == "context_tier":
        return _context_tier(p.context_length if p else None)
    if dim == "recency":
        # UTC calendar year of the release date — a pure function of `created`,
        # so a pinned table tiers identically (no wall-clock edges that age).
        if p is None or p.created is None:
            return "unknown"
        return str(datetime.fromtimestamp(p.created, tz=timezone.utc).year)
    return stratum(model)  # defensive: unknown dim never reaches here (validated)


def _released_after_ts(date_str: str) -> int:
    """A YYYY-MM-DD cutoff as a Unix timestamp (UTC midnight)."""
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _apply_where(ids: list[str], sample: ModelSample, pricing: PricingTable) -> list[str]:
    where = sample.where
    cutoff = _released_after_ts(where.released_after) if where.released_after is not None else None
    out = []
    for k in ids:
        p = pricing.models.get(k)
        if where.provider is not None and stratum(k) not in where.provider:
            continue
        if where.max_output_usd_per_mtok is not None and (
            p is None or p.output_usd_per_mtok > where.max_output_usd_per_mtok
        ):
            continue
        if where.reasoning is not None and (p is None or bool(p.reasoning) != where.reasoning):
            continue
        if where.multimodal is not None and (p is None or bool(p.multimodal) != where.multimodal):
            continue
        # output_text_only: a drawable model always has output_modalities (text_model
        # requires "text" in it); drop those that emit more than text.
        if where.output_text_only is not None:
            text_only = p is not None and set(p.output_modalities or []) == {"text"}
            if text_only != where.output_text_only:
                continue
        if where.min_context_length is not None and (
            p is None or (p.context_length or 0) < where.min_context_length
        ):
            continue
        # released_after: drop undated models (can't prove they're recent enough).
        if cutoff is not None and (p is None or p.created is None or p.created < cutoff):
            continue
        out.append(k)
    return out


def _build_universe(
    sample: ModelSample, pricing: PricingTable, input_base: Path
) -> "tuple[str, list[str]]":
    """(source, sorted unique universe ids) for the configured universe."""
    universe = sample.universe
    if isinstance(universe, list):
        source, ids = "explicit", list(universe)
    elif universe == PRICING_TABLE_UNIVERSE:
        # The roster is the openrouter/* models OpenRouter lists as runnable
        # text->text chat models (text_model set on refresh) — a reliable
        # universe, not the raw catalog (which also carries meta/router entries).
        # Free ($0 output) models are dropped: they are rate-limited :free
        # endpoints, not representative of the paid models a measurement frame
        # samples. They stay in the pricing table (so lookup_price still prices
        # one named directly in solvers.models); they are just not *drawable*.
        # Free edge matches _price_tier (output_usd_per_mtok <= 0). Routing
        # aliases (-latest/:latest/~-prefixed) are likewise dropped — they
        # resolve to a moving target, so a pinned draw can't reproduce them.
        ids = [
            k
            for k, p in pricing.models.items()
            if k.startswith("openrouter/")
            and p.text_model
            and p.output_usd_per_mtok > 0
            and not _is_routing_alias(k)
        ]
        if not ids:
            raise ConfigError(
                "solvers.sample universe: pricing-table has no runnable, non-free text models — "
                "run with --refresh-pricing to fetch the current OpenRouter roster "
                "(with model metadata), or use an explicit list/file universe"
            )
        if sample.where is not None:
            ids = _apply_where(ids, sample, pricing)
            if not ids:
                extra = (
                    " (if released_after dropped everything, the table may lack release "
                    "dates — run with --refresh-pricing)"
                    if sample.where.released_after is not None
                    else ""
                )
                raise ConfigError(
                    "solvers.sample.where excluded every priced openrouter/* model — "
                    "loosen where (provider allowlist / max_output_usd_per_mtok / "
                    f"released_after){extra}"
                )
        source = "pricing-table"
    else:
        # any other string -> a file of ids, one per line ('#' comments / blanks skipped)
        path = (input_base / universe).resolve()
        if not path.is_file():
            raise ConfigError(f"solvers.sample universe file not found: {path}")
        ids = [
            s
            for line in path.read_text(encoding="utf-8").splitlines()
            if (s := line.strip()) and not s.startswith("#")
        ]
        if not ids:
            raise ConfigError(f"solvers.sample universe file has no model ids: {path}")
        source = "file"
    # exclude is universe-agnostic (unlike where): drop the blocklisted ids from
    # any universe type before drawing. include re-adds nothing here (overlap is
    # rejected at config load), so the blocklist is final.
    if sample.exclude:
        blocked = set(sample.exclude)
        ids = [k for k in ids if k not in blocked]
        if not ids:
            raise ConfigError(
                "solvers.sample.exclude removed every model from the universe — "
                "loosen exclude or widen the universe"
            )
    return source, sorted(set(ids))


def _largest_remainder(total: int, sizes: "list[int]") -> "list[int]":
    """Apportion `total` across strata weighted by `sizes`, summing to `total`.

    Hamilton's method: floor each quota, then hand the leftover +1s to the
    largest fractional parts (ties broken by larger weight, then index — fully
    deterministic). With proportional weights every allotment stays <= its size
    (quota `total*size/grand <= size` when `total <= grand`); callers that pass
    non-size weights (equal allocation) clamp to caps separately via `_allocate`.
    """
    grand = sum(sizes)
    if grand == 0:  # degenerate all-zero weights -> distribute as evenly as possible
        sizes = [1] * len(sizes)
        grand = len(sizes)
    quotas = [total * s / grand for s in sizes]
    base = [int(q) for q in quotas]
    order = sorted(
        range(len(sizes)), key=lambda i: (quotas[i] - base[i], sizes[i], -i), reverse=True
    )
    for i in order[: total - sum(base)]:
        base[i] += 1
    return base


def _allocate(
    n: int,
    keys: "list[str]",
    weights: "dict[str, int]",
    floors: "dict[str, int]",
    caps: "dict[str, int]",
) -> "dict[str, int]":
    """Apportion `n` across `keys`, balanced ~ `weights`, with
    ``floors[k] <= alloc[k] <= caps[k]`` (precondition ``sum(floors) <= n <=
    sum(caps)``, guaranteed by the config + universe-size checks).

    Iterative fix-and-reapportion: a stratum whose proportional quota lands below
    its floor (purposive `include` pins) or above its cap (too few drawable
    models) is fixed at that bound and the remaining budget re-apportioned over
    the rest. Converges — each pass fixes >= 1 stratum. Pure (no rng).
    """
    fixed: "dict[str, int]" = {}
    while True:
        free = [k for k in keys if k not in fixed]
        if not free:
            return fixed
        budget = n - sum(fixed.values())
        q = _largest_remainder(budget, [weights[k] for k in free])
        alloc = dict(zip(free, q))
        changed = False
        for k in free:
            if alloc[k] < floors[k]:
                fixed[k] = floors[k]
                changed = True
            elif alloc[k] > caps[k]:
                fixed[k] = caps[k]
                changed = True
        if not changed:
            return {**fixed, **alloc}


def _draw(universe: "list[str]", sample: ModelSample, pricing: PricingTable) -> "list[str]":
    """Deterministic seeded draw of `n` ids from the sorted universe.

    `random.Random(seed).sample` is stable across runs and CPython versions; the
    lock pins the result regardless, so any drift can only matter before the
    first pin. `include` ids are always present and counted against `n`; the
    random draw fills the rest from ``universe \\ include``. When stratified,
    pins count toward each stratum's balanced share (as floors), `allocation`
    (proportional | equal) balances the *final* per-stratum counts, and the fill
    tops each stratum up to its target.
    """
    rng = random.Random(sample.seed)
    include = sorted(set(sample.include))
    fill_pool = sorted(set(universe) - set(include))

    if sample.stratify_by is None:
        fill_n = sample.n - len(include)
        fill = rng.sample(fill_pool, fill_n) if fill_n else []
        return sorted(set(include) | set(fill))

    dim = sample.stratify_by
    drawable: "dict[str, list[str]]" = {}
    for mid in fill_pool:  # fill_pool is sorted -> each drawable[k] is sorted
        drawable.setdefault(_stratum_value(mid, dim, pricing), []).append(mid)
    floors = dict(Counter(_stratum_value(m, dim, pricing) for m in include))
    uni_size = dict(Counter(_stratum_value(m, dim, pricing) for m in universe))
    if dim == "recency" and set(uni_size) == {"unknown"}:
        raise ConfigError(
            "solvers.sample.stratify_by: recency needs model release dates, but the "
            "pricing table has none — run with --refresh-pricing to fetch them"
        )
    keys = sorted(set(drawable) | set(floors))
    weights = (
        {k: 1 for k in keys}
        if sample.allocation == "equal"
        else {k: uni_size.get(k, 0) for k in keys}
    )
    floors_d = {k: floors.get(k, 0) for k in keys}
    caps = {k: floors_d[k] + len(drawable.get(k, [])) for k in keys}
    final = _allocate(sample.n, keys, weights, floors_d, caps)
    drawn = list(include)
    for k in keys:
        fill_k = final[k] - floors_d[k]
        if fill_k:
            drawn.extend(rng.sample(drawable[k], fill_k))
    return sorted(drawn)


def read_model_lock(path: Path) -> "dict | None":
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"corrupt model lock file {path}: {e}") from e


def _write_model_lock(
    path: Path, spec: dict, universe_hash: str, universe: "list[str]", models: "list[str]"
) -> None:
    data = {
        "version": MODEL_LOCKS_VERSION,
        "resolved_at": utc_now_iso(),
        "sample": spec,
        "universe_hash": universe_hash,
        "universe": universe,
        "models": models,
    }
    atomic_write_bytes(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def resolve_model_sample(
    config: ExperimentConfig, pricing: PricingTable, locks_path: Path
) -> "ModelSampleResult | None":
    """Resolve `solvers.sample`: draw or reuse the pinned set, mutate
    `config.solvers.models` to it, return the provenance (None when no sample).

    Mutating `config.solvers.models` lets grid/manifest/card record the drawn
    set unchanged. The config then holds both `models` and `sample`, which the
    load-time XOR validator forbids — but assignment never re-validates (pydantic
    `validate_assignment` is off) and nothing re-validates the resolved config.
    """
    sample = config.solvers.sample
    if sample is None:
        return None

    source, universe = _build_universe(sample, pricing, config._input_base)
    # include pins are counted against n; the random draw fills the rest from
    # universe \ include, so the fill (not n) must fit the non-included pool.
    fill_needed = sample.n - len(sample.include)
    fill_pool_size = len(set(universe) - set(sample.include))
    if fill_needed > fill_pool_size:
        reasons = []
        if sample.where is not None:
            reasons.append("the where filter may be too tight")
        if sample.include:
            reasons.append(f"include reserves {len(sample.include)} of n")
        tail = f" ({'; '.join(reasons)})" if reasons else ""
        raise ConfigError(
            f"solvers.sample.n ({sample.n}) exceeds the {len(universe)}-model "
            f"universe available to draw{tail}"
        )
    universe_hash = sha256_hex(canonical_json(universe).encode("utf-8"))[:12]
    # Built (and compared) through _LockSpec so additive fields normalize — see
    # _LockSpec for why a raw dict here would re-introduce the lock-spec brick.
    spec = _LockSpec(
        source=source,
        n=sample.n,
        seed=sample.seed,
        stratify_by=sample.stratify_by,
        allocation=sample.allocation,
        include=sorted(sample.include),
        exclude=sorted(sample.exclude),
        where=sample.where,
    ).model_dump()

    lock = read_model_lock(locks_path)
    if lock is not None:
        if _normalized_spec(lock.get("sample")) != spec:
            raise ConfigError(
                f"solvers.sample spec changed since {locks_path.name} was written "
                f"(was {lock.get('sample')}, now {spec}) — clear {locks_path.name} to "
                "re-draw; existing solutions for previously-sampled models remain"
            )
        models = list(lock["models"])
        config.solvers.models = models
        return ModelSampleResult(
            source=source,
            universe_size=len(universe),
            universe_hash=universe_hash,
            n=sample.n,
            seed=sample.seed,
            stratify_by=sample.stratify_by,
            allocation=sample.allocation,
            include=sorted(sample.include),
            exclude=sorted(sample.exclude),
            models=models,
            pinned_now=False,
            universe_drift=lock.get("universe_hash") != universe_hash,
        )

    models = _draw(universe, sample, pricing)
    config.solvers.models = models
    _write_model_lock(locks_path, spec, universe_hash, universe, models)
    return ModelSampleResult(
        source=source,
        universe_size=len(universe),
        universe_hash=universe_hash,
        n=sample.n,
        seed=sample.seed,
        stratify_by=sample.stratify_by,
        allocation=sample.allocation,
        include=sorted(sample.include),
        exclude=sorted(sample.exclude),
        models=models,
        pinned_now=True,
        universe_drift=False,
    )
