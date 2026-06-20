"""W2: resolve + pin model sampling (hermetic — stub roster, tmp lock, no network)."""

import pytest

from itemeval import ExperimentConfig
from itemeval._errors import ConfigError
from itemeval._modelsample import _largest_remainder, resolve_model_sample, stratum
from itemeval.budget._pricing import ModelPrice, PricingTable


def _pricing(out_prices: "dict[str, float]") -> PricingTable:
    return PricingTable(
        updated_at="2026-06-16T00:00:00Z",
        source="seed",
        models={
            mid: ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=p, text_model=True)
            for mid, p in out_prices.items()
        },
    )


# A roster with openrouter/* text models across orgs, plus a native + mock id to exclude.
ROSTER_PRICES = {
    "openrouter/anthropic/claude-a": 10.0,
    "openrouter/anthropic/claude-b": 30.0,
    "openrouter/openai/gpt-a": 20.0,
    "openrouter/openai/gpt-b": 5.0,
    "openrouter/google/gemini-a": 12.0,
    "anthropic/claude-native": 9.0,  # native id — not in the roster universe
    "mockllm/*": 0.0,
}
ROSTER = _pricing(ROSTER_PRICES)
# a meta/router entry with no generation params (text_model False) — must be
# excluded from the sampling universe even though it is openrouter/*:
ROSTER.models["openrouter/router/meta"] = ModelPrice(
    input_usd_per_mtok=1.0, output_usd_per_mtok=1.0, text_model=False
)
ROSTER_IDS = sorted(
    k for k, p in ROSTER.models.items() if k.startswith("openrouter/") and p.text_model
)


def _cfg(sample: "dict | None", models: "list[str] | None" = None) -> ExperimentConfig:
    solvers = {"models": models} if sample is None else {"sample": sample}
    return ExperimentConfig.model_validate(
        {
            "study": "s",
            "benchmark": {
                "adapter": "hf",
                "datasets": [{"id": "x/y"}],
                "mapping": {"input": "q", "target": "a"},
            },
            "solvers": solvers,
            "facets": {"scorer": "numeric"},
        }
    )


def test_stratum_handles_openrouter_prefix():
    assert stratum("openrouter/anthropic/claude-3.5") == "anthropic"
    assert stratum("anthropic/claude-3.5") == "anthropic"
    assert stratum("openai/gpt-5") == "openai"


def test_largest_remainder_sums_and_bounds():
    counts = _largest_remainder(5, [2, 2, 1])
    assert sum(counts) == 5
    assert all(c <= s for c, s in zip(counts, [2, 2, 1]))


def test_no_sample_is_noop(tmp_path):
    cfg = _cfg(None, models=["m/a", "m/b"])
    assert resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json") is None
    assert cfg.solvers.models == ["m/a", "m/b"]


def test_pricing_table_universe_is_runnable_text_models_only(tmp_path):
    cfg = _cfg({"n": 5, "seed": 1, "universe": "pricing-table"})
    res = resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")
    assert res.source == "pricing-table"
    assert res.universe_size == len(ROSTER_IDS)  # excludes native, mock, and the router
    assert set(res.models) <= set(ROSTER_IDS)
    assert "openrouter/router/meta" not in res.models  # non-text/router excluded
    assert cfg.solvers.models == res.models  # config mutated to the draw


def test_pricing_table_drops_routing_aliases(tmp_path):
    # OpenRouter routing aliases (-latest / :latest / ~-prefixed variant routes)
    # resolve to a *moving* target, so a "pinned" draw can't reproduce them —
    # they must not be drawable from the default pricing-table universe, just as
    # free models aren't.
    def _m():
        return ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0, text_model=True)

    table = PricingTable(
        updated_at="t",
        source="seed",
        models={
            "openrouter/a/model-1": _m(),
            "openrouter/a/model-2": _m(),
            "openrouter/a/model-latest": _m(),  # -latest alias
            "openrouter/~b/model-pin": _m(),  # ~-prefixed variant route
            "openrouter/c/model:latest": _m(),  # :latest alias
        },
    )
    cfg = _cfg({"n": 2, "seed": 1, "universe": "pricing-table"})
    res = resolve_model_sample(cfg, table, tmp_path / "model_locks.json")
    assert res.universe_size == 2  # the three aliases are dropped
    assert set(res.models) == {"openrouter/a/model-1", "openrouter/a/model-2"}


def test_pricing_table_without_text_metadata_raises(tmp_path):
    # openrouter/* keys present but none flagged text_model (e.g. a stale cache)
    stale = PricingTable(
        updated_at="t",
        source="seed",
        models={"openrouter/x/y": ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)},
    )
    cfg = _cfg({"n": 1, "seed": 1, "universe": "pricing-table"})
    with pytest.raises(ConfigError, match="runnable, non-free text models.*refresh-pricing"):
        resolve_model_sample(cfg, stale, tmp_path / "model_locks.json")


