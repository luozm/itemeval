"""Experiment config schema and YAML loader.

YAML *shape* validation happens at load; *reference* resolution (template
files exist, grader names defined) is deferred to prepare/grid-expansion so
the README sketch validates as-is.
"""

from datetime import datetime
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

# What to do with a *completed* generation that produced no gradable text
# (empty/blank `solution`, no API error — e.g. a reasoning model whose token
# budget was spent entirely on hidden reasoning). Distinct from API errors
# (always re-attempted) and parse failures (always final).
#   skip  — exclude from grading, but report the count + stop reasons (default)
#   rerun — also treat as not-done in generate, so a subsequent `generate`
#           re-attempts them (raise max_tokens / lower reasoning effort first;
#           an identical request will hit the response cache and stay empty)
#   grade — send to the judge as-is (an empty answer, typically scored low)
EmptySolutionPolicy = Literal["skip", "rerun", "grade"]


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
    # record column -> Item.id (else row index). A list of segments joined with
    # ":" (multi-column natural keys); a segment containing "{" is a template
    # over record columns + a synthetic "{dataset}" token (the dataset basename)
    # — e.g. ["{dataset}", problem_idx] -> "set_2026:6". A single column name is
    # unchanged. See adapters/_hf.py:_synthesize_id and Configuration#composite-item-ids.
    id: str | list[str] | None = None
    grading_scheme: str | None = None
    metadata: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: "str | list[str] | None") -> "str | list[str] | None":
        if isinstance(v, list) and not v:
            raise ValueError("mapping.id list must be non-empty")
        for seg in [v] if isinstance(v, str) else (v or []):
            if not seg.strip():
                raise ValueError("mapping.id entries must be non-empty")
        return v


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal["hf"]
    datasets: list[DatasetSpec] = Field(min_length=1)
    mapping: MappingSpec


PRICING_TABLE_UNIVERSE = "pricing-table"  # reserved `universe` keyword (the roster)
# stratify_by dimensions that read per-model roster metadata (vs `provider`,
# which is derivable from any model id) — valid only for a pricing-table universe.
METADATA_STRATA = ("reasoning", "multimodal", "price_tier", "context_tier", "recency")
StratifyBy = Literal["provider", "reasoning", "multimodal", "price_tier", "context_tier", "recency"]


class ModelUniverseFilter(BaseModel):
    """`solvers.sample.where`: narrow the pricing-table roster before drawing.

    Roster-only (rejected for inline-list / file universes, which are already
    curated). All fields optional; an empty filter is inert. Filters are
    continuous/boolean — tiers are a stratify concept, not a filter.
    """

    model_config = ConfigDict(extra="forbid")

    provider: list[str] | None = None  # org allowlist (the model-id org segment)
    max_output_usd_per_mtok: float | None = Field(default=None, gt=0.0)
    reasoning: bool | None = None  # keep only reasoning (True) / non-reasoning (False) models
    multimodal: bool | None = None  # keep only multimodal (True) / text-only (False) models
    # Keep only text-only-output models (True) / only non-text-output models
    # (False). True drops image/audio/video generators that still emit text and
    # so pass the text_model gate; reads ModelPrice.output_modalities.
    output_text_only: bool | None = None
    min_context_length: int | None = Field(default=None, ge=1)  # keep models with >= this context
    # Absolute YYYY-MM-DD release cutoff (uses the roster's `created` timestamp);
    # keep only models released on/after it. Absolute, never wall-clock age, so a
    # pinned table draws identically. Models without a `created` date are dropped.
    released_after: str | None = None

    @field_validator("released_after")
    @classmethod
    def _check_released_after(cls, v: "str | None") -> "str | None":
        if v is not None:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError as e:
                raise ValueError(
                    f"released_after must be an absolute date YYYY-MM-DD, got {v!r}"
                ) from e
        return v


