"""Native batch routing decision layer (budget/_routing.py) — pure, hermetic."""

import pytest

from itemeval._config import ExperimentConfig
from itemeval.budget._policies import EffectivePlan
from itemeval.budget._routing import (
    NATIVE_API_KEY_ENV,
    active_native_routes,
    eligible_native_routes,
    native_id,
    native_key_present,
)

ALL_KEY_ENVS = sorted({v for vs in NATIVE_API_KEY_ENV.values() for v in vs})


@pytest.fixture(autouse=True)
def _no_native_keys(monkeypatch):
    """Clear every native API-key env so routing decisions are deterministic
    regardless of the developer's shell."""
    for var in ALL_KEY_ENVS:
        monkeypatch.delenv(var, raising=False)


def _config(models, grader_model="mockllm/judge", prefer_native_batch=False) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "study": "tstudy",
            "benchmark": {
                "adapter": "hf",
                "datasets": [{"id": "fake/ds"}],
                "mapping": {"input": "problem"},
            },
            "solvers": {"models": models},
            "facets": {"grader": ["judge"]},
            "graders": {"judge": {"model": grader_model}},
            "budget": {"prefer_native_batch": prefer_native_batch},
        }
    )


def _plan(batch) -> EffectivePlan:
    return EffectivePlan(policy="full-batch", items_limit=None, replications=1, batch=batch)


# --- native_id: per-provider spelling map ------------------------------------


@pytest.mark.parametrize(
    "sampled,expected",
    [
        # Anthropic: dots -> dashes (the only provider that needs it).
        ("openrouter/anthropic/claude-haiku-4.5", "anthropic/claude-haiku-4-5"),
        ("openrouter/anthropic/claude-opus-4.8", "anthropic/claude-opus-4-8"),
        # OpenAI / Google keep dots; only the provider segment is validated.
        ("openrouter/openai/gpt-5.1", "openai/gpt-5.1"),
        ("openrouter/google/gemini-3.1-pro-preview", "google/gemini-3.1-pro-preview"),
        # x-ai -> grok (the one provider-segment rename).
        ("openrouter/x-ai/grok-4", "grok/grok-4"),
    ],
)
def test_native_id_maps(sampled, expected):
    assert native_id(sampled) == expected


@pytest.mark.parametrize(
    "sampled",
    [
        "openrouter/deepseek/deepseek-v3.2",  # native deepseek has no batch API
        "openrouter/meta-llama/llama-4-scout",  # not a batch provider
        "openrouter/mistralai/mistral-large-2512",
        "anthropic/claude-haiku-4-5",  # already native — nothing to route
        "openai/gpt-5",
        "mockllm/judge",
        "openrouter/anthropic",  # not a triple
        "openrouter/anthropic/",  # empty name
    ],
)
def test_native_id_none(sampled):
    assert native_id(sampled) is None


# --- native_key_present ------------------------------------------------------