def test_draw_is_deterministic_and_order_independent(tmp_path):
    res1 = resolve_model_sample(
        _cfg({"n": 3, "seed": 7, "universe": "pricing-table"}), ROSTER, tmp_path / "a.json"
    )
    reversed_roster = _pricing(dict(reversed(list(ROSTER_PRICES.items()))))
    res2 = resolve_model_sample(
        _cfg({"n": 3, "seed": 7, "universe": "pricing-table"}), reversed_roster, tmp_path / "b.json"
    )
    assert res1.models == res2.models  # draw independent of dict order
    res3 = resolve_model_sample(
        _cfg({"n": 3, "seed": 99, "universe": "pricing-table"}), ROSTER, tmp_path / "c.json"
    )
    assert res3.models != res1.models  # different seed -> different draw


def test_stratified_draw_covers_providers(tmp_path):
    cfg = _cfg({"n": 3, "seed": 3, "stratify_by": "provider", "universe": "pricing-table"})
    res = resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")
    assert len(res.models) == 3
    # 3 orgs (anthropic x2, openai x2, google x1) -> one drawn from each
    assert {stratum(m) for m in res.models} == {"anthropic", "openai", "google"}


def test_where_provider_and_price_filter(tmp_path):
    cfg = _cfg({"n": 1, "seed": 1, "universe": "pricing-table", "where": {"provider": ["google"]}})
    res = resolve_model_sample(cfg, ROSTER, tmp_path / "p.json")
    assert res.models == ["openrouter/google/gemini-a"]
    assert res.universe_size == 1
    # price ceiling: only outputs <= 12 qualify (claude-a 10, gpt-b 5, gemini-a 12)
    cfg = _cfg(
        {"n": 3, "seed": 1, "universe": "pricing-table", "where": {"max_output_usd_per_mtok": 12}}
    )
    res = resolve_model_sample(cfg, ROSTER, tmp_path / "q.json")
    assert set(res.models) == {
        "openrouter/anthropic/claude-a",
        "openrouter/openai/gpt-b",
        "openrouter/google/gemini-a",
    }


def test_where_excluding_all_raises(tmp_path):
    cfg = _cfg(
        {"n": 1, "seed": 1, "universe": "pricing-table", "where": {"max_output_usd_per_mtok": 0.1}}
    )
    with pytest.raises(ConfigError, match="excluded every"):
        resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")


def test_empty_pricing_table_universe_raises(tmp_path):
    cfg = _cfg({"n": 1, "seed": 1, "universe": "pricing-table"})
    bare = _pricing({"anthropic/claude": 5.0})  # no openrouter/* keys
    with pytest.raises(ConfigError, match="no runnable, non-free text models"):
        resolve_model_sample(cfg, bare, tmp_path / "model_locks.json")


def test_n_exceeds_universe_raises(tmp_path):
    cfg = _cfg({"n": 9, "seed": 1, "universe": "pricing-table", "where": {"provider": ["google"]}})
    with pytest.raises(ConfigError, match="exceeds.*too tight"):
        resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")


def test_lock_lifecycle_reuse_drift_and_spec_change(tmp_path):
    lock = tmp_path / "model_locks.json"
    spec = {"n": 2, "seed": 5, "universe": "pricing-table"}

    res1 = resolve_model_sample(_cfg(spec), ROSTER, lock)
    assert res1.pinned_now and not res1.universe_drift
    assert lock.is_file()

    res2 = resolve_model_sample(_cfg(spec), ROSTER, lock)  # same spec -> reuse frozen draw
    assert not res2.pinned_now
    assert res2.models == res1.models

    drifted = _pricing({**ROSTER_PRICES, "openrouter/meta/llama": 1.0})  # roster grew
    res3 = resolve_model_sample(_cfg(spec), drifted, lock)
    assert res3.universe_drift and res3.models == res1.models  # warn, draw stands

    with pytest.raises(ConfigError, match="spec changed"):  # n changed -> fail loud
        resolve_model_sample(_cfg({"n": 3, "seed": 5, "universe": "pricing-table"}), ROSTER, lock)


def test_additive_lock_fields_do_not_brick_reuse(tmp_path):
    """A lock written by an older itemeval — before today's additive sample/where
    fields existed — must still match the current spec and reuse the frozen draw,
    not hard-fail. Reproduces the lock-spec brick: a package update added
    where.output_text_only (default None, #18), absent from a pre-update lock, and
    raw-dict spec inequality bricked *every* command (`spec changed … clear
    model_locks.json`) though nothing in the study changed. The fix compares specs
    normalized through the current schema, so an absent additive field defaults
    compatibly while a real change still mismatches."""
    import json

    lock = tmp_path / "model_locks.json"
    spec = {"n": 2, "seed": 5, "universe": "pricing-table", "where": {"provider": ["anthropic"]}}
    res1 = resolve_model_sample(_cfg(spec), ROSTER, lock)
    assert res1.pinned_now

    # Rewrite the lock as an older version wrote it: drop the additive keys that
    # did not exist yet — where.output_text_only / released_after (later releases)
    # and the top-level allocation / include / exclude knobs.
    data = json.loads(lock.read_text())
    for k in ("output_text_only", "released_after"):
        data["sample"]["where"].pop(k, None)
    for k in ("allocation", "include", "exclude"):
        data["sample"].pop(k, None)
    lock.write_text(json.dumps(data))

    res2 = resolve_model_sample(_cfg(spec), ROSTER, lock)  # must reuse, not brick
    assert not res2.pinned_now
    assert res2.models == res1.models