class ModelSample(BaseModel):
    """A seeded, optionally provider-stratified draw of models from a universe.

    `universe` is one of: the reserved string ``"pricing-table"`` (the
    ``openrouter/*`` roster from the pricing table), any other string (a file
    path of model ids, one per line), or an inline list of model ids. The draw
    populates ``solvers.models`` and is pinned in ``model_locks.json``.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    seed: int
    stratify_by: StratifyBy | None = None
    # Per-stratum apportionment. "proportional" (default) allocates n by stratum
    # size (large vendors dominate); "equal" balances n across strata (requires
    # stratify_by). Changes the drawn set, hence the grid — a design declaration.
    allocation: Literal["proportional", "equal"] = "proportional"
    # Pinned model ids always present, counted against n; the seeded draw fills
    # the rest. Bypasses `where` and universe membership (purposive). When also
    # stratified, pins count toward their stratum's balanced share (not on top).
    include: list[str] = Field(default_factory=list)
    # Remove these exact model-ids from the universe before drawing (e.g. judge
    # ids, for rater-object independence). Exact match; ids absent from the
    # universe are a no-op. The inverse of `include`, which *adds* purposive
    # pins; unlike `where`, it is not roster-only (works for any universe).
    exclude: list[str] = Field(default_factory=list)
    universe: str | list[str]  # "pricing-table" | a file path | an inline list
    where: ModelUniverseFilter | None = None

    @model_validator(mode="after")
    def _check(self) -> "ModelSample":
        if isinstance(self.universe, list):
            if not self.universe:
                raise ValueError("solvers.sample.universe list must be non-empty")
            if len(set(self.universe)) != len(self.universe):
                raise ValueError("solvers.sample.universe entries must be unique")
            if len(self.universe) < self.n:
                raise ValueError(
                    f"solvers.sample.n ({self.n}) exceeds the {len(self.universe)}-id universe list"
                )
        roster = self.universe == PRICING_TABLE_UNIVERSE
        if self.where is not None and not roster:
            raise ValueError(
                "solvers.sample.where applies only to universe: pricing-table "
                "(inline lists and files are already curated)"
            )
        if self.stratify_by in METADATA_STRATA and not roster:
            raise ValueError(
                f"solvers.sample.stratify_by: {self.stratify_by} reads roster metadata, so it "
                "requires universe: pricing-table; use stratify_by: provider for list/file universes"
            )
        if self.allocation == "equal" and self.stratify_by is None:
            raise ValueError(
                "solvers.sample.allocation: equal requires stratify_by (it balances n "
                "across strata); drop allocation or set stratify_by"
            )
        if len(set(self.include)) != len(self.include):
            raise ValueError("solvers.sample.include entries must be unique")
        if len(self.include) > self.n:
            raise ValueError(
                f"solvers.sample.include ({len(self.include)} ids) exceeds n ({self.n}) — "
                "pinned models are counted against n"
            )
        if any(not e.strip() for e in self.exclude):
            raise ValueError("solvers.sample.exclude entries must be non-empty")
        if len(set(self.exclude)) != len(self.exclude):
            raise ValueError("solvers.sample.exclude entries must be unique")
        overlap = set(self.include) & set(self.exclude)
        if overlap:
            raise ValueError(
                "solvers.sample: ids cannot be both included and excluded "
                f"(include ∩ exclude = {sorted(overlap)})"
            )
        return self


class SolversConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int | None = None  # recorded; only some providers honor it
    on_empty: EmptySolutionPolicy = "skip"  # handling of empty (no-error) solutions
    # Provider prompt caching for the generate stage (Anthropic-style explicit
    # cache_control markers; token-prefix providers like OpenAI cache
    # automatically regardless). "auto" enables it when replications > 1 —
    # epochs send byte-identical prompts, the textbook cacheable workload.
    cache_prompt: Literal["auto", "on", "off"] = "auto"
    # Render the solver prompt as two messages split at {input}: the static
    # template head becomes a system message — where providers with
    # block-granular prompt caching (Anthropic) get an explicit cache
    # breakpoint — and the item the user message. The concatenated text is
    # unchanged. Changes generate condition ids when enabled.
    split_prompt: bool = False
    # Verbatim OpenRouter provider-routing object (e.g. {order: [anthropic],
    # allow_fallbacks: false}) sent with every openrouter/* request — pins the
    # upstream so cached runs don't land on a marker-ignoring host (Bedrock).
    # Pass-through, never renamed/validated beyond "a dict": OpenRouter owns
    # the schema. Optimization knob; never enters condition ids (endpoint
    # identity never has — endpoint drift warnings cover served-model drift).
    provider_routing: "dict[str, Any] | None" = None
    # Draw `models` from a universe instead of listing them. XOR with `models`:
    # exactly one of the two must be set. Resolved + pinned at prepare time.
    sample: "ModelSample | None" = None

    @field_validator("cache_prompt", mode="before")
    @classmethod
    def _yaml_bool_cache_prompt(cls, v: object) -> object:
        # YAML 1.1 parses bare on/off as booleans; accept them as intended.
        if isinstance(v, bool):
            return "on" if v else "off"
        return v

    @field_validator("models")
    @classmethod
    def _unique_models(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("solvers.models must be unique")
        return v

    @model_validator(mode="after")
    def _check_models_xor_sample(self) -> "SolversConfig":
        # Exactly one of an explicit `models` list / a `sample` draw.
        if (self.sample is None) == (len(self.models) == 0):
            raise ValueError("solvers must set exactly one of models / sample")
        return self


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

    prompt: list[str] = Field(default_factory=lambda: ["builtin:standard"], min_length=1)
    grader: list[str] = Field(default_factory=list)
    rubric: list[str] = Field(default_factory=lambda: ["builtin:standard"], min_length=1)
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
    # Render the rubric as two messages split at {solution}: the shared head
    # (rubric + problem + scheme + reference) becomes a system message — where
    # an explicit provider cache breakpoint lands — and the solution the user
    # message. Lets same-item judge calls share a cached prefix on providers
    # with block-granular caching (Anthropic). Changes the grade condition id.
    split_rubric: bool = False
    # Same contract as solvers.provider_routing (judges route too).
    provider_routing: "dict[str, Any] | None" = None


class MaterializeSpec(BaseModel):
    """Per-item rubric materialization: an LLM renders a frozen rubric from the
    item's reference only (no candidate solution), reused verbatim by every
    grader call for that item. A generated marking scheme is a multi-section
    document, so `max_tokens` defaults high (2048), not the judge's 512."""

    model_config = ConfigDict(extra="forbid")

    model: str  # the materializer model id
    template: str  # build template ref; rendered over {input,target,grading_scheme,id}
    max_tokens: int | None = Field(default=2048, ge=1)
    reasoning_effort: ReasoningEffort | None = None


