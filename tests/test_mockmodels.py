from inspect_ai.model import ChatMessageUser, GenerateConfig, Model

from itemeval._mockmodels import (
    is_mock_model,
    mock_generate_callable,
    mock_judge_callable,
    resolve_model,
)
from itemeval.grade._parse import parse_judge_output


def _messages(text: str):
    return [ChatMessageUser(content=text)]


def test_generate_callable_deterministic():
    fn = mock_generate_callable("mockllm/solver-a")
    out1 = fn(_messages("prompt one"), [], "none", GenerateConfig())
    out2 = fn(_messages("prompt one"), [], "none", GenerateConfig())
    out3 = fn(_messages("prompt two"), [], "none", GenerateConfig())
    assert out1.completion == out2.completion
    assert out1.completion != out3.completion
    assert "ANSWER:" in out1.completion
    assert out1.usage.input_tokens > 0
    assert out1.usage.total_tokens == out1.usage.input_tokens + out1.usage.output_tokens


def test_judge_callable_emits_parseable_score():
    fn = mock_judge_callable("mockllm/judge")
    out = fn(_messages("grade this"), [], "none", GenerateConfig())
    parsed = parse_judge_output(out.completion)
    assert parsed.parse_ok
    assert parsed.score is not None and 0.0 <= parsed.score <= 10.0
    assert parsed.reasoning


def test_resolve_model_passthrough_for_real_ids():
    assert resolve_model("openai/gpt-5-mini", "generate") == "openai/gpt-5-mini"
    assert is_mock_model("mockllm/x")
    assert not is_mock_model("openai/x")


def test_resolve_model_returns_model_for_mock_ids():
    model = resolve_model("mockllm/solver-a", "generate")
    assert isinstance(model, Model)