def test_read_only_tolerates_genuine_spec_change(tmp_path):
    """L2: a *genuine* sample-spec change hard-fails the draw/write path
    (generate/grade) as before, but read-only commands (estimate/status/snapshot)
    must still inspect the pinned panel — `allow_spec_drift=True` warns and
    proceeds on the frozen draw rather than bricking, and flags `spec_drift`."""
    lock = tmp_path / "model_locks.json"
    res1 = resolve_model_sample(
        _cfg({"n": 2, "seed": 5, "universe": "pricing-table"}), ROSTER, lock
    )
    assert res1.pinned_now

    changed = _cfg({"n": 3, "seed": 5, "universe": "pricing-table"})  # n: 2 -> 3, a real change
    with pytest.raises(ConfigError, match="spec changed"):  # write/draw path still fails loud
        resolve_model_sample(changed, ROSTER, lock)

    changed = _cfg({"n": 3, "seed": 5, "universe": "pricing-table"})  # fresh cfg (models mutated)
    res2 = resolve_model_sample(changed, ROSTER, lock, allow_spec_drift=True)
    assert res2.spec_drift and not res2.pinned_now
    assert res2.models == res1.models  # the pinned 2-model panel, not a 3-model redraw
    assert res2.n == 2  # described from the STORED spec (the panel in use), not the new n=3
    assert changed.solvers.models == res1.models  # config mutated to the pinned panel


def test_read_only_tolerates_unbuildable_universe(tmp_path):
    """A read path falls back to the pinned panel even when the *current* config no
    longer builds a universe at all (a where that now excludes everything, or an
    offline/stale roster) — the pinned panel is what's being inspected."""
    lock = tmp_path / "model_locks.json"
    res1 = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": "pricing-table"}), ROSTER, lock
    )
    empty = {  # a price ceiling that drops every model -> _build_universe raises
        "n": 2,
        "seed": 1,
        "universe": "pricing-table",
        "where": {"max_output_usd_per_mtok": 0.01},
    }
    with pytest.raises(ConfigError):  # write path: the empty universe is fatal
        resolve_model_sample(_cfg(empty), ROSTER, lock)
    res2 = resolve_model_sample(_cfg(empty), ROSTER, lock, allow_spec_drift=True)
    assert res2.spec_drift and res2.models == res1.models


def test_file_universe(tmp_path):
    ids_file = tmp_path / "models.txt"
    ids_file.write_text("# my shortlist\nm/a\n\nm/b\nm/c\n")
    cfg = _cfg({"n": 2, "seed": 1, "universe": str(ids_file)})
    res = resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")
    assert res.source == "file"
    assert res.universe_size == 3  # blank + comment skipped
    assert set(res.models) <= {"m/a", "m/b", "m/c"}


def test_file_universe_missing_raises(tmp_path):
    cfg = _cfg({"n": 1, "seed": 1, "universe": str(tmp_path / "nope.txt")})
    with pytest.raises(ConfigError, match="not found"):
        resolve_model_sample(cfg, ROSTER, tmp_path / "model_locks.json")


def test_prepare_surfaces_sample_end_to_end(tmp_path, offline_adapter):
    """prepare -> mutated solvers.models -> pinned lock -> manifest + estimate provenance."""
    from conftest import TEST_CONFIG_YAML, write_study_files

    from itemeval._config import load_config
    from itemeval._manifest import build_manifest
    from itemeval._prepare import prepare_study
    from itemeval.budget._estimator import estimate_study

    sample_yaml = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]",
        "  sample:\n    n: 2\n    seed: 7\n"
        "    universe: [mockllm/solver-a, mockllm/solver-b, mockllm/solver-c]",
    )
    cfg = load_config(write_study_files(tmp_path, sample_yaml))
    prep = prepare_study(cfg)

    drawn = prep.config.solvers.models
    assert prep.model_sample is not None and prep.model_sample.source == "explicit"
    assert len(drawn) == 2 and set(drawn) <= {
        "mockllm/solver-a",
        "mockllm/solver-b",
        "mockllm/solver-c",
    }
    assert prep.paths.model_locks.is_file()  # pinned on first prepare
    assert {c.model for c in prep.grid.generate} == set(drawn)  # grid uses the draw

    manifest = build_manifest(prep, "generate", "r1", [], None)
    assert manifest.models == drawn
    assert manifest.model_sample["source"] == "explicit" and manifest.model_sample["n"] == 2
    assert "sample" in manifest.sampling_requested  # requested spec echoed

    est = estimate_study(prep)
    assert est.model_sample is not None and est.model_sample.n == 2

    # a fresh prepare reuses the frozen draw (no re-pin)
    prep2 = prepare_study(load_config(write_study_files(tmp_path, sample_yaml)))
    assert not prep2.model_sample.pinned_now
    assert prep2.config.solvers.models == drawn


