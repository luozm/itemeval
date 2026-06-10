"""Effective sampling-param extraction from eval logs (requested vs effective)."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from itemeval.design._grid import GenParams

if TYPE_CHECKING:
    from inspect_ai.log import EvalSample


class EffectiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_tokens: int | None = None


def extract_effective_params(sample: "EvalSample", requested: GenParams) -> EffectiveParams:
    """Effective values from the sample's last model event; requested as fallback.

    Provider-forced values surface as a requested/effective mismatch. Never raises.
    """
    event_config = None
    try:
        for event in reversed(sample.events or []):
            if getattr(event, "event", None) == "model":
                event_config = event.config
                break
    except Exception:
        event_config = None

    def pick(field: str):
        if event_config is not None:
            value = getattr(event_config, field, None)
            if value is not None:
                return value
        return getattr(requested, field)

    return EffectiveParams(
        temperature=pick("temperature"),
        top_p=pick("top_p"),
        max_tokens=pick("max_tokens"),
        reasoning_effort=pick("reasoning_effort"),
        reasoning_tokens=pick("reasoning_tokens"),
    )