def test_native_key_present(monkeypatch):
    assert not native_key_present("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert native_key_present("anthropic")


def test_grok_accepts_either_key(monkeypatch):
    assert not native_key_present("grok")
    monkeypatch.setenv("GROK_API_KEY", "x")  # the fallback name
    assert native_key_present("grok")
    monkeypatch.delenv("GROK_API_KEY")
    monkeypatch.setenv("XAI_API_KEY", "x")  # the primary name
    assert native_key_present("grok")


# --- eligible_native_routes: key gate + unavailable list ---------------------


def test_eligible_routes_key_gate(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    # GOOGLE_API_KEY deliberately unset.
    cfg = _config(
        models=[
            "openrouter/anthropic/claude-opus-4.8",
            "openrouter/openai/gpt-5.1",
            "openrouter/deepseek/deepseek-v3.2",  # not eligible
            "mockllm/x",  # not eligible
        ],
        grader_model="openrouter/google/gemini-3.1-pro-preview",  # eligible but keyless
    )
    routes, unavailable = eligible_native_routes(cfg)
    assert routes == {
        "openrouter/anthropic/claude-opus-4.8": "anthropic/claude-opus-4-8",
        "openrouter/openai/gpt-5.1": "openai/gpt-5.1",
    }
    assert unavailable == ["openrouter/google/gemini-3.1-pro-preview"]


# --- active_native_routes: the batch + knob gate -----------------------------


def test_active_requires_batch_and_knob(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    models = ["openrouter/anthropic/claude-opus-4.8"]
    want = {"openrouter/anthropic/claude-opus-4.8": "anthropic/claude-opus-4-8"}

    # batch on + knob on -> routes
    cfg_on = _config(models, prefer_native_batch=True)
    assert active_native_routes(cfg_on, _plan(batch=True)) == want
    # knob off -> no routes (even under batch)
    cfg_off = _config(models, prefer_native_batch=False)
    assert active_native_routes(cfg_off, _plan(batch=True)) == {}
    # knob on but not a batch plan -> no routes (routing only buys batch)
    assert active_native_routes(cfg_on, _plan(batch=None)) == {}


# --- estimator integration: the routed batch discount + savings + hint --------

PRICING_JSON = """\
{
  "updated_at": "2026-06-17T00:00:00Z",
  "source": "file",
  "models": {
    "openrouter/anthropic/claude-opus-4.8": {
      "input_usd_per_mtok": 5.0, "output_usd_per_mtok": 15.0, "reasoning": false
    },
    "mockllm/*": {"input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0}
  }
}
"""

ROUTED_YAML = """\
study: tstudy
output_dir: studies
prompts_dir: prompts
rubrics_dir: rubrics
benchmark:
  adapter: hf
  datasets: [{id: fake/ds}]
  mapping: {id: problem_idx, input: problem, target: sample_solution}
solvers:
  models: [openrouter/anthropic/claude-opus-4.8]
  max_tokens: 256
facets:
  prompt: [minimal]
  grader: [judge]
  rubric: [standard]
  replications: 1
graders:
  judge: {model: mockllm/judge, max_tokens: 256}
budget:
  policy: full-batch
  confirm_above_usd: 100000
  pricing_path: pricing.json
  prefer_native_batch: PREFER
"""


def _routed_estimate(tmp_path, offline_adapter, prefer):
    from conftest import write_study_files
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study
    from itemeval.budget._estimator import estimate_study

    (tmp_path / "pricing.json").write_text(PRICING_JSON)
    yaml_text = ROUTED_YAML.replace("PREFER", "true" if prefer else "false")
    cfg = load_config(write_study_files(tmp_path, yaml_text))
    return estimate_study(prepare_study(cfg))


def _hint(est, code):
    return next((h for h in est.hints if h.code == code), None)


def test_estimator_routes_and_discounts(tmp_path, offline_adapter, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    on = _routed_estimate(tmp_path, offline_adapter, prefer=True)
    off = _routed_estimate(tmp_path, offline_adapter, prefer=False)

    # Both list the eligible route; the sampled id stays the scientific identity.
    for est in (on, off):
        assert [(r.sampled, r.execution, r.provider) for r in est.routes] == [
            ("openrouter/anthropic/claude-opus-4.8", "anthropic/claude-opus-4-8", "anthropic")
        ]
        assert est.generate.conditions[0].model == "openrouter/anthropic/claude-opus-4.8"

    # Knob on halves the routed (generate) cost; knob off pays full price.
    assert on.generate.usd == pytest.approx(0.5 * off.generate.usd, rel=1e-6)
    assert on.generate.usd > 0
    # Savings surface either way (available lever / realized discount).
    assert on.generate.native_route_savings_usd == pytest.approx(off.generate.usd * 0.5, rel=1e-6)

    # The hint fires only when the lever is unused (knob off), never when on.
    assert _hint(on, "native-batch-available") is None
    h = _hint(off, "native-batch-available")
    assert h is not None and "prefer_native_batch" in h.message

    # W2 dual projection: per route, native-batch vs OpenRouter-cache (expected).
    # This solver runs monolithic (no split_prompt) anthropic via OpenRouter, so
    # caching can't engage -> cache_usd is the full price, batch is cheaper.
    (route,) = on.routes
    assert route.batch_usd > 0 and route.cache_usd is not None
    assert route.batch_usd < route.cache_usd  # batch halves output too
    assert route.cheaper == "batch"
    # The comparison is plan-independent: knob off shows the same numbers.
    assert off.routes[0].batch_usd == pytest.approx(route.batch_usd, rel=1e-9)
    assert off.routes[0].cache_usd == pytest.approx(route.cache_usd, rel=1e-9)


def test_estimator_no_routes_without_key(tmp_path, offline_adapter):
    # No ANTHROPIC_API_KEY in env (cleared by the autouse fixture) -> not routable.
    est = _routed_estimate(tmp_path, offline_adapter, prefer=True)
    assert est.routes == []
    assert est.generate.native_route_savings_usd == 0.0
    assert _hint(est, "native-batch-available") is None


def test_run_generate_executes_on_native_id(tmp_path, offline_adapter, monkeypatch):
    """The generate run calls the model factory with the native id, but records
    the sampled id everywhere it is the scientific identity."""
    import json

    from inspect_ai.model import get_model

    from conftest import write_study_files
    from itemeval._config import load_config
    from itemeval._mockmodels import mock_generate_callable
    from itemeval._prepare import prepare_study
    from itemeval.generate._run import run_generate
    from itemeval.store._ledger import read_ledger
    from itemeval.store._solutions import read_solutions

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    (tmp_path / "pricing.json").write_text(PRICING_JSON)
    cfg = load_config(write_study_files(tmp_path, ROUTED_YAML.replace("PREFER", "true")))
    prep = prepare_study(cfg)

    seen: list[str] = []

    def factory(model_id, stage, model_args):
        seen.append(model_id)  # the id the eval actually runs on
        return get_model("mockllm/exec", custom_outputs=mock_generate_callable(model_id))

    result = run_generate(prep, model_factory=factory)

    # Executed on the native id; sampled id stays the recorded scientific identity.
    assert seen == ["anthropic/claude-opus-4-8"]
    df = read_solutions(prep.paths)
    assert (df["model"] == "openrouter/anthropic/claude-opus-4.8").all()
    assert [(r.sampled, r.execution, r.provider) for r in result.routed_models] == [
        ("openrouter/anthropic/claude-opus-4.8", "anthropic/claude-opus-4-8", "anthropic")
    ]
    assert result.batch_providers == ["anthropic"]
    # Ledger provider is the billing (native) provider; manifest records the switch.
    assert (read_ledger(prep.paths)["provider"] == "anthropic").all()
    manifest = json.loads((prep.paths.study_dir / result.manifest_path).read_text())
    ep = manifest["endpoints_effective"][prep.grid.generate[0].id]
    assert ep["routed"] is True and ep["execution_model"] == "anthropic/claude-opus-4-8"


# --- the openrouter-unpinned-cache hint is route-aware ------------------------

GRADE_ROUTED_YAML = """\
study: tstudy
output_dir: studies
prompts_dir: prompts
rubrics_dir: rubrics
benchmark:
  adapter: hf
  datasets: [{id: fake/ds}]
  mapping: {id: problem_idx, input: problem, target: sample_solution}
solvers:
  models: [mockllm/solver]
  max_tokens: 256
facets:
  prompt: [minimal]
  grader: [judge]
  rubric: [standard]
  replications: 1
graders:
  judge: {model: openrouter/anthropic/claude-opus-4.8, max_tokens: 256}
budget:
  policy: full-batch
  confirm_above_usd: 100000
  pricing_path: pricing.json
  prefer_native_batch: PREFER
"""


def _routed_grade(tmp_path, offline_adapter, prefer):
    """generate (mock solver) then grade an openrouter/anthropic judge; returns
    the GradeResult. The judge routes to native iff prefer + a key are present."""
    from inspect_ai.model import get_model

    from conftest import write_study_files
    from itemeval._config import load_config
    from itemeval._mockmodels import mock_generate_callable, mock_judge_callable
    from itemeval._prepare import prepare_study
    from itemeval.generate._run import run_generate
    from itemeval.grade._run import run_grade

    (tmp_path / "pricing.json").write_text(PRICING_JSON)
    yaml_text = GRADE_ROUTED_YAML.replace("PREFER", "true" if prefer else "false")
    prep = prepare_study(load_config(write_study_files(tmp_path, yaml_text)))

    def factory(model_id, stage, model_args):
        cb = mock_judge_callable(model_id) if stage == "grade" else mock_generate_callable(model_id)
        return get_model("mockllm/exec", custom_outputs=cb)

    run_generate(prep, model_factory=factory)
    return run_grade(prep, model_factory=factory)


def test_grade_routed_judge_skips_openrouter_cache_hint(tmp_path, offline_adapter, monkeypatch):
    """A judge routed to its native batch API never touches OpenRouter, so the
    openrouter-unpinned-cache caveat must not fire (regression: it fired on the
    sampled openrouter/anthropic id regardless of the active native route)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    routed = _routed_grade(tmp_path, offline_adapter, prefer=True)
    # Sanity: the judge really did route (else the assertion below proves nothing).
    assert [(r.sampled, r.execution) for r in routed.routed_models] == [
        ("openrouter/anthropic/claude-opus-4.8", "anthropic/claude-opus-4-8")
    ]
    assert "openrouter-unpinned-cache" not in [h.code for h in routed.hints]


def test_grade_unrouted_judge_keeps_openrouter_cache_hint(tmp_path, offline_adapter):
    """Contrast: with no native key (routing off), the same openrouter/anthropic
    judge runs via OpenRouter, so the cache caveat still fires — the fix is
    route-specific, not a blanket suppression."""
    unrouted = _routed_grade(tmp_path, offline_adapter, prefer=False)
    assert unrouted.routed_models == []
    assert "openrouter-unpinned-cache" in [h.code for h in unrouted.hints]