def test_prepare_read_only_tolerates_spec_change_end_to_end(tmp_path, offline_adapter):
    """L2 through prepare_study: after a genuine sample-spec change, generate/grade
    (default) hard-fails while a read-only command (allow_spec_drift) inspects the
    pinned panel — config mutated to it and the grid built from it."""
    from conftest import TEST_CONFIG_YAML, write_study_files

    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    def _yaml(n: int) -> str:
        return TEST_CONFIG_YAML.replace(
            "  models: [mockllm/solver-a, mockllm/solver-b]",
            f"  sample:\n    n: {n}\n    seed: 7\n"
            "    universe: [mockllm/solver-a, mockllm/solver-b, mockllm/solver-c]",
        )

    prep = prepare_study(load_config(write_study_files(tmp_path, _yaml(2))))  # pins a 2-model panel
    pinned = list(prep.config.solvers.models)
    assert len(pinned) == 2

    with pytest.raises(ConfigError, match="spec changed"):  # write path: n 2->3 is fatal
        prepare_study(load_config(write_study_files(tmp_path, _yaml(3))))

    prep2 = prepare_study(load_config(write_study_files(tmp_path, _yaml(3))), allow_spec_drift=True)
    assert prep2.model_sample.spec_drift
    assert prep2.config.solvers.models == pinned  # the pinned 2-model panel, not a 3-model redraw
    assert {c.model for c in prep2.grid.generate} == set(pinned)  # grid built from the pinned panel

    # Python-surface parity: build_status(config) without a prep is read-only too —
    # it must inspect the pinned panel, not brick, after the spec change.
    from itemeval._status import build_status

    report = build_status(load_config(write_study_files(tmp_path, _yaml(3))))
    assert report.model_sample.spec_drift
    assert {c.detail["model"] for c in report.generate} == set(pinned)


def test_prepare_auto_refreshes_schema_stale_cache_for_pricing_table(
    tmp_path, monkeypatch, offline_adapter
):
    """A schema-stale cache (no text_model fields, but recent updated_at) reads as
    fresh by age, so a pricing-table universe used to dead-end in a ConfigError.
    prepare now detects the missing roster metadata and refreshes once."""
    import io
    import json
    import urllib.request

    from conftest import TEST_CONFIG_YAML, write_study_files

    from itemeval._config import load_config
    from itemeval._prepare import prepare_study
    from itemeval._util import utc_now_iso

    # A cache written before the roster-metadata fields existed: recent stamp, but
    # no entry carries text_model — exactly the case the age check cannot see.
    pricing_path = tmp_path / "user_pricing.json"
    pricing_path.write_text(
        json.dumps(
            {
                "updated_at": utc_now_iso(),
                "source": "merged",
                "models": {
                    "openrouter/old/model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}
                },
            }
        )
    )
    monkeypatch.setenv("ITEMEVAL_PRICING_PATH", str(pricing_path))
    payload = {
        "data": [
            {
                "id": "anthropic/claude-x",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "supported_parameters": ["temperature", "reasoning"],
                "context_length": 200_000,
            }
        ]
    }
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout: io.BytesIO(json.dumps(payload).encode())
    )
    sample_yaml = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]\n",
        "  sample:\n    n: 1\n    seed: 1\n    universe: pricing-table\n",
    )
    cfg = load_config(write_study_files(tmp_path, sample_yaml))
    prep = prepare_study(cfg)

    assert prep.pricing_refreshed  # the schema-stale recovery refresh announces itself
    assert prep.model_sample is not None and prep.model_sample.source == "pricing-table"
    assert prep.config.solvers.models == ["openrouter/anthropic/claude-x"]


def test_model_sample_provenance_line(capsys):
    from types import SimpleNamespace

    from itemeval._modelsample import ModelSampleResult
    from itemeval.cli import _print_model_sample

    ms = ModelSampleResult(
        source="pricing-table",
        universe_size=412,
        universe_hash="abc123abc123",
        n=20,
        seed=7,
        stratify_by="provider",
        models=[f"openrouter/o/m{i}" for i in range(20)],
        pinned_now=True,
    )
    _print_model_sample(SimpleNamespace(model_sample=ms))
    out = capsys.readouterr().out
    assert "sampled 20 of 412" in out and "seed 7" in out and "stratified by provider" in out
    assert "from the OpenRouter roster" in out and "pinned in model_locks.json" in out

    reused = ms.model_copy(update={"pinned_now": False, "universe_drift": True})
    _print_model_sample(SimpleNamespace(model_sample=reused))
    out = capsys.readouterr().out
    assert "reused from model_locks.json" in out and "universe changed" in out

    # spec_drift (read-only command on a study whose spec changed): pinned panel
    # shown + a warning pointing at the re-draw.
    drifted = ms.model_copy(update={"pinned_now": False, "spec_drift": True})
    _print_model_sample(SimpleNamespace(model_sample=drifted))
    out = capsys.readouterr().out
    assert "pinned panel" in out
    assert "warning: solvers.sample spec differs from model_locks.json" in out
    assert "clear model_locks.json to re-draw" in out

    _print_model_sample(SimpleNamespace(model_sample=None))  # no sample -> silent
    assert capsys.readouterr().out == ""