class RubricSpec(BaseModel):
    """A named two-stage rubric (declared under top-level `rubrics:`). A
    `facets.rubric` name found here materializes a per-item rubric before
    grading; a bare/`builtin:` name stays a plain template reference, unchanged.
    Temperature is pinned to 0.0 (the materialized rubric is a frozen artifact)."""

    model_config = ConfigDict(extra="forbid")

    grade_template: str  # grade template ref; receives the materialized {rubric} + {solution}
    materialize: MaterializeSpec


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["dev", "full-interactive", "full-batch"] = "dev"
    confirm_above_usd: float = Field(default=5.0, ge=0.0)
    batch: bool | int | Literal["auto"] = "auto"
    max_usd: float | None = Field(default=None, gt=0.0)  # hard cap, never overridable
    dev_items: int = Field(default=2, ge=1)  # dev preset: first N items
    dev_replications: int | None = Field(default=None, ge=1)  # None = keep config reps
    pricing_path: str | None = None
    # Auto-refresh the cached pricing table from OpenRouter when it is at least
    # this many days old (best-effort; failures keep the stale table). None
    # disables it; ignored when pricing_path pins an explicit table.
    pricing_max_age_days: float | None = Field(default=None, ge=0.0)
    # Warm-then-fan-out scheduling of same-prefix calls so replications and
    # judge fan-outs hit provider prompt caches (see docs/COST-OPTIMIZATION.md).
    # "auto" gates whenever a condition has same-prefix groups of ≥2 calls and
    # batch mode is off; "off" disables scheduling entirely.
    cache_schedule: Literal["auto", "off"] = "auto"
    # Under a batch plan, route OpenRouter-sampled models to their native
    # provider API so they actually receive the provider batch discount
    # (OpenRouter has no batch API). Opt-in optimization knob: switching the
    # serving endpoint can change outputs and confounds an endpoint comparison,
    # so it is never silent (like provider_routing). The sampled openrouter/* id
    # stays the model's scientific identity; the native id is recorded as the
    # execution id. Inert (warns) off-batch or with no routable model. See
    # docs/wiki/Cost-Savings.md#native-batch-routing.
    prefer_native_batch: bool = False

    @field_validator("cache_schedule", mode="before")
    @classmethod
    def _yaml_bool_cache_schedule(cls, v: object) -> object:
        # YAML 1.1 parses bare off as boolean False; accept it as intended.
        if isinstance(v, bool):
            return "auto" if v else "off"
        return v


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str = Field(pattern=STUDY_RE)
    output_dir: str = "studies"  # outputs: resolved relative to work_dir (CWD)
    prompts_dir: str = (
        "prompts"  # solver templates: <prompts_dir>/solver/<name>.md (relative to config_dir)
    )
    rubrics_dir: str = (
        "rubrics"  # rubric templates: <rubrics_dir>/<name>.md (relative to config_dir)
    )
    cache: bool = True  # inspect local response cache, both stages
    benchmark: BenchmarkConfig
    solvers: SolversConfig
    facets: FacetsConfig
    graders: dict[str, GraderSpec] = Field(default_factory=dict)
    rubrics: dict[str, RubricSpec] = Field(default_factory=dict)  # two-stage rubric specs
    crossing: Literal["full"] = "full"
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    # Two resolution anchors (see docs/wiki/Configuration.md):
    #   config_dir — the loaded YAML's directory; anchors INPUTS (prompts/rubrics/pricing).
    #                None for in-memory configs, which then anchor inputs to work_dir.
    #   work_dir   — defaults to CWD; anchors OUTPUTS (the study directory). Never the package.
    _config_dir: Path | None = PrivateAttr(default=None)
    _work_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _config_path: Path | None = PrivateAttr(default=None)
    _config_sha256: str | None = PrivateAttr(default=None)

    @property
    def config_dir(self) -> Path | None:
        return self._config_dir

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def config_sha256(self) -> str | None:
        return self._config_sha256

    @property
    def _input_base(self) -> Path:
        """Anchor for input dirs: the config's directory, or work_dir for in-memory configs."""
        return self._config_dir if self._config_dir is not None else self._work_dir

    def resolve_input_dir(self, rel: str) -> Path:
        """Resolve an input dir (prompts/rubrics/pricing) under config_dir; absolute paths pass through."""
        p = Path(rel).expanduser()
        return (p if p.is_absolute() else self._input_base / p).resolve()

    @property
    def study_dir(self) -> Path:
        """Output study directory, anchored to work_dir (CWD); absolute output_dir passes through."""
        out = Path(self.output_dir).expanduser()
        base = out if out.is_absolute() else self._work_dir / out
        return (base / self.study).resolve()

    def grader_spec(self, name: str) -> GraderSpec:
        """Resolve a facets.grader entry. Raises ConfigError if unresolvable."""
        if name in self.graders:
            return self.graders[name]
        if "/" in name:  # bare model id used directly as a grader
            return GraderSpec(model=name)
        raise ConfigError(f"grader '{name}' is not defined under graders: and is not a model id")

    def rubric_spec(self, name: str) -> "RubricSpec | None":
        """A `facets.rubric` name declared under `rubrics:` (two-stage
        materialization), or None for a plain template reference (today's path)."""
        return self.rubrics.get(name)


def load_config(path: "str | Path", *, work_dir: "str | Path | None" = None) -> ExperimentConfig:
    """Load and validate an experiment config YAML file.

    Inputs (prompts/rubrics) anchor to the config file's directory; outputs (the
    study dir) anchor to `work_dir`, defaulting to the current working directory.
    """
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
    cfg._config_dir = p.parent
    cfg._work_dir = Path(work_dir).expanduser().resolve() if work_dir is not None else Path.cwd()
    cfg._config_path = p
    cfg._config_sha256 = sha256_hex(raw)
    return cfg


def config_to_jsonable(cfg: ExperimentConfig) -> dict[str, Any]:
    """Config as a JSON-ready dict using YAML key names (for manifests)."""
    return cfg.model_dump(mode="json", by_alias=True)
