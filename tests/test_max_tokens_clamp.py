"""Auto-clamp a generate condition's max_tokens to the model's context window.

A global ``solvers.max_tokens`` larger than a small-context model's window makes
every call to that model a guaranteed HTTP 400 (input + max_tokens > context).
The clamp shrinks max_tokens to fit at runtime, so a heterogeneous roster runs
end to end; the condition id keeps the *requested* design value (no store-key
churn, no churn when the roster's context_length refreshes).
"""

from itemeval._config import ExperimentConfig
from itemeval._item import Item
from itemeval._templates import Template
from itemeval._util import sha256_hex
from itemeval.design._grid import expand_generate_grid
from itemeval.generate._params import effective_context, fit_max_tokens
from itemeval.generate._task import build_generate_task


def _template(text: str, name: str = "p") -> Template:
    return Template(
        name=name, source="local", path=f"/x/{name}.md", text=text, sha256=sha256_hex(text.encode())
    )


def test_fit_noop_when_context_unknown_or_no_request():
    # No roster context_length -> can't know -> leave untouched.
    assert fit_max_tokens(32768, None, 100) == (32768, False)
    # No requested cap -> nothing to clamp.
    assert fit_max_tokens(None, 16385, 100) == (None, False)


def test_fit_large_context_is_untouched():
    assert fit_max_tokens(32768, 400_000, 200) == (32768, False)


def test_fit_small_context_clamps_to_fit_input_and_window():
    eff, clamped = fit_max_tokens(32768, 16385, 143)
    assert clamped is True
    # Leaves room for the input plus a margin — the whole point is no 400.
    assert eff + 143 < 16385


def test_effective_context_takes_the_smaller_known_window():
    # Model-level max vs the smallest routed endpoint window: clamp to the floor.
    assert effective_context(131072, 32768) == 32768
    assert effective_context(32768, 131072) == 32768
    # Only one known -> that one (endpoint unknown falls back to model-level,
    # i.e. today's behavior; model-level unknown uses the endpoint window).
    assert effective_context(131072, None) == 131072
    assert effective_context(None, 32768) == 32768
    # Both unknown -> None, so fit_max_tokens leaves max_tokens untouched.
    assert effective_context(None, None) is None


def test_endpoint_window_clamps_optimistic_model_context():
    # Regression: openrouter/qwen/qwen-2.5-7b-instruct advertises context_length
    # 131072 in the pricing table, but the served endpoint capped at 32768, so
    # the model-level clamp let a guaranteed HTTP 400 through. Clamping against
    # the endpoint minimum makes the request fit.
    ctx = effective_context(131072, 32768)  # min endpoint window
    eff, clamped = fit_max_tokens(32768, ctx, 157)
    assert clamped is True
    assert eff + 157 < 32768  # the request that used to 400 now fits the window


def test_build_generate_task_honors_override_without_moving_the_id():
    tmpl = _template("Solve:\n{input}")
    item = Item(id="p1", input="2+2?", target="4", grading_scheme=None, metadata={})
    cfg = ExperimentConfig.model_validate(
        {
            "study": "s",
            "benchmark": {
                "adapter": "hf",
                "datasets": [{"id": "org/ds"}],
                "mapping": {"input": "q"},
            },
            "solvers": {"models": ["mockllm/m"], "max_tokens": 32768},
            "facets": {"scorer": "exact_match", "prompt": ["p"], "replications": 1},
        }
    )
    [cond] = expand_generate_grid(cfg, {"p": tmpl})

    class Origin:
        dataset_id = "org/ds"
        revision = "r"

    task = build_generate_task(
        [item], cond, tmpl, "s", 1, False, {"p1": Origin()}, max_tokens_override=8000
    )
    assert task.config.max_tokens == 8000
    # The clamp is runtime-only: the design value (and thus the condition id) is
    # still the requested 32768.
    assert cond.gen_params.max_tokens == 32768