# --- richer stratify_by / where dimensions (reasoning, multimodal, tiers) ---


def _model(out, *, reasoning=False, multimodal=False, ctx=128_000) -> ModelPrice:
    return ModelPrice(
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=out,
        text_model=True,
        reasoning=reasoning,
        multimodal=multimodal,
        context_length=ctx,
    )


# 8 models: 4 price tiers x2, 4 context tiers x2, reasoning 4/4, multimodal 4/4.
META = PricingTable(
    updated_at="t",
    source="seed",
    models={
        "openrouter/p/free-1": _model(0.0, reasoning=True, multimodal=False, ctx=8_000),
        "openrouter/p/free-2": _model(0.0, reasoning=False, multimodal=True, ctx=200_000),
        "openrouter/p/low-1": _model(0.5, reasoning=True, multimodal=True, ctx=64_000),
        "openrouter/p/low-2": _model(1.0, reasoning=False, multimodal=False, ctx=128_000),
        "openrouter/p/mid-1": _model(5.0, reasoning=True, multimodal=False, ctx=300_000),
        "openrouter/p/mid-2": _model(8.0, reasoning=False, multimodal=True, ctx=500_000),
        "openrouter/p/high-1": _model(20.0, reasoning=True, multimodal=True, ctx=1_000_000),
        "openrouter/p/high-2": _model(50.0, reasoning=False, multimodal=False, ctx=16_000),
    },
)


def _draw_meta(stratify_by=None, where=None, n=4, tmp=None):
    spec = {"n": n, "seed": 1, "universe": "pricing-table"}
    if stratify_by:
        spec["stratify_by"] = stratify_by
    if where:
        spec["where"] = where
    _draw_meta.i = getattr(_draw_meta, "i", 0) + 1  # unique lock per call (no spec-change clash)
    return resolve_model_sample(_cfg(spec), META, tmp / f"lock{_draw_meta.i}.json")


def test_stratify_by_reasoning_and_multimodal(tmp_path):
    res = _draw_meta("reasoning", n=2, tmp=tmp_path)
    assert {(META.models[m].reasoning) for m in res.models} == {True, False}  # both strata
    res = _draw_meta("multimodal", n=2, tmp=tmp_path)
    assert {(META.models[m].multimodal) for m in res.models} == {True, False}


def test_stratify_by_price_and_context_tier(tmp_path):
    from itemeval._modelsample import _context_tier, _price_tier

    # free models are not drawable from a pricing-table universe (W2), so a
    # pricing-table draw never yields a "free" stratum — only low/mid/high.
    res = _draw_meta("price_tier", n=3, tmp=tmp_path)
    assert {_price_tier(META.models[m].output_usd_per_mtok) for m in res.models} == {
        "low",
        "mid",
        "high",
    }
    res = _draw_meta("context_tier", n=4, tmp=tmp_path)
    assert {_context_tier(META.models[m].context_length) for m in res.models} == {
        "short",
        "medium",
        "long",
        "xlong",
    }


def test_where_reasoning_multimodal_and_min_context(tmp_path):
    # 3 non-free reasoning models (low-1, mid-1, high-1); free-1 is excluded by W2.
    res = _draw_meta(where={"reasoning": True}, n=3, tmp=tmp_path)
    assert all(META.models[m].reasoning for m in res.models)  # only reasoning models
    res = _draw_meta(where={"multimodal": False}, n=2, tmp=tmp_path)
    assert all(not META.models[m].multimodal for m in res.models)
    res = _draw_meta(where={"min_context_length": 128_000}, n=1, tmp=tmp_path)
    assert all(META.models[m].context_length >= 128_000 for m in res.models)


def test_where_output_text_only(tmp_path):
    import json

    # A generator that also emits text passes the text_model gate ("text" is in
    # output_modalities), so output_text_only is what actually drops it.
    def _m(out_mods):
        return ModelPrice(
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            text_model=True,
            output_modalities=out_mods,
        )

    table = PricingTable(
        updated_at="t",
        source="seed",
        models={
            "openrouter/p/text-a": _m(["text"]),
            "openrouter/p/text-b": _m(["text"]),
            "openrouter/p/image-gen": _m(["image", "text"]),
            "openrouter/p/audio-gen": _m(["audio", "text"]),
        },
    )
    # Without the filter, the generators are drawable.
    res = resolve_model_sample(
        _cfg({"n": 4, "seed": 1, "universe": "pricing-table"}), table, tmp_path / "l1.json"
    )
    assert len(res.models) == 4
    # output_text_only: True keeps only the text-only-output models.
    res = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": "pricing-table", "where": {"output_text_only": True}}),
        table,
        tmp_path / "l2.json",
    )
    assert set(res.models) == {"openrouter/p/text-a", "openrouter/p/text-b"}
    # The filter value is pinned in the lock spec (flows via where.model_dump()).
    lock = json.loads((tmp_path / "l2.json").read_text())
    assert lock["sample"]["where"]["output_text_only"] is True
    # False is the symmetric inverse: keep only the non-text-output generators.
    res = resolve_model_sample(
        _cfg(
            {"n": 2, "seed": 1, "universe": "pricing-table", "where": {"output_text_only": False}}
        ),
        table,
        tmp_path / "l3.json",
    )
    assert set(res.models) == {"openrouter/p/image-gen", "openrouter/p/audio-gen"}


