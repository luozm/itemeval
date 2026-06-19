from inspect_ai.model import ChatMessageUser, GenerateConfig, Model, get_model

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


def test_is_mock_model():
    assert is_mock_model("mockllm/x")
    assert not is_mock_model("openai/x")


def test_resolve_model_returns_model_for_real_ids(monkeypatch):
    # Regression (parallel-conditions): a non-mock id must resolve to a Model,
    # never a bare str. The concurrent eval sets task.model per condition and
    # inspect reads task.model.model_args — a str raises AttributeError there.
    # The common case is empty model_args, which previously returned the raw id
    # string. get_model is stubbed so the contract is checked without a key.
    import itemeval._mockmodels as mm

    probe = get_model("mockllm/probe")  # a real Model, constructs offline
    seen: dict = {}

    def fake_get_model(model, **kwargs):
        seen["model"], seen["kwargs"] = model, kwargs
        return probe

    monkeypatch.setattr(mm, "get_model", fake_get_model)

    out = mm.resolve_model("openai/gpt-5-mini", "generate")  # empty model_args
    assert isinstance(out, Model)
    assert seen == {"model": "openai/gpt-5-mini", "kwargs": {}}

    mm.resolve_model("openai/gpt-5-mini", "grade", {"base_url": "https://x"})
    assert seen["kwargs"] == {"base_url": "https://x"}  # extras forwarded


def test_resolve_model_returns_model_for_mock_ids():
    model = resolve_model("mockllm/solver-a", "generate")
    assert isinstance(model, Model)
