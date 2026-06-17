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


def test_pricing_table_without_text_metadata_raises(tmp_path):
    # openrouter/* keys present but none flagged text_model (e.g. a stale cache)
    stale = PricingTable(
        updated_at="t",
        source="seed",
        models={"openrouter/x/y": ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)},
    )
    cfg = _cfg({"n": 1, "seed": 1, "universe": "pricing-table"})
    with pytest.raises(ConfigError, match="runnable text models.*refresh-pricing"):
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
    with pytest.raises(ConfigError, match="no runnable text models"):
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

    _print_model_sample(SimpleNamespace(model_sample=None))  # no sample -> silent
    assert capsys.readouterr().out == ""