def test_metadata_stratify_requires_pricing_table_universe():
    # provider stratify is fine for an inline list; metadata strata are not.
    _cfg({"n": 1, "seed": 1, "universe": ["m/a", "m/b"], "stratify_by": "provider"})
    with pytest.raises(Exception, match="requires universe: pricing-table"):
        _cfg({"n": 1, "seed": 1, "universe": ["m/a", "m/b"], "stratify_by": "reasoning"})


# --- model-sample-composition: recency, equal allocation, pinned include ---

from collections import Counter  # noqa: E402

from itemeval._modelsample import _stratum_value  # noqa: E402

# Release timestamps (Unix seconds, mid-year so the UTC-year bucket is unambiguous).
Y2023, Y2024, Y2025 = 1685577600, 1717200000, 1748736000


def _dated(out: float, created: "int | None") -> ModelPrice:
    return ModelPrice(
        input_usd_per_mtok=1.0, output_usd_per_mtok=out, text_model=True, created=created
    )


DATED = PricingTable(
    updated_at="t",
    source="seed",
    models={
        "openrouter/a/old": _dated(5.0, Y2023),
        "openrouter/a/mid": _dated(5.0, Y2024),
        "openrouter/b/new": _dated(5.0, Y2025),
        "openrouter/c/new2": _dated(5.0, Y2025),
        "openrouter/d/undated": _dated(5.0, None),  # no release date
    },
)


def test_released_after_filters_and_drops_undated(tmp_path):
    cfg = _cfg(
        {"n": 3, "seed": 1, "universe": "pricing-table", "where": {"released_after": "2024-01-01"}}
    )
    res = resolve_model_sample(cfg, DATED, tmp_path / "r.json")
    assert res.universe_size == 3  # a/mid (2024), b/new, c/new2 — a/old and undated dropped
    assert set(res.models) <= {"openrouter/a/mid", "openrouter/b/new", "openrouter/c/new2"}
    assert "openrouter/a/old" not in res.models and "openrouter/d/undated" not in res.models


def test_stratify_by_recency_buckets_by_year(tmp_path):
    cfg = _cfg({"n": 4, "seed": 1, "stratify_by": "recency", "universe": "pricing-table"})
    res = resolve_model_sample(cfg, DATED, tmp_path / "r.json")
    # one per stratum: 2023, 2024, 2025, and the undated "unknown" bucket
    assert {_stratum_value(m, "recency", DATED) for m in res.models} == {
        "2023",
        "2024",
        "2025",
        "unknown",
    }


def test_stratify_by_recency_all_undated_raises(tmp_path):
    nodates = _pricing({"openrouter/x/a": 5.0, "openrouter/x/b": 5.0})  # text_model but no created
    cfg = _cfg({"n": 1, "seed": 1, "stratify_by": "recency", "universe": "pricing-table"})
    with pytest.raises(ConfigError, match="release dates.*refresh-pricing"):
        resolve_model_sample(cfg, nodates, tmp_path / "r.json")


# Allocation rosters: provider A has 4 models, B has 2 (unequal sizes).
ALLOC = _pricing(
    {f"openrouter/a/{i}": 5.0 for i in range(4)} | {f"openrouter/b/{i}": 5.0 for i in range(2)}
)
SMALL = _pricing({f"openrouter/a/{i}": 5.0 for i in range(4)} | {"openrouter/b/0": 5.0})


def test_allocation_equal_vs_proportional(tmp_path):
    base = {"n": 4, "seed": 1, "stratify_by": "provider", "universe": "pricing-table"}
    prop = resolve_model_sample(_cfg(base), ALLOC, tmp_path / "p.json")
    assert Counter(stratum(m) for m in prop.models) == {"a": 3, "b": 1}  # proportional to size
    eq = resolve_model_sample(_cfg({**base, "allocation": "equal"}), ALLOC, tmp_path / "e.json")
    assert Counter(stratum(m) for m in eq.models) == {"a": 2, "b": 2}  # balanced
    assert eq.allocation == "equal"


def test_equal_allocation_caps_small_stratum(tmp_path):
    eq = resolve_model_sample(
        _cfg(
            {
                "n": 4,
                "seed": 1,
                "stratify_by": "provider",
                "allocation": "equal",
                "universe": "pricing-table",
            }
        ),
        SMALL,
        tmp_path / "e.json",
    )
    # B has only 1 model -> capped at 1, overflow redistributed to A (sums to n)
    assert Counter(stratum(m) for m in eq.models) == {"a": 3, "b": 1}


