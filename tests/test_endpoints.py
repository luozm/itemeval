"""Endpoint request shaping (W1): model_args_for, routing warnings, runner wiring."""

from itemeval._config import ExperimentConfig
from itemeval._endpoints import cache_provider_of, model_args_for, routing_warnings

ROUTING = {"order": ["anthropic"], "allow_fallbacks": False}


def _cfg(
    models,
    routing=None,
    grader_model="mockllm/judge",
    grader_routing=None,
) -> ExperimentConfig:
    data = {
        "study": "s",
        "benchmark": {
            "adapter": "hf",
            "datasets": [{"id": "x/y"}],
            "mapping": {"input": "q"},
        },
        "solvers": {"models": models},
        "facets": {"grader": ["judge"]},
        "graders": {"judge": {"model": grader_model}},
    }
    if routing is not None:
        data["solvers"]["provider_routing"] = routing
    if grader_routing is not None:
        data["graders"]["judge"]["provider_routing"] = grader_routing
    return ExperimentConfig.model_validate(data)


# --- model_args_for ---


def test_openrouter_model_gets_routing_object_verbatim():
    args = model_args_for("openrouter/anthropic/claude-haiku-4.5", provider_routing=ROUTING)
    assert args == {"provider": ROUTING}
    assert args["provider"] is ROUTING  # pass through, don't rename or copy


def test_direct_model_ignores_routing():
    assert model_args_for("anthropic/claude-haiku-4-5", provider_routing=ROUTING) == {}


def test_no_routing_is_empty():
    assert model_args_for("openrouter/anthropic/claude-haiku-4.5") == {}


def test_cache_provider_of_maps_openrouter_to_upstream():
    assert cache_provider_of("openrouter/anthropic/claude-haiku-4.5") == "anthropic"
    assert cache_provider_of("openrouter/openai/gpt-5-mini") == "openai"
    assert cache_provider_of("anthropic/claude-haiku-4-5") == "anthropic"
    assert cache_provider_of("openai/gpt-5-mini") == "openai"


# --- min cacheable prefix (W4 table; numbers checked 2026-06-12) ---


def test_min_cacheable_prefix_per_provider():
    from itemeval._endpoints import min_cacheable_prefix

    assert min_cacheable_prefix("openai/gpt-5-mini") == 1024
    assert min_cacheable_prefix("deepseek/deepseek-chat") == 64
    # anthropic is model-aware
    assert min_cacheable_prefix("anthropic/claude-haiku-4-5") == 4096
    assert min_cacheable_prefix("anthropic/claude-opus-4-6") == 4096
    assert min_cacheable_prefix("anthropic/claude-opus-4-7") == 2048
    assert min_cacheable_prefix("anthropic/claude-opus-4-8") == 1024
    assert min_cacheable_prefix("anthropic/claude-sonnet-4-6") == 1024
    assert min_cacheable_prefix("anthropic/claude-fable-5") == 512
    assert min_cacheable_prefix("anthropic/claude-mythos-5-preview") == 2048
    # google is model-aware; pre-2.5 has no implicit caching
    assert min_cacheable_prefix("google/gemini-2.5-flash") == 2048
    assert min_cacheable_prefix("google/gemini-3.5-flash") == 4096
    assert min_cacheable_prefix("google/gemini-1.5-pro") is None
    # openrouter ids obey the upstream's minimums (dot spelling normalized)
    assert min_cacheable_prefix("openrouter/anthropic/claude-haiku-4.5") == 4096
    assert min_cacheable_prefix("openrouter/openai/gpt-5-mini") == 1024
    # no documented minimum / no caching through inspect: omitted, never guessed
    assert min_cacheable_prefix("grok/grok-4") is None
    assert min_cacheable_prefix("together/llama-4") is None
    assert min_cacheable_prefix("mistral/mistral-large") is None
    assert min_cacheable_prefix("bedrock/anthropic.claude") is None
    assert min_cacheable_prefix("mockllm/solver-a") is None


# --- inert-routing warnings ---


def test_inert_solvers_routing_warns():
    gen, grade = routing_warnings(_cfg(["openai/gpt-5-mini"], routing=ROUTING))
    assert len(gen) == 1 and "solvers.provider_routing" in gen[0] and "inert" in gen[0]
    assert grade == []


def test_active_solvers_routing_does_not_warn():
    gen, grade = routing_warnings(_cfg(["openrouter/anthropic/claude-haiku-4.5"], routing=ROUTING))
    assert gen == [] and grade == []


def test_inert_grader_routing_warns_on_grade_side():
    gen, grade = routing_warnings(
        _cfg(
            ["mockllm/solver-a"], grader_model="anthropic/claude-haiku-4-5", grader_routing=ROUTING
        )
    )
    assert gen == []
    assert len(grade) == 1 and "graders.judge.provider_routing" in grade[0]


def test_active_grader_routing_does_not_warn():
    gen, grade = routing_warnings(
        _cfg(
            ["mockllm/solver-a"],
            grader_model="openrouter/anthropic/claude-haiku-4.5",
            grader_routing=ROUTING,
        )
    )
    assert gen == [] and grade == []


def test_no_routing_no_warnings():
    gen, grade = routing_warnings(_cfg(["openai/gpt-5-mini"]))
    assert gen == [] and grade == []


# --- config round-trip ---


def test_provider_routing_round_trips_to_manifest_echo():
    from itemeval._config import config_to_jsonable

    cfg = _cfg(["openrouter/anthropic/claude-haiku-4.5"], routing=ROUTING)
    echo = config_to_jsonable(cfg)
    assert echo["solvers"]["provider_routing"] == {
        "order": ["anthropic"],
        "allow_fallbacks": False,
    }


# --- runner wiring: factory receives the args; the unpinned hint fires ---


def _mock_stand_in_factory(seen):
    from inspect_ai.model import get_model

    from itemeval._mockmodels import mock_generate_callable

    def factory(model, stage, model_args):
        seen.append((model, stage, model_args))
        return get_model("mockllm/stand-in", custom_outputs=mock_generate_callable(model))

    return factory


def _openrouter_study(tmp_path, routing_yaml=""):
    from conftest import TEST_CONFIG_YAML, write_study_files

    yaml_text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]",
        "  models: [openrouter/anthropic/claude-haiku-4.5]" + routing_yaml,
    )
    return write_study_files(tmp_path, yaml_text)


def test_generate_passes_routing_args_and_skips_hint_when_pinned(tmp_path, offline_adapter):
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study
    from itemeval.generate._run import run_generate

    config = _openrouter_study(
        tmp_path, "\n  provider_routing: {order: [anthropic], allow_fallbacks: false}"
    )
    prep = prepare_study(load_config(config))
    seen = []
    result = run_generate(prep, model_factory=_mock_stand_in_factory(seen), display="none")
    assert all(
        args == {"provider": {"order": ["anthropic"], "allow_fallbacks": False}}
        for _, _, args in seen
    )
    assert not any(h.code == "openrouter-unpinned-cache" for h in result.hints)


def test_generate_unpinned_cached_openrouter_anthropic_hints(tmp_path, offline_adapter):
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study
    from itemeval.generate._run import run_generate

    config = _openrouter_study(tmp_path)  # replications: 2 -> cache_prompt auto resolves on
    prep = prepare_study(load_config(config))
    seen = []
    result = run_generate(prep, model_factory=_mock_stand_in_factory(seen), display="none")
    assert all(args == {} for _, _, args in seen)
    hint = next(h for h in result.hints if h.code == "openrouter-unpinned-cache")
    assert "openrouter/anthropic/claude-haiku-4.5" in hint.message
