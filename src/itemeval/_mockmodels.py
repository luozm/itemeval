"""mockllm pass-through: any `mockllm/*` model id runs the pipeline free.

A documented dev affordance, not test-only code: every exit-criterion demo and
the M6 CLI-only pipeline run on these deterministic callables. Outputs and
fabricated token usage are pure functions of the prompt text.
"""

from typing import Any, Union

from inspect_ai.model import Model, ModelOutput, ModelUsage, get_model

from itemeval._util import canonical_json, estimate_tokens, sha256_hex


def is_mock_model(model: str) -> bool:
    return model.startswith("mockllm/")


def _last_user_text(input: "list[Any]") -> str:
    for message in reversed(input):
        if getattr(message, "role", None) == "user":
            return message.text or ""
    return ""


def _with_usage(out: ModelOutput, input: "list[Any]", content: str) -> ModelOutput:
    it = sum(estimate_tokens(m.text or "") for m in input)
    ot = estimate_tokens(content)
    out.usage = ModelUsage(input_tokens=it, output_tokens=ot, total_tokens=it + ot)
    return out


def mock_generate_callable(model: str):
    def fn(input, tools, tool_choice, config) -> ModelOutput:
        prompt = _last_user_text(input)
        h = sha256_hex(prompt.encode("utf-8"))
        content = (
            f"Mock solution from {model}.\n"
            f"Deterministic reasoning over input hash {h[:12]}.\n"
            f"ANSWER: {h[:6]}"
        )
        out = ModelOutput.from_content(model=model, content=content, stop_reason="stop")
        return _with_usage(out, input, content)

    return fn


def mock_judge_callable(model: str):
    def fn(input, tools, tool_choice, config) -> ModelOutput:
        prompt = _last_user_text(input)
        h = sha256_hex(prompt.encode("utf-8"))
        score = (int(h[:8], 16) % 101) / 10.0  # 0.0 .. 10.0, step 0.1
        body = canonical_json(
            {"score": score, "reasoning": f"Deterministic mock grade (h={h[:8]})."}
        )
        content = f"Mock evaluation.\n\n```json\n{body}\n```\n"
        out = ModelOutput.from_content(model=model, content=content, stop_reason="stop")
        return _with_usage(out, input, content)

    return fn


def resolve_model(
    model: str, stage: str, model_args: "dict[str, Any] | None" = None
) -> Union[str, Model]:
    """Non-mock ids pass through as strings (or as a Model carrying request
    extras when model_args is non-empty — see _endpoints.model_args_for);
    mock ids get a stage-suited callable."""
    if not is_mock_model(model):
        if model_args:
            return get_model(model, **model_args)
        return model
    factory = mock_judge_callable if stage == "grade" else mock_generate_callable
    return get_model(model, custom_outputs=factory(model))