def test_include_present_counted_and_filled_from_rest(tmp_path):
    inc = ["openrouter/anthropic/claude-a", "openrouter/openai/gpt-a"]
    res = resolve_model_sample(
        _cfg({"n": 4, "seed": 1, "universe": "pricing-table", "include": inc}),
        ROSTER,
        tmp_path / "i.json",
    )
    assert set(inc) <= set(res.models) and len(res.models) == 4
    assert res.include == sorted(inc)
    drawn = set(res.models) - set(inc)
    assert drawn <= set(ROSTER_IDS) - set(inc)  # fill from the non-included pool


def test_include_bypasses_membership_and_where(tmp_path):
    res = resolve_model_sample(
        _cfg(
            {
                "n": 2,
                "seed": 1,
                "universe": "pricing-table",
                "where": {"provider": ["google"]},  # roster narrowed to google
                "include": ["some/custom-model"],  # not in the roster at all
            }
        ),
        ROSTER,
        tmp_path / "i.json",
    )
    assert "some/custom-model" in res.models  # present despite not being in the universe
    assert "openrouter/google/gemini-a" in res.models  # fill respects where
    assert len(res.models) == 2 and res.universe_size == 1


def test_include_equals_n_no_fill(tmp_path):
    res = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": "pricing-table", "include": ["x/a", "x/b"]}),
        ROSTER,
        tmp_path / "i.json",
    )
    assert sorted(res.models) == ["x/a", "x/b"]


def test_n_exceeds_available_with_include_raises(tmp_path):
    cfg = _cfg(
        {
            "n": 3,
            "seed": 1,
            "universe": "pricing-table",
            "where": {"provider": ["google"]},
            "include": ["openrouter/google/gemini-a"],
        }
    )
    with pytest.raises(ConfigError, match="exceeds.*available to draw"):
        resolve_model_sample(cfg, ROSTER, tmp_path / "i.json")


def test_include_counts_toward_stratum_share(tmp_path):
    # 2 'a' pins + equal stratify over A/B with n=4 -> a stays at its share (2),
    # NOT 2 + the equal fill (which would over-represent A).
    res = resolve_model_sample(
        _cfg(
            {
                "n": 4,
                "seed": 1,
                "stratify_by": "provider",
                "allocation": "equal",
                "universe": "pricing-table",
                "include": ["openrouter/a/0", "openrouter/a/1"],
            }
        ),
        ALLOC,
        tmp_path / "i.json",
    )
    assert Counter(stratum(m) for m in res.models) == {"a": 2, "b": 2}
    assert {"openrouter/a/0", "openrouter/a/1"} <= set(res.models)


def test_include_over_pin_pins_win_rest_rebalances(tmp_path):
    # 3 'a' pins exceed a's equal share (2): all pins kept, remainder goes to B.
    res = resolve_model_sample(
        _cfg(
            {
                "n": 4,
                "seed": 1,
                "stratify_by": "provider",
                "allocation": "equal",
                "universe": "pricing-table",
                "include": ["openrouter/a/0", "openrouter/a/1", "openrouter/a/2"],
            }
        ),
        ALLOC,
        tmp_path / "o.json",
    )
    assert Counter(stratum(m) for m in res.models) == {"a": 3, "b": 1}


def test_provenance_line_equal_and_include(capsys):
    from types import SimpleNamespace

    from itemeval._modelsample import ModelSampleResult
    from itemeval.cli import _print_model_sample

    ms = ModelSampleResult(
        source="pricing-table",
        universe_size=400,
        universe_hash="h",
        n=5,
        seed=1,
        stratify_by="provider",
        allocation="equal",
        include=["x/a"],
        models=["x/a", *[f"o/m{i}" for i in range(4)]],
        pinned_now=True,
    )
    _print_model_sample(SimpleNamespace(model_sample=ms))
    out = capsys.readouterr().out
    assert "stratified by provider (equal)" in out and "1 via include" in out


# --- W2: non-free roster by default ---


def test_pricing_table_excludes_free_models(tmp_path):
    # META has 8 models, 2 of them free ($0 output) — the roster drops those.
    res = resolve_model_sample(
        _cfg({"n": 6, "seed": 1, "universe": "pricing-table"}), META, tmp_path / "f.json"
    )
    assert res.universe_size == 6  # 8 minus the 2 free models
    assert "openrouter/p/free-1" not in res.models
    assert "openrouter/p/free-2" not in res.models
    assert all(META.models[m].output_usd_per_mtok > 0 for m in res.models)


def test_free_models_stay_in_table_for_lookup():
    # W2 filters the drawable *roster*, not the pricing *data*: a free model
    # named directly in solvers.models still resolves a price (the escape hatch).
    from itemeval.budget._pricing import lookup_price

    assert "openrouter/p/free-1" in META.models
    assert lookup_price(META, "openrouter/p/free-1").output_usd_per_mtok == 0.0


def test_free_model_usable_in_inline_list(tmp_path):
    # W2 narrows only the pricing-table roster; an explicit list may carry a free id.
    res = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": ["openrouter/p/free-1", "openrouter/p/low-1"]}),
        META,
        tmp_path / "l.json",
    )
    assert "openrouter/p/free-1" in res.models  # not filtered (list is not roster-gated)


