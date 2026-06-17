"""Resolve `solvers.sample` to a concrete, reproducible, pinned model list.

Engine-free (no inspect import). The draw is deterministic given
`(seed, sorted universe ids)`; the result is pinned in `model_locks.json` so
resume/status see a stable model set and a later run reuses the frozen draw.
A drifting universe (the roster grew, or a file/list was edited) only *warns*;
a changed sample spec (n/seed/stratify_by/where/source) *fails loudly*.
"""

import json
import random
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from itemeval._config import PRICING_TABLE_UNIVERSE, ExperimentConfig, ModelSample
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
    models: list[str]  # the drawn ids, sorted
    pinned_now: bool = False  # this run wrote model_locks.json
    universe_drift: bool = False  # universe changed since the pin (frozen draw stands)


def stratum(model: str) -> str:
    """Org segment used for provider stratification.

    Handles the `openrouter/<org>/<model>` shape (org is the second segment)
    and native `<org>/<model>` ids (org is the first).
    """
    parts = model.split("/")
    if parts[0] == "openrouter" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _apply_where(ids: list[str], sample: ModelSample, pricing: PricingTable) -> list[str]:
    where = sample.where
    out = []
    for k in ids:
        if where.provider is not None and stratum(k) not in where.provider:
            continue
        if where.max_output_usd_per_mtok is not None:
            price = pricing.models.get(k)
            if price is None or price.output_usd_per_mtok > where.max_output_usd_per_mtok:
                continue
        out.append(k)
    return out


def _build_universe(
    sample: ModelSample, pricing: PricingTable, input_base: Path
) -> "tuple[str, list[str]]":
    """(source, sorted unique universe ids) for the configured universe."""
    universe = sample.universe
    if isinstance(universe, list):
        return "explicit", sorted(set(universe))
    if universe == PRICING_TABLE_UNIVERSE:
        # The roster is the openrouter/* models OpenRouter lists as runnable
        # text->text chat models (text_model set on refresh) — a reliable
        # universe, not the raw catalog (which also carries meta/router entries).
        ids = [k for k, p in pricing.models.items() if k.startswith("openrouter/") and p.text_model]
        if not ids:
            raise ConfigError(
                "solvers.sample universe: pricing-table has no runnable text models — "
                "run with --refresh-pricing to fetch the current OpenRouter roster "
                "(with model metadata), or use an explicit list/file universe"
            )
        if sample.where is not None:
            ids = _apply_where(ids, sample, pricing)
            if not ids:
                raise ConfigError(
                    "solvers.sample.where excluded every priced openrouter/* model — "
                    "loosen where (provider allowlist / max_output_usd_per_mtok)"
                )
        return "pricing-table", sorted(set(ids))
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
    return "file", sorted(set(ids))


def _largest_remainder(total: int, sizes: "list[int]") -> "list[int]":
    """Apportion `total` across strata of the given sizes, summing to `total`.

    Hamilton's method: floor each quota, then hand the leftover +1s to the
    largest fractional parts (ties broken by larger stratum, then index — fully
    deterministic). Each allotment stays <= its stratum size because every quota
    `total*size/grand <= size` when `total <= grand`.
    """
    grand = sum(sizes)
    quotas = [total * s / grand for s in sizes]
    base = [int(q) for q in quotas]
    order = sorted(
        range(len(sizes)), key=lambda i: (quotas[i] - base[i], sizes[i], -i), reverse=True
    )
    for i in order[: total - sum(base)]:
        base[i] += 1
    return base


def _draw(universe: "list[str]", sample: ModelSample) -> "list[str]":
    """Deterministic seeded draw of `n` ids from the sorted universe.

    `random.Random(seed).sample` is stable across runs and CPython versions; the
    lock pins the result regardless, so any drift can only matter before the
    first pin.
    """
    rng = random.Random(sample.seed)
    ids = sorted(universe)
    if sample.stratify_by == "provider":
        groups: "dict[str, list[str]]" = {}
        for mid in ids:
            groups.setdefault(stratum(mid), []).append(mid)
        keys = sorted(groups)
        counts = _largest_remainder(sample.n, [len(groups[k]) for k in keys])
        drawn: "list[str]" = []
        for key, count in zip(keys, counts):
            drawn.extend(rng.sample(sorted(groups[key]), count))
        return sorted(drawn)
    return sorted(rng.sample(ids, sample.n))


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
    if sample.n > len(universe):
        hint = " (the where filter may be too tight)" if sample.where is not None else ""
        raise ConfigError(
            f"solvers.sample.n ({sample.n}) exceeds the {len(universe)}-model universe{hint}"
        )
    universe_hash = sha256_hex(canonical_json(universe).encode("utf-8"))[:12]
    spec = {
        "source": source,
        "n": sample.n,
        "seed": sample.seed,
        "stratify_by": sample.stratify_by,
        "where": sample.where.model_dump() if sample.where is not None else None,
    }

    lock = read_model_lock(locks_path)
    if lock is not None:
        if lock.get("sample") != spec:
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
            models=models,
            pinned_now=False,
            universe_drift=lock.get("universe_hash") != universe_hash,
        )

    models = _draw(universe, sample)
    config.solvers.models = models
    _write_model_lock(locks_path, spec, universe_hash, universe, models)
    return ModelSampleResult(
        source=source,
        universe_size=len(universe),
        universe_hash=universe_hash,
        n=sample.n,
        seed=sample.seed,
        stratify_by=sample.stratify_by,
        models=models,
        pinned_now=True,
        universe_drift=False,
    )
