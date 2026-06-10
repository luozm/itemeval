"""Canonical benchmark item model."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Item(BaseModel):
    """Canonical benchmark item: what adapters produce and both stages consume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    input: str
    target: str = ""
    grading_scheme: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return str(v)

    @field_validator("input")
    @classmethod
    def _non_empty_input(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Item.input must be non-empty")
        return v