def test_no_runnable_nonfree_text_models_raises(tmp_path):
    only_free = _pricing({"openrouter/x/a": 0.0, "openrouter/x/b": 0.0})  # text_model but $0
    cfg = _cfg({"n": 1, "seed": 1, "universe": "pricing-table"})
    with pytest.raises(ConfigError, match="no runnable, non-free text models"):
        resolve_model_sample(cfg, only_free, tmp_path / "f.json")


# --- W1: top-level exclude (id blocklist) ---


def test_exclude_removes_ids_from_pricing_table(tmp_path):
    excluded = ["openrouter/anthropic/claude-a", "some/non-roster-id"]
    res = resolve_model_sample(
        _cfg({"n": 3, "seed": 1, "universe": "pricing-table", "exclude": excluded}),
        ROSTER,
        tmp_path / "e.json",
    )
    assert "openrouter/anthropic/claude-a" not in res.models
    assert res.universe_size == len(ROSTER_IDS) - 1  # one roster id removed; non-roster is a no-op
    assert res.exclude == sorted(excluded)


def test_exclude_works_on_inline_list_and_file(tmp_path):
    # inline list — where would be rejected here, but exclude is not roster-gated
    res = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": ["m/a", "m/b", "m/c"], "exclude": ["m/b"]}),
        ROSTER,
        tmp_path / "il.json",
    )
    assert res.universe_size == 2 and "m/b" not in res.models
    # file universe
    ids_file = tmp_path / "models.txt"
    ids_file.write_text("m/a\nm/b\nm/c\n")
    res = resolve_model_sample(
        _cfg({"n": 2, "seed": 1, "universe": str(ids_file), "exclude": ["m/c"]}),
        ROSTER,
        tmp_path / "fl.json",
    )
    assert res.universe_size == 2 and "m/c" not in res.models


def test_include_exclude_overlap_raises():
    with pytest.raises(Exception, match="both included and excluded"):
        _cfg(
            {
                "n": 2,
                "seed": 1,
                "universe": "pricing-table",
                "include": ["x/a"],
                "exclude": ["x/a"],
            }
        )


def test_exclude_duplicates_and_empty_raise():
    with pytest.raises(Exception, match="exclude entries must be unique"):
        _cfg({"n": 1, "seed": 1, "universe": "pricing-table", "exclude": ["a", "a"]})
    with pytest.raises(Exception, match="exclude entries must be non-empty"):
        _cfg({"n": 1, "seed": 1, "universe": "pricing-table", "exclude": [" "]})


def test_exclude_enters_lock_spec_and_triggers_redraw_guard(tmp_path):
    lock = tmp_path / "model_locks.json"
    base = {
        "n": 2,
        "seed": 1,
        "universe": "pricing-table",
        "exclude": ["openrouter/anthropic/claude-a"],
    }
    res = resolve_model_sample(_cfg(base), ROSTER, lock)
    assert res.exclude == ["openrouter/anthropic/claude-a"]
    assert "openrouter/anthropic/claude-a" not in res.models
    # editing exclude changes the pinned spec -> fails loud
    with pytest.raises(ConfigError, match="spec changed"):
        resolve_model_sample(_cfg({**base, "exclude": ["openrouter/openai/gpt-a"]}), ROSTER, lock)


def test_exclude_removing_everything_raises(tmp_path):
    with pytest.raises(ConfigError, match="exclude removed every model"):
        resolve_model_sample(
            _cfg({"n": 1, "seed": 1, "universe": ["m/a"], "exclude": ["m/a"]}),
            ROSTER,
            tmp_path / "z.json",
        )


def test_combined_frame_exclude_recency_nonfree(tmp_path):
    # ADR-0003-style frame: year-stratified, released_after, price ceiling, judge
    # id excluded, over a roster that also has a free and an over-ceiling model.
    pt = PricingTable(
        updated_at="t",
        source="seed",
        models={
            "openrouter/j/judge": _dated(10.0, Y2025),  # judge id -> exclude
            "openrouter/a/m1": _dated(5.0, Y2024),
            "openrouter/a/m2": _dated(5.0, Y2025),
            "openrouter/b/m3": _dated(5.0, Y2023),
            "openrouter/c/free": _dated(0.0, Y2025),  # free -> dropped by W2
            "openrouter/d/pricey": _dated(50.0, Y2025),  # over the ceiling
        },
    )
    res = resolve_model_sample(
        _cfg(
            {
                "n": 3,
                "seed": 1,
                "stratify_by": "recency",
                "universe": "pricing-table",
                "where": {"released_after": "2023-01-01", "max_output_usd_per_mtok": 15},
                "exclude": ["openrouter/j/judge"],
            }
        ),
        pt,
        tmp_path / "c.json",
    )
    assert res.universe_size == 3  # judge, free, pricey all gone
    assert set(res.models) == {"openrouter/a/m1", "openrouter/a/m2", "openrouter/b/m3"}
    assert {_stratum_value(m, "recency", pt) for m in res.models} == {"2023", "2024", "2025"}
