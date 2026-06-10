"""Experiment config schema and YAML loader.

YAML *shape* validation happens at load; *reference* resolution (template
files exist, grader names defined) is deferred to prepare/grid-expansion so
the README sketch validates as-is.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from itemeval._errors import ConfigError
from itemeval._util import sha256_hex

NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
STUDY_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    revision: str | None = None  # branch/tag/SHA; None -> lock file / resolve at first run
    split: str = "train"
    name: str | None = None  # HF config name
    limit: int | None = Field(default=None, ge=1)  # first N rows, no shuffle


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str
    target: str | None = None
    id: str | None = None  # record column -> Item.id (else row index)
    grading_scheme: str | None = None
    metadata: list[str] = Field(default_factory=list)


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal["hf"]
    datasets: list[DatasetSpec] = Field(min_length=1)
    mapping: MappingSpec


class SolversConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int | None = None  # recorded; only some providers honor it

    @field_validator("models")
    @classmethod
    def _unique_models(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("solvers.models must be unique")
        return v


class ModelConfigFacet(BaseModel):
    """One model-config grid cell (sampling overrides + thinking/reasoning toggle)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_RE)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    reasoning_effort: ReasoningEffort | None = None  # OpenAI-style
    reasoning_tokens: int | None = Field(default=None, ge=1)  # Anthropic extended thinking


class FacetsConfig(BaseModel):
    # `model_config` is reserved on pydantic models; the facet list is stored as
    # `model_config_facet` with alias "model_config" (the YAML key).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    prompt: list[str] = Field(default_factory=lambda: ["default"], min_length=1)
    grader: list[str] = Field(default_factory=list)
    rubric: list[str] = Field(default_factory=lambda: ["default"], min_length=1)
    scorer: Literal["exact_match", "multiple_choice", "numeric"] | None = None
    replications: int = Field(default=1, ge=1)
    model_config_facet: list[ModelConfigFacet] = Field(
        default_factory=lambda: [ModelConfigFacet(name="default")],
        alias="model_config",
        min_length=1,
    )

    @model_validator(mode="after")
    def _check(self) -> "FacetsConfig":
        if not self.grader and self.scorer is None:
            raise ValueError("facets must declare at least one of grader / scorer")
        names = [m.name for m in self.model_config_facet]
        if len(set(names)) != len(names):
            raise ValueError("facets.model_config names must be unique")
        for field in ("prompt", "grader", "rubric"):
            vals = getattr(self, field)
            if len(set(vals)) != len(vals):
                raise ValueError(f"facets.{field} entries must be unique")
        return self


class GraderSpec(BaseModel):
    """Judge model spec. Temperature is pinned to 0.0 in v0.1 (ROADMAP M3)."""

    model_config = ConfigDict(extra="forbid")

    model: str
    max_tokens: int | None = Field(default=2048, ge=1)
    reasoning_effort: ReasoningEffort | None = None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["dev", "full-interactive", "full-batch"] = "dev"
    confirm_above_usd: float = Field(default=5.0, ge=0.0)
    batch: bool | int | Literal["auto"] = "auto"
    max_usd: float | None = Field(default=None, gt=0.0)  # hard cap, never overridable
    dev_items: int = Field(default=2, ge=1)  # dev preset: first N items
    dev_replications: int | None = Field(default=None, ge=1)  # None = keep config reps
    pricing_path: str | None = None


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str = Field(pattern=STUDY_RE)
    output_dir: str = "studies"  # resolved relative to the config file's dir
    prompts_dir: str = "prompts"  # solver templates: <prompts_dir>/solver/<name>.md
    rubrics_dir: str = "rubrics"  # rubric templates: <rubrics_dir>/<name>.md
    cache: bool = True  # inspect local response cache, both stages
    benchmark: BenchmarkConfig
    solvers: SolversConfig
    facets: FacetsConfig
    graders: dict[str, GraderSpec] = Field(default_factory=dict)
    crossing: Literal["full"] = "full"
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _config_path: Path | None = PrivateAttr(default=None)
    _config_sha256: str | None = PrivateAttr(default=None)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def config_sha256(self) -> str | None:
        return self._config_sha256

    @property
    def study_dir(self) -> Path:
        return (self._base_dir / self.output_dir / self.study).resolve()

    def grader_spec(self, name: str) -> GraderSpec:
        """Resolve a facets.grader entry. Raises ConfigError if unresolvable."""
        if name in self.graders:
            return self.graders[name]
        if "/" in name:  # bare model id used directly as a grader
            return GraderSpec(model=name)
        raise ConfigError(f"grader '{name}' is not defined under graders: and is not a model id")


def load_config(path: "str | Path") -> ExperimentConfig:
    """Load and validate an experiment config YAML file."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    raw = p.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a YAML mapping: {p}")
    try:
        cfg = ExperimentConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid config {p}:\n{e}") from e
    cfg._base_dir = p.parent
    cfg._config_path = p
    cfg._config_sha256 = sha256_hex(raw)
    return cfg


def config_to_jsonable(cfg: ExperimentConfig) -> dict[str, Any]:
    """Config as a JSON-ready dict using YAML key names (for manifests)."""
    return cfg.model_dump(mode="json", by_alias=True)
