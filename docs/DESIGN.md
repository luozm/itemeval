# itemeval — Implementation Design Contract

Status: binding design for milestones M1–M6. Implementer agents build from this
document plus `README.md`, `ROADMAP.md`, `CLAUDE.md`, `DEVELOPMENT.md`. Where
this document is more specific than the README sketch, this document wins.
Where it conflicts with repo conventions (`CLAUDE.md`), conventions win.

Verified against installed `inspect-ai==0.3.239` (see recon notes embedded
inline). Python `>=3.10` syntax only (PEP 604 unions OK; **no** `tomllib`,
`StrEnum`, `Self`, `except*`). All runs in tests/demos use `mockllm/*` models —
**zero paid API calls anywhere in tests or exit-criterion demos**.

## Amendments (adopted during implementation review — these win over the text below)

1. `eval()` has **no** `batch` kwarg in 0.3.239: batch flows through
   `GenerateConfig.batch` on the task config (§8.4 step 7 corrected).
2. `GraderSpec.temperature` removed — judge temperature is pinned to 0.0 for
   v0.1 (ROADMAP M3); the judge condition payload records the constant 0.0.
3. Parse-flag invariant unified across verifiable scorers and the judge
   parser: `parse_ok=False` ⟺ `parse_error` set ⟺ `score` null
   (`exact_match` empty target is `parse_ok=False`/`empty_target`). Rows with
   a sample-level `error` are a separate channel (`error` set,
   `parse_error` null) and are re-run; parse failures are final.
4. `ExportResult.reconciled` renamed `internally_reconciled` (ledger vs row
   sums); reconciliation against provider dashboards is a documented manual
   step, not an automated check.
5. `DEFAULT_OUTPUT_TOKENS_GENERATE` raised 1024 → 4096 and `estimate` prints
   an `uncapped-generation` warning when no `max_tokens` is configured, so the
   gate is not driven by a structural under-estimate.
6. Subpackage `__init__.py` files are docstring-only (no convenience
   re-exports); internal imports target the `_modules` directly. Public
   surface stays exactly `__version__`, `Item`, `ExperimentConfig`,
   `load_config`.
7. Manifests gain `sampling_effective` (per-condition effective params),
   backfilled by `finalize_manifest()` after each generate run — satisfying
   the README's "effective values in the manifest" promise.
8. Missing per-sample usage with a *priced* model is recorded as `usd=0.0`
   (inspect response-cache hit ⇒ free), not null; null usd strictly means
   "model unpriced".
9. Demo configs live in `configs/` and use `../prompts`, `../rubrics`,
   `../studies` (paths resolve relative to the config file's directory).
10. `prepare_study(config, refresh_pricing_table=...)` (renamed kwarg);
    conftest fixtures route the `hf` adapter to an offline fake for all
    non-network tests.

---

## 0. Overview & module dependency graph

Two decoupled stages share one study output directory:

```
benchmark source ─▶ adapter ─▶ items ─┐
                                      ├─▶ GENERATE ─▶ solutions store ─▶ GRADE ─▶ gradings table
design.yaml ─▶ facet grid expansion ──┘   (inspect)    (parquet+logs)   (inspect)  (long-format)
```

- **generate** runs one inspect `Task` per generation condition
  (model × prompt × model_config); `epochs` = replications. Output rows are
  upserted into `solutions.parquet`.
- **grade** fans out grade conditions (judge: grader × rubric; verifiable:
  scorer) over **stored** solutions — never re-generates. Judge grading is its
  own inspect task; verifiable scoring is pure in-process Python ($0).
- **export** joins gradings × solutions into one long-format row per grading
  event (parquet + CSV mirror) and mirrors the cost ledger.
- **budget** estimates USD before any run; the CLI enforces a confirmation
  gate.
- Every run writes a JSON **manifest** (full reproducibility record).

### Module dependency graph (arrows = "imports from"; no cycles)

```
                 ┌────────────────────────────────────────────────────┐
                 │ cli.py                                             │
                 └─┬───────┬─────────┬────────┬────────┬────────┬─────┘
                   ▼       ▼         ▼        ▼        ▼        ▼
              _prepare  _status   generate/  grade/  budget/  store/_export
                   │       │         │  │      │  │     │         │
   ┌───────────────┘       │         │  └──────┼──┼─────┤         │
   ▼                       ▼         ▼         ▼  ▼     ▼         ▼
 adapters/             design/   _mockmodels  store/  _manifest  store/
   │                       │         │          │        │         │
   ▼                       ▼         ▼          ▼        ▼         ▼
 _config ◀── _templates   _config   (inspect)  _config  _config   _config
   │             │           │                  │        │
   ▼             ▼           ▼                  ▼        ▼
 _item, _errors, _util  (leaf modules: no internal imports)
```

Leaf modules `_item.py`, `_errors.py`, `_util.py` import nothing from
itemeval. `_config.py` imports only `_errors`. `_templates.py` imports
`_errors`, `_util`. Everything else builds on those. `_manifest.py` and the
stage runners may import `_prepare` types under `typing.TYPE_CHECKING` only.

---

## 1. File-by-file layout (exact, final)

```
src/itemeval/
  __init__.py            # public API: __version__, Item, ExperimentConfig, load_config
  cli.py                 # argparse CLI: estimate | generate | grade | export | status
  _errors.py             # exception hierarchy
  _util.py               # canonical_json, sha256_hex, utc_now_iso, new_run_id,
                         # atomic_write_bytes, estimate_tokens, drop_none
  _item.py               # Item model
  _config.py             # ExperimentConfig + all config sub-models + load_config
  _templates.py          # Template, TemplateRegistry, render_template, content hash
  _mockmodels.py         # mockllm pass-through (deterministic mock outputs + usage)
  _manifest.py           # Manifest models + build_manifest + write_manifest
  _prepare.py            # PreparedStudy aggregate + prepare_study()
  _status.py             # StatusReport + build_status()
  adapters/
    __init__.py          # internal re-exports: get_adapter, load_items, LoadedDataset
    _base.py             # Adapter protocol, registry, dataset lock file, load_items
    _hf.py               # HFAdapter (HuggingFace datasets, pinned revision)
  design/
    __init__.py          # internal re-exports: expand_grid, Grid, GenCondition, GradeCondition
    _ids.py              # slugify, condition_digest, make_condition_id
    _grid.py             # GenParams, GenCondition, GradeCondition, Grid, expand_*
  generate/
    __init__.py          # internal re-exports: run_generate, build_generate_task
    _task.py             # build_generate_task()
    _params.py           # EffectiveParams + extract_effective_params()
    _run.py              # run_generate() orchestrator + rows_from_generate_log()
  grade/
    __init__.py          # internal re-exports: run_grade, parse_judge_output, VERIFIABLE_SCORERS
    _verifiable.py       # exact_match / multiple_choice / numeric (pure functions)
    _parse.py            # strict judge-output parsing (ParsedGrade)
    _judge.py            # JUDGE_FORMAT_SUFFIX, build_judge_task()
    _run.py              # run_grade() orchestrator
  store/
    __init__.py          # internal re-exports of the functions below
    _base.py             # upsert_parquet, read_parquet_or_empty, rel_to_study
    _layout.py           # StudyPaths
    _items.py            # ITEMS_SCHEMA, upsert_items, read_items
    _solutions.py        # SOLUTIONS_SCHEMA, upsert/read, items_to_run
    _gradings.py         # GRADINGS_SCHEMA, upsert/read, pending_solutions
    _logs.py             # LOG_INDEX_SCHEMA, upsert_log_index, read_log_index
    _ledger.py           # LEDGER_SCHEMA, upsert_ledger, read_ledger
    _export.py           # export_study() — long table + CSV mirrors
  budget/
    __init__.py          # internal re-exports: load_pricing, estimate_study, check_gate, ...
    pricing_seed.json    # packaged static pricing seed (data file)
    _pricing.py          # PricingTable, lookup_price, cost_usd, refresh_pricing
    _policies.py         # EffectivePlan, effective_plan, effective_batch
    _estimator.py        # Estimate models + estimate_study()
    _gate.py             # GateResult + check_gate()

configs/
  usamo_demo.yaml        # pilot demo config (pinned HF dataset + mockllm models)
  usamo_demo_gate.yaml   # same, confirm_above_usd: 0.0 (M5 gate demo)

prompts/solver/minimal.md
prompts/solver/standard.md
rubrics/standard.md

tests/                   # see §16 for ownership and contents
docs/DESIGN.md           # this file
```

Study output directory layout (created under `<config-dir>/<output_dir>/<study>/`):

```
studies/<study>/
  items.parquet            # canonical items snapshot
  solutions.parquet        # one row per (gen condition × item × epoch)
  gradings.parquet         # one row per grading event
  log_index.parquet        # index of raw .eval logs
  ledger.parquet           # cost ledger (per run × stage × condition × model)
  dataset_locks.json       # dataset revision pins ("pinned at first run")
  manifests/<run_id>.json  # one manifest per generate/grade run
  logs/generate/<condition_id>/*.eval
  logs/grade/<condition_id>/*.eval
  export/gradings_long.parquet
  export/gradings_long.csv
  export/ledger.csv
```

---

## 2. Core models

### 2.1 `src/itemeval/_errors.py`

```python
class ItemevalError(Exception):
    """Base class for all itemeval errors."""

class ConfigError(ItemevalError): ...      # YAML shape/validation, bad references
class AdapterError(ItemevalError): ...     # dataset load / mapping failures
class TemplateError(ItemevalError): ...    # missing template file / placeholder
class StoreError(ItemevalError): ...       # parquet schema/IO problems
class BudgetError(ItemevalError): ...      # pricing refresh / estimator failures
```

CLI exit-code mapping (see §14): `ConfigError|TemplateError|AdapterError` → 2,
other `ItemevalError` → 1.

### 2.2 `src/itemeval/_util.py`

```python
import hashlib, json, math, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def new_run_id(stage: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stage}_{ts}_{uuid.uuid4().hex[:8]}"

def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to `<path>.tmp` then os.replace() into place. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)

def estimate_tokens(text: str) -> int:
    """Token heuristic used by the estimator AND the mock models: ceil(chars/4)."""
    return max(1, math.ceil(len(text) / 4))

def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow: remove keys whose value is None (used for condition payloads)."""
    return {k: v for k, v in d.items() if v is not None}
```

### 2.3 `src/itemeval/_item.py`

```python
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Item(BaseModel):
    """Canonical benchmark item (ROADMAP M1)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str                                    # unique across the whole study
    input: str                                 # problem statement (non-empty)
    target: str = ""                           # reference answer/solution ("" if none)
    grading_scheme: str | None = None          # rubric/points spec as text (JSON ok)
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
```

`grading_scheme` coercion happens in the adapter (§5): non-string dataset
values are serialized with `canonical_json`.

---

## 3. Config schema (`src/itemeval/_config.py`) + example YAML

All models: `model_config = ConfigDict(extra="forbid")`. The README sketch
**must validate as-is** (acceptance test in `tests/test_config.py` embeds the
sketch verbatim). Reference resolution (prompt files exist, grader names
defined) is deliberately deferred to `prepare_study()` / grid expansion — YAML
*shape* validation happens at load, *reference* validation at command time.

```python
from pathlib import Path
from typing import Any, Literal
from pydantic import (BaseModel, ConfigDict, Field, PrivateAttr,
                      field_validator, model_validator)

NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"      # facet/grader/template names
STUDY_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"       # study slug

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                                    # e.g. "MathArena/usamo_2025"
    revision: str | None = None                # branch/tag/SHA; None -> lock/resolve
    split: str = "train"
    name: str | None = None                    # HF config name
    limit: int | None = Field(default=None, ge=1)   # first N rows, no shuffle


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str                                 # record column -> Item.input (required)
    target: str | None = None                  # record column -> Item.target
    id: str | None = None                      # record column -> Item.id (else row index)
    grading_scheme: str | None = None          # record column -> Item.grading_scheme
    metadata: list[str] = Field(default_factory=list)  # columns copied into Item.metadata


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["hf"]                     # only "hf" in v0.1 (registry, §5)
    datasets: list[DatasetSpec] = Field(min_length=1)
    mapping: MappingSpec


class SolversConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[str] = Field(min_length=1)    # inspect model ids
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int | None = None                    # recorded; only some providers honor it

    @field_validator("models")
    @classmethod
    def _unique_models(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("solvers.models must be unique")
        return v


class ModelConfigFacet(BaseModel):
    """One model-config grid cell. M2 checkbox: thinking/reasoning toggle."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)  # overrides solvers.*
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    reasoning_effort: ReasoningEffort | None = None   # OpenAI-style
    reasoning_tokens: int | None = Field(default=None, ge=1)  # Anthropic extended thinking


class FacetsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: list[str] = Field(default_factory=lambda: ["default"], min_length=1)
    grader: list[str] = Field(default_factory=list)
    rubric: list[str] = Field(default_factory=lambda: ["default"], min_length=1)
    scorer: Literal["exact_match", "multiple_choice", "numeric"] | None = None
    replications: int = Field(default=1, ge=1)
    model_config_facet: list[ModelConfigFacet] = Field(
        default_factory=lambda: [ModelConfigFacet(name="default")],
        alias="model_config", min_length=1)

    @model_validator(mode="after")
    def _grading_present(self) -> "FacetsConfig":
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
```

**Pydantic gotcha**: `model_config` is reserved on pydantic models, so the
facet list is stored as `model_config_facet` with `alias="model_config"`;
set `model_config = ConfigDict(extra="forbid", populate_by_name=True)` on
`FacetsConfig` so both names work programmatically. YAML always uses
`model_config:`.

```python
class GraderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str                                  # inspect model id of the judge
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=2048, ge=1)
    reasoning_effort: ReasoningEffort | None = None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["dev", "full-interactive", "full-batch"] = "dev"
    confirm_above_usd: float = Field(default=5.0, ge=0.0)
    batch: bool | int | Literal["auto"] = "auto"
    max_usd: float | None = Field(default=None, gt=0.0)   # hard cap, never overridable
    dev_items: int = Field(default=2, ge=1)               # dev preset: first N items
    dev_replications: int | None = Field(default=None, ge=1)  # None = keep config reps
    pricing_path: str | None = None                       # explicit pricing JSON


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: str = Field(pattern=STUDY_RE)
    output_dir: str = "studies"        # resolved relative to the config file's dir
    prompts_dir: str = "prompts"       # solver templates: <prompts_dir>/solver/<name>.md
    rubrics_dir: str = "rubrics"       # rubric templates: <rubrics_dir>/<name>.md
    cache: bool = True                 # inspect local response cache, both stages
    benchmark: BenchmarkConfig
    solvers: SolversConfig
    facets: FacetsConfig
    graders: dict[str, GraderSpec] = Field(default_factory=dict)
    crossing: Literal["full"] = "full" # only full crossing in v0.1
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _config_path: Path | None = PrivateAttr(default=None)
    _config_sha256: str | None = PrivateAttr(default=None)

    @property
    def base_dir(self) -> Path: return self._base_dir
    @property
    def config_path(self) -> Path | None: return self._config_path
    @property
    def config_sha256(self) -> str | None: return self._config_sha256
    @property
    def study_dir(self) -> Path:
        return (self._base_dir / self.output_dir / self.study).resolve()

    def grader_spec(self, name: str) -> GraderSpec:
        """Resolve a facets.grader entry. Raises ConfigError if unresolvable."""
        if name in self.graders:
            return self.graders[name]
        if "/" in name:                       # bare model id used directly as a grader
            return GraderSpec(model=name)
        raise ConfigError(
            f"grader '{name}' is not defined under graders: and is not a model id")


def load_config(path: str | Path) -> ExperimentConfig:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    raw = p.read_bytes()
    data = yaml.safe_load(raw)
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
```

### 3.1 Fully-annotated example YAML — `configs/usamo_demo.yaml` (exact file content)

```yaml
study: usamo_demo
output_dir: studies            # study dir: <config dir>/studies/usamo_demo
prompts_dir: prompts           # prompts/solver/<name>.md
rubrics_dir: rubrics           # rubrics/<name>.md
benchmark:
  adapter: hf
  datasets:
    - id: MathArena/usamo_2025
      revision: 0a2c60f2249e07b8ee76c942bca4f5f87aa959df
      split: train
  mapping:
    id: problem_idx
    input: problem
    target: sample_solution
    grading_scheme: grading_scheme
    metadata: [points]
solvers:
  models: [mockllm/solver-a, mockllm/solver-b, mockllm/solver-c]
  temperature: 0.7
  max_tokens: 1024
facets:
  prompt: [minimal, standard]
  grader: [mock_judge]
  rubric: [standard]
  replications: 2
graders:
  mock_judge:
    model: mockllm/judge
    max_tokens: 512
crossing: full
budget:
  policy: dev                  # dev: first 2 items, replications kept (dev_replications null)
  confirm_above_usd: 5
  batch: auto
```

`configs/usamo_demo_gate.yaml` is byte-identical except:
`study: usamo_demo_gate` and `confirm_above_usd: 0.0`.

Expected demo arithmetic (used by tests): 6 items in dataset; dev policy keeps
first 2 (ids `"1"`, `"2"` — `str(problem_idx)`); generate grid = 3 models × 2
prompts × 1 model_config = **6 conditions**; replications 2 ⇒ solutions rows =
6 × 2 × 2 = **24**; grade grid = 1 judge × 1 rubric = **1 condition** ⇒
gradings rows = **24**.

### 3.2 README-sketch acceptance

`tests/test_config.py::test_readme_sketch_validates` embeds the README YAML
sketch verbatim (study/benchmark/solvers/facets/crossing/budget exactly as in
README §"Experiment config") and asserts `ExperimentConfig.model_validate`
succeeds, with `facets.scorer is None`, `facets.rubric == ["default"]`,
`budget.batch == "auto"`. Grader names `judge_a`/`judge_b` are *not* resolved
at load time (resolution error surfaces from `grader_spec()` during grid
expansion — covered by `test_grader_unresolved_raises_config_error`).

---

## 4. Condition-id algorithm (`design/_ids.py`)

Condition ids are **stable and content-derived**: identical facet content on
any machine yields the identical id. An id is
`"<slug>--<digest12>"` — a human-readable slug plus the first 12 hex chars of
the SHA-256 of the canonical JSON of the condition payload.

```python
import re
from itemeval._util import canonical_json, sha256_hex

def slugify(text: str, max_len: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].strip("-")) or "x"

def model_short(model_id: str) -> str:
    """'openrouter/deepseek/deepseek-v3.2' -> 'deepseek-v3.2'."""
    return model_id.split("/")[-1]

def condition_digest(payload: dict) -> str:
    return sha256_hex(canonical_json(payload).encode("utf-8"))[:12]

def make_condition_id(slug_parts: list[str], payload: dict) -> tuple[str, str]:
    """Returns (condition_id, slug). slug = '_'.join(slugify(p) for p in parts)."""
    slug = "_".join(slugify(p) for p in slug_parts)
    return f"{slug}--{condition_digest(payload)}", slug
```

### Exact payloads

Generate condition (slug parts `[model_short(model), prompt_name, model_config_name]`):

```json
{"kind": "generate",
 "model": "<full inspect model id>",
 "model_config": {"name": "<facet name>", "params": {<resolved GenParams, None dropped>}},
 "prompt": {"name": "<prompt name>", "hash": "<prompt content sha256[:12]>"}}
```

`params` = `drop_none(gen_params.model_dump())` where `GenParams` is the
*resolved* sampling config (§6) — so editing `solvers.temperature` or a prompt
file's content changes the condition id; renaming changes the slug and the id.

Judge grade condition (slug parts `[grader_name, rubric_name]`):

```json
{"kind": "grade",
 "grader": {"name": "<grader name>", "model": "<judge model id>",
            "temperature": 0.0, "max_tokens": 2048,
            "reasoning_effort": null -> dropped via drop_none},
 "rubric": {"name": "<rubric name>", "hash": "<rubric content sha256[:12]>"},
 "format": 1}
```

`"format"` is `JUDGE_FORMAT_VERSION = 1` (§9.3) — bumping the packaged judge
output-format suffix changes judge condition ids.

Verifiable grade condition (slug parts `["scorer", scorer_name]`):

```json
{"kind": "grade", "scorer": "exact_match"}
```

Canonical JSON = `json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`. Hash = SHA-256 of its UTF-8 bytes; digest = first 12 hex
chars. Replication/epoch is **never** part of a condition id (it is a separate
`epoch` column). Items/datasets are never part of condition ids (full
crossing).

Example: `gpt-5-mini_minimal_default--3fa9c2d1e0b4`.

---

## 5. Adapters

### 5.1 `adapters/_base.py`

```python
from typing import Protocol
from pydantic import BaseModel, ConfigDict

class LoadedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    adapter: str
    split: str
    name: str | None = None
    revision_requested: str | None = None
    revision: str                      # resolved commit SHA actually loaded
    items: list[Item]

class Adapter(Protocol):
    def resolve_revision(self, spec: DatasetSpec) -> str: ...
    def load(self, spec: DatasetSpec, mapping: MappingSpec, revision: str) -> LoadedDataset: ...

_ADAPTERS: dict[str, type] = {"hf": HFAdapter}    # populated at import; extension point

def get_adapter(name: str) -> Adapter:
    """Raises AdapterError for unknown adapter names."""

# ---- dataset lock file ("revision pinned at first run") ----
LOCKS_VERSION = 1
def read_locks(path: Path) -> dict[str, str]:
    """{} if missing. File format below."""
def write_locks(path: Path, locks: dict[str, str]) -> None:
    """atomic_write_bytes; format below."""

def load_items(config: ExperimentConfig, locks_path: Path) -> list[LoadedDataset]:
    """For each DatasetSpec, resolve revision (precedence: spec.revision ->
    lock entry -> adapter.resolve_revision + write lock), then adapter.load().
    Asserts Item.id uniqueness ACROSS all datasets; duplicate -> AdapterError
    naming the colliding id and both dataset ids."""
```

`dataset_locks.json` exact format:

```json
{"version": 1,
 "datasets": {"MathArena/usamo_2025": {"revision": "<sha>", "resolved_at": "<utc iso>"}}}
```

`read_locks` returns `{dataset_id: revision}`. When `spec.revision` is set it
wins and the lock is updated to match.

### 5.2 `adapters/_hf.py` — `HFAdapter`

Uses `datasets.load_dataset` directly (NOT `inspect_ai.dataset.hf_dataset` —
we need `Item`s, not `Sample`s, and we avoid the FieldSpec metadata/choices
gotchas).

```python
class HFAdapter:
    def resolve_revision(self, spec: DatasetSpec) -> str:
        from huggingface_hub import HfApi   # dep of `datasets`
        info = HfApi().dataset_info(spec.id, revision=spec.revision)
        return info.sha                     # full commit SHA

    def load(self, spec: DatasetSpec, mapping: MappingSpec, revision: str) -> LoadedDataset:
        import datasets
        ds = datasets.load_dataset(spec.id, name=spec.name, split=spec.split,
                                   revision=revision)
        if spec.limit is not None:
            ds = ds.select(range(min(spec.limit, len(ds))))
        items = [_record_to_item(rec, idx, mapping) for idx, rec in enumerate(ds)]
        return LoadedDataset(dataset_id=spec.id, adapter="hf", split=spec.split,
                             name=spec.name, revision_requested=spec.revision,
                             revision=revision, items=items)
```

`_record_to_item(record: dict, index: int, mapping: MappingSpec) -> Item` —
exact mapping rules:

- `id`: `str(record[mapping.id])` if `mapping.id` else `str(index)` (0-based
  row position). Missing column → `AdapterError`.
- `input`: `str(record[mapping.input])`; missing column or empty/whitespace →
  `AdapterError` naming dataset+column.
- `target`: `str(record[mapping.target])` if `mapping.target` else `""`;
  `None` value → `""`.
- `grading_scheme`: if `mapping.grading_scheme` is set: value if already
  `str`, else `canonical_json(value)`; `None` value → `None`.
- `metadata`: `{col: record.get(col) for col in mapping.metadata}` (missing
  column → `None` value). Values stored as-is (must be JSON-serializable for
  the items store; non-serializable → `AdapterError` at store time).

The **one network test** (`tests/test_adapter_hf.py`, marked
`@pytest.mark.network`) loads `MathArena/usamo_2025` at revision
`0a2c60f2249e07b8ee76c942bca4f5f87aa959df` with the usamo_demo mapping and
asserts: 6 items; ids unique; every `input` non-empty; `grading_scheme` is a
non-empty `str`; `metadata["points"]` present. Register the marker in
`pyproject.toml` (`[tool.pytest.ini_options] markers = ["network: hits the
network (HF Hub, free)"]`); it runs by default, deselect with `-m "not
network"`. All other adapter tests use fake in-memory records (no network).

---

## 6. Design / grid (`design/_grid.py`)

```python
class GenParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    reasoning_tokens: int | None = None

def resolve_gen_params(solvers: SolversConfig, mc: ModelConfigFacet) -> GenParams:
    """Facet value overrides solvers default, field by field:
    temperature = mc.temperature if mc.temperature is not None else solvers.temperature
    (same for max_tokens, top_p); seed = solvers.seed;
    reasoning_effort/reasoning_tokens come only from the facet."""

class GenCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    slug: str
    model: str
    prompt_name: str
    prompt_hash: str            # 12 hex
    model_config_name: str
    gen_params: GenParams
    payload: dict               # exact §4 payload

class GradeCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    slug: str
    kind: Literal["judge", "verifiable"]
    grader_name: str | None = None
    grader_model: str | None = None
    grader_temperature: float | None = None
    grader_max_tokens: int | None = None
    grader_reasoning_effort: ReasoningEffort | None = None
    rubric_name: str | None = None
    rubric_hash: str | None = None
    scorer: str | None = None
    payload: dict

class Grid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replications: int                       # from facets (NOT policy-adjusted)
    generate: list[GenCondition]
    grade: list[GradeCondition]

def expand_generate_grid(config: ExperimentConfig,
                         solver_templates: dict[str, Template]) -> list[GenCondition]:
    """Deterministic order: for model in solvers.models:
                              for prompt in facets.prompt:
                                for mc in facets.model_config_facet: yield cond"""

def expand_grade_grid(config: ExperimentConfig,
                      rubric_templates: dict[str, Template]) -> list[GradeCondition]:
    """Order: verifiable condition first (if facets.scorer), then
    for grader in facets.grader: for rubric in facets.rubric. Grader names
    resolved via config.grader_spec(name) -> ConfigError if unresolvable.
    rubric_templates must contain every facets.rubric name (only required when
    facets.grader is non-empty)."""

def expand_grid(config, solver_templates, rubric_templates) -> Grid
```

Duplicate condition ids after expansion (possible only via pathological
configs) → `ConfigError`.

---

## 7. Prompts / rubrics registry (`src/itemeval/_templates.py`)

```python
class Template(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    path: str            # absolute path as str
    text: str            # newline-normalized content
    sha256: str          # full 64-hex content hash
    @property
    def hash12(self) -> str: return self.sha256[:12]

def load_template(path: Path, name: str) -> Template:
    """text = path.read_text(encoding='utf-8').replace('\r\n', '\n');
    sha256 = sha256_hex(text.encode('utf-8')). Missing file -> TemplateError
    with the absolute path in the message."""

class TemplateRegistry:
    def __init__(self, root: Path, kind: str) -> None: ...   # kind: "solver"|"rubric" (messages)
    def get(self, name: str) -> Template:
        """Loads <root>/<name>.md (cached per instance). TemplateError if missing,
        message lists root dir and available names()."""
    def names(self) -> list[str]:
        """Sorted *.md stems in root; [] if root missing."""

def solver_registry(config: ExperimentConfig) -> TemplateRegistry:
    # root = config.base_dir / config.prompts_dir / "solver"
def rubric_registry(config: ExperimentConfig) -> TemplateRegistry:
    # root = config.base_dir / config.rubrics_dir
```

**Content-hash algorithm** (the one recorded in condition payloads and
manifests): SHA-256 of the UTF-8 bytes of the file text after `\r\n` → `\n`
normalization. No other normalization (trailing whitespace is significant).

**Rendering** — `str.format` is forbidden (LaTeX/JSON braces in templates and
item text would break it). Exact replacement of known placeholders only:

```python
def render_template(text: str, values: Mapping[str, str]) -> str:
    pattern = re.compile("|".join(r"\{" + re.escape(k) + r"\}" for k in sorted(values)))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], text)

def validate_template(template: Template, required: set[str]) -> None:
    """TemplateError if any '{name}' for name in required is absent from text."""
```

Placeholder contracts:

- **Solver prompts** (`prompts/solver/<name>.md`): required `{input}`;
  optional `{id}`. Validated in `expand_generate_grid`.
- **Rubrics** (`rubrics/<name>.md`): required `{input}` and `{solution}`;
  optional `{target}`, `{grading_scheme}`, `{id}`. Validated in
  `expand_grade_grid`. Missing item values render as `""`.

Demo template files (exact content, owned by work unit U3):

`prompts/solver/minimal.md`
```
Solve the following problem. Show your reasoning, then state your final answer
on a line starting with "ANSWER:".

{input}
```

`prompts/solver/standard.md`
```
You are an expert competition mathematician. Solve the problem below with a
complete, rigorous argument. Show all work. End with a line starting with
"ANSWER:" giving your final answer.

Problem:
{input}
```

`rubrics/standard.md`
```
You are grading a candidate solution to a mathematics problem.

Problem:
{input}

Grading scheme:
{grading_scheme}

Reference solution:
{target}

Candidate solution:
{solution}

Evaluate the candidate solution against the grading scheme. Award a numeric
score according to the scheme.
```

(The structured-output instruction is NOT in the rubric — itemeval appends
`JUDGE_FORMAT_SUFFIX`, §9.3.)

---

## 8. Generate stage

### 8.1 `_mockmodels.py` — mockllm pass-through (shared by both stages)

Any configured model id beginning with `mockllm/` is wired to a deterministic
callable so the **entire pipeline runs free and reproducibly** (all demos, M6
CLI-only run). This is a documented dev affordance, not test-only code.

```python
from inspect_ai.model import GenerateConfig, Model, ModelOutput, ModelUsage, get_model

def is_mock_model(model: str) -> bool:
    return model.startswith("mockllm/")

def _last_user_text(input: list) -> str:
    """Text of the last user message ('' if none)."""

def mock_generate_callable(model: str):
    def fn(input, tools, tool_choice, config) -> ModelOutput:
        prompt = _last_user_text(input)
        h = sha256_hex(prompt.encode("utf-8"))
        content = (f"Mock solution from {model}.\n"
                   f"Deterministic reasoning over input hash {h[:12]}.\n"
                   f"ANSWER: {h[:6]}")
        out = ModelOutput.from_content(model=model, content=content, stop_reason="stop")
        it = sum(estimate_tokens(getattr(m, "text", "") or "") for m in input)
        ot = estimate_tokens(content)
        out.usage = ModelUsage(input_tokens=it, output_tokens=ot, total_tokens=it + ot)
        return out               # plain return; sync callable is supported
    return fn

def mock_judge_callable(model: str):
    def fn(input, tools, tool_choice, config) -> ModelOutput:
        prompt = _last_user_text(input)
        h = sha256_hex(prompt.encode("utf-8"))
        score = (int(h[:8], 16) % 101) / 10.0            # 0.0 .. 10.0, step 0.1
        body = canonical_json({"score": score,
                               "reasoning": f"Deterministic mock grade (h={h[:8]})."})
        content = f"Mock evaluation.\n\n```json\n{body}\n```\n"
        out = ModelOutput.from_content(model=model, content=content, stop_reason="stop")
        it = sum(estimate_tokens(getattr(m, "text", "") or "") for m in input)
        ot = estimate_tokens(content)
        out.usage = ModelUsage(input_tokens=it, output_tokens=ot, total_tokens=it + ot)
        return out
    return fn

def resolve_model(model: str, stage: str) -> "str | Model":
    """stage in {"generate", "grade"}. Non-mock ids pass through as the string.
    Mock ids -> get_model(model, custom_outputs=<callable for stage>).
    get_model memoization is disabled for mockllm (verified 0.3.239), so each
    call returns a fresh Model — no shared-iterator hazards. The CALLABLE form
    is mandatory (stateless => safe under concurrency and epochs)."""
```

Facts incorporated from recon: callable `custom_outputs` is invoked per
generate and its `usage` is respected verbatim (no fabrication on the callable
path — always set it); real `ModelOutput`s can only be passed
programmatically; response caching sits **above** the provider, so mock
outputs do cache — tests set `INSPECT_CACHE_DIR` to a tmpdir (autouse fixture,
§16) to keep runs hermetic.

### 8.2 `generate/_task.py`

```python
def build_generate_task(items: list[Item], cond: GenCondition, template: Template,
                        study: str, replications: int, cache: bool,
                        origins: dict[str, "DatasetOrigin"]) -> Task:
```

- One `Sample` per item:
  `Sample(input=render_template(template.text, {"input": item.input, "id": item.id}),
  target=item.target, id=item.id, metadata={"item_id": item.id,
  "dataset_id": origins[item.id].dataset_id,
  "dataset_revision": origins[item.id].revision, "condition_id": cond.id})`.
- `dataset=MemoryDataset(samples, name=f"{study}:{cond.id}")`.
- `solver=generate(cache=CachePolicy(expiry=None, per_epoch=True)) if cache
  else generate()` — `cache` flows through `GenerateConfigArgs` on the
  `generate()` solver (verified API; there is no named `cache` param).
  `per_epoch=True` (default) keeps replications distinct in the cache;
  `expiry=None` = cache forever (re-runs never re-pay).
- `config=GenerateConfig(temperature=p.temperature, top_p=p.top_p,
  max_tokens=p.max_tokens, seed=p.seed, reasoning_effort=p.reasoning_effort,
  reasoning_tokens=p.reasoning_tokens)` with `p = cond.gen_params`.
- `scorer=None`; `epochs=Epochs(replications)` (reducer irrelevant, no scorer).
- `name=f"gen_{cond.slug}"`, `version=0`,
  `metadata={"itemeval": {"stage": "generate", "study": study,
  "condition_id": cond.id}}`.

### 8.3 `generate/_params.py` — effective sampling params (M2 checkbox)

```python
class EffectiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_tokens: int | None = None

def extract_effective_params(sample: "EvalSample", requested: GenParams) -> EffectiveParams:
    """Find the LAST event in sample.events with event.event == "model";
    read event.config.<field>. For each field: effective = event value if not
    None else requested value. If there is no model event (errored sample) or
    events are missing, fall back entirely to requested. Never raises."""
```

`temperature_requested` and `temperature_effective` are both stored per row
(§10.2) — provider-forced values show up as a requested/effective mismatch.

### 8.4 `generate/_run.py`

```python
ModelFactory = Callable[[str, str], Any]   # (model_id, stage) -> str | Model

class ConditionRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str
    slug: str
    status: Literal["run", "skipped", "error"]
    items_run: int
    rows_written: int
    errors: int                  # samples with error in this run
    usd: float | None            # None when model unpriced
    log_file: str | None         # relative to study_dir

class GenerateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    study: str
    conditions: list[ConditionRunReport]
    rows_written: int
    total_usd: float
    manifest_path: str

def run_generate(prep: "PreparedStudy", *, run_id: str | None = None,
                 force: bool = False, condition_filter: list[str] | None = None,
                 display: str = "none", model_factory: ModelFactory | None = None,
                 estimate_usd: float | None = None) -> GenerateResult:
```

Exact algorithm:

1. `run_id = run_id or new_run_id("generate")`; `prep.paths.ensure()`.
2. Upsert items snapshot: `store.upsert_items(prep.paths, prep.items_all,
   prep.origins)` (ALL loaded items, not policy-limited).
3. Write the manifest **before** any eval (`_manifest.build_manifest(...)`,
   stage `"generate"`, `conditions_run` = filtered condition ids,
   `estimate_usd` passthrough).
4. Select conditions: all `prep.grid.generate`, filtered by
   `condition_filter` (match rule: `cond.id == f or cond.id.startswith(f) or
   cond.slug == f` for any filter `f`).
5. Per condition, **resume computation**: `items_to_run = store.items_to_run(
   solutions_df, cond.id, [it.id for it in prep.items_effective],
   prep.plan.replications)` — an item needs running iff any epoch in
   `1..replications` is missing OR has a non-null `error`. `force=True` → all
   items. Empty → report `status="skipped"`, no eval call.
6. Build task over the to-run items only (full epochs — completed epochs of a
   re-run item are absorbed by the inspect response cache when
   `config.cache=True`; documented re-pay risk when cache disabled).
7. ```python
   kwargs: dict[str, Any] = {}
   if prep.plan.batch is not None: kwargs["batch"] = prep.plan.batch
   logs = inspect_ai.eval(
       task, model=(model_factory or resolve_model)(cond.model, "generate"),
       display=display, log_dir=str(prep.paths.logs_dir("generate", cond.id)),
       log_format="eval", fail_on_error=False, retry_on_error=1,
       tags=["itemeval", "generate"],
       metadata={"itemeval_run_id": run_id, "itemeval_study": prep.config.study,
                 "itemeval_condition_id": cond.id},
       **kwargs)
   log = logs[0]    # full EvalLog in memory (samples included by default)
   ```
   `eval()` is called serially, one condition at a time (the `eval_async`
   single-run guard makes intra-process parallel evals unsafe).
8. `rows = rows_from_generate_log(log, cond, prep, run_id)`;
   `store.upsert_solutions(prep.paths, rows)`.
9. `store.upsert_log_index(...)` (one row per produced `.eval`, §10.4);
   `store.upsert_ledger(...)` (one row per (run, stage, condition, model),
   §10.5).
10. On exception from `eval()` for a condition: record
    `status="error"` in the report, continue with remaining conditions; CLI
    exits 1 if any condition errored.

```python
def rows_from_generate_log(log: "EvalLog", cond: GenCondition,
                           prep: "PreparedStudy", run_id: str) -> list[dict]:
```

Per `sample in log.samples`: usage = sum of `sample.model_usage.values()`
(`ModelUsage.__add__`); `error = sample.error.message if sample.error else
None`; `solution = sample.output.completion if (error is None and
sample.output.completion) else None`; `eff =
extract_effective_params(sample, cond.gen_params)`; `usd =
cost_usd(price, ...) * (0.5 if batch_used and provider in BATCH_PROVIDERS else 1.0)`
or `None` if unpriced (§11.2); `latency_s = sample.total_time`;
`log_file = os.path.relpath(log.location, prep.paths.study_dir)`. Column
mapping per §10.2.

### 8.5 Resume semantics summary (both stages)

- The **store is the source of truth** for completion; logs are evidence.
- Upserts are keyed (§10) and `keep="last"` — re-runs replace errored rows.
- Rows with `error != null` count as incomplete and are re-attempted.
- `--force` re-runs everything selected (rows replaced via upsert).
- Re-invoking `generate`/`grade` after an interrupt is always safe.

---

## 9. Grade stage

### 9.1 Verifiable scorers (`grade/_verifiable.py`) — no LLM, no inspect

```python
class VerifiableResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float | None            # 1.0/0.0 (None when parse failed)
    score_raw: str | None          # extracted answer segment (<=500 chars)
    parse_ok: bool
    parse_error: str | None

def extract_answer_segment(text: str) -> str:
    """Group 1 of the LAST match of r'(?im)^.*?ANSWER\s*:\s*(.*)$' stripped;
    falls back to the full text stripped if no match."""

def _norm(text: str) -> str:
    """' '.join(text.split()).casefold().rstrip('.')"""

def exact_match(solution: str, item: Item) -> VerifiableResult:
    """score = 1.0 if _norm(extract_answer_segment(solution)) == _norm(item.target)
    else 0.0; parse_ok always True; empty target -> parse_error='empty_target',
    score None."""

def multiple_choice(solution: str, item: Item) -> VerifiableResult:
    """target letter = item.target.strip().upper(); must match r'^[A-Z]$' else
    parse_error='target_not_letter'. Candidate = first r'\b([A-Za-z])\b' in
    extract_answer_segment(solution), uppercased; none found ->
    parse_error='no_letter_found', score None."""

def numeric(solution: str, item: Item) -> VerifiableResult:
    """Strip '$', ',' from both sides. Candidate = LAST match of
    r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?' in extract_answer_segment(solution);
    none -> 'no_number_found'. float(item.target) failure -> 'target_not_numeric'.
    score = 1.0 if math.isclose(cand, tgt, rel_tol=1e-6, abs_tol=1e-9) else 0.0."""

VERIFIABLE_SCORERS: dict[str, Callable[[str, Item], VerifiableResult]] = {
    "exact_match": exact_match, "multiple_choice": multiple_choice, "numeric": numeric}
```

### 9.2 Strict judge-output parsing (`grade/_parse.py`)

```python
class ParsedGrade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float | None
    reasoning: str | None
    score_raw: str | None          # repr of the raw 'score' JSON value
    parse_ok: bool
    parse_error: str | None        # one of: no_json_object | no_score_in_json |
                                   # score_not_numeric | score_not_finite

def parse_judge_output(completion: str) -> ParsedGrade:
```

Exact algorithm:

1. Fenced candidates: all matches of
   `re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)`, bodies stripped,
   iterated **last → first**; each tried with `json.loads`.
2. If no fenced candidate parses to a `dict` containing `"score"`: raw-brace
   candidates — for each index `i` of `"{"` in `completion`, iterated
   **last → first**, try `json.JSONDecoder().raw_decode(completion[i:])`.
3. Accept the first candidate that is a `dict` with key `"score"`. Track
   whether ANY dict (without score) was seen.
4. Validation of the accepted dict: `v = obj["score"]`. `bool` → not numeric.
   `int|float` → `float(v)`. `str` → `float(v)` if it parses, else
   `score_not_numeric`. Other types → `score_not_numeric`. Non-finite
   (`not math.isfinite`) → `score_not_finite`. `reasoning =
   str(obj["reasoning"])` if present and not None, else `None`.
5. No dict at all → `no_json_object`; dicts but none with `"score"` →
   `no_score_in_json`.
6. Failures return `parse_ok=False`, `score=None`, with `reasoning` carried
   when extractable, `score_raw` = repr of the offending value (or None).

**Parse failures are results, not errors**: they produce gradings rows with
`parse_ok=False` and are never dropped and never auto-retried.

### 9.3 Judge task (`grade/_judge.py`)

```python
JUDGE_FORMAT_VERSION = 1
JUDGE_FORMAT_SUFFIX = (
    "\n\n---\n"
    "After your evaluation, output your final grade as a JSON object in a fenced\n"
    "code block, exactly in this form (score must be a number):\n"
    "```json\n"
    '{"score": <number>, "reasoning": "<one-paragraph justification>"}\n'
    "```\n"
    "The JSON code block must be the last thing in your response.\n")

def build_judge_input(item: Item, solution: str, rubric: Template) -> str:
    values = {"input": item.input, "solution": solution, "target": item.target,
              "grading_scheme": item.grading_scheme or "", "id": item.id}
    return render_template(rubric.text, values) + JUDGE_FORMAT_SUFFIX

def judge_sample_id(gen_condition_id: str, item_id: str, epoch: int) -> str:
    return f"{gen_condition_id}::{item_id}::{epoch}"

def build_judge_task(pending: "pd.DataFrame",      # solutions-store rows to grade
                     items_by_id: dict[str, Item], cond: GradeCondition,
                     rubric: Template, study: str, cache: bool) -> Task:
```

- One `Sample` per pending solutions row:
  `Sample(input=build_judge_input(...), target=item.target,
  id=judge_sample_id(row.condition_id, row.item_id, int(row.epoch)),
  metadata={"gen_condition_id": ..., "item_id": ..., "epoch": int(...),
  "grade_condition_id": cond.id})`.
- `solver=generate(cache=CachePolicy(expiry=None, per_epoch=True)) if cache else generate()`.
- `config=GenerateConfig(temperature=cond.grader_temperature,
  max_tokens=cond.grader_max_tokens,
  reasoning_effort=cond.grader_reasoning_effort, cache_prompt="auto")` —
  `cache_prompt="auto"` exploits Anthropic prefix caching on repeated
  rubric+problem prefixes; other providers ignore it. Judge temperature is
  0.0 unless the GraderSpec overrides it (design requirement: temperature 0).
- `scorer=None` (scores parsed post-hoc from completions — keeps the parse
  contract in one place), `epochs=None` (1).
- `name=f"judge_{cond.slug}"`,
  `metadata={"itemeval": {"stage": "grade", "study": study, "condition_id": cond.id}}`.

### 9.4 `grade/_run.py`

```python
class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    study: str
    conditions: list[ConditionRunReport]     # reuse model from generate/_run
    rows_written: int
    parse_failures: int
    total_usd: float
    manifest_path: str

def run_grade(prep: "PreparedStudy", *, run_id: str | None = None,
              force: bool = False, condition_filter: list[str] | None = None,
              graders: list[str] | None = None, rubrics: list[str] | None = None,
              display: str = "none", model_factory: ModelFactory | None = None,
              estimate_usd: float | None = None) -> GradeResult:
```

Algorithm:

1. `run_id = run_id or new_run_id("grade")`. Read `solutions_df =
   store.read_solutions(prep.paths)`; empty → `StoreError("no solutions; run
   generate first")`.
2. **Gradable rows** = solutions rows with `error` null and `solution`
   non-null, restricted to policy-effective item ids and `epoch <=
   plan.replications`. Rows with generation errors are skipped (no grading
   row); `status` reports them as incomplete generation.
3. Select grade conditions from `prep.grid.grade`, filtered by
   `condition_filter` (same match rule as generate) and by
   `graders`/`rubrics` name lists when given (`grader_name in graders`,
   `rubric_name in rubrics`; verifiable conditions excluded when either
   filter is set).
4. **Verifiable conditions**: `pending = store.pending_solutions(solutions_df,
   gradings_df, cond.id, force)`; for each row call
   `VERIFIABLE_SCORERS[cond.scorer](row.solution, items_by_id[row.item_id])`;
   build gradings rows with `grade_kind="verifiable"`, all token columns
   null, `usd=0.0`, `latency_s=None`, `log_file=None`. No inspect, no model.
5. **Judge conditions**: same `pending` computation; build judge task; call
   `inspect_ai.eval(...)` exactly as §8.4 step 7 but `stage="grade"`,
   `log_dir=paths.logs_dir("grade", cond.id)`, model =
   `(model_factory or resolve_model)(cond.grader_model, "grade")`. For each
   sample: `parsed = parse_judge_output(sample.output.completion)`; row gets
   `score`, `reasoning`, `score_raw`, `parse_ok`, `parse_error`,
   `judge_completion = sample.output.completion`, usage/usd/latency/log_file
   as in generate. Sample-level errors → row with `error` set,
   `parse_ok=False`, `parse_error=None`.
6. Pending rule (`store.pending_solutions`): a solutions row is pending for
   grade condition G iff there is **no** gradings row with key
   `(G.id, row.condition_id, row.item_id, row.epoch)` having `error` null.
   Existing rows with `parse_ok=False` are **final** (not pending); rows with
   `error != null` are pending again. `force=True` → all gradable rows.
7. Manifest (stage `"grade"`) written before evals; upserts + log index +
   ledger as in generate. Solutions store is **never written** by this stage.

---

## 10. Store

### 10.1 Common (`store/_base.py`, `store/_layout.py`)

```python
class StudyPaths:
    def __init__(self, study_dir: Path) -> None:
        self.study_dir = study_dir
    @property
    def items(self) -> Path: return self.study_dir / "items.parquet"
    @property
    def solutions(self) -> Path: return self.study_dir / "solutions.parquet"
    @property
    def gradings(self) -> Path: return self.study_dir / "gradings.parquet"
    @property
    def log_index(self) -> Path: return self.study_dir / "log_index.parquet"
    @property
    def ledger(self) -> Path: return self.study_dir / "ledger.parquet"
    @property
    def dataset_locks(self) -> Path: return self.study_dir / "dataset_locks.json"
    @property
    def manifests_dir(self) -> Path: return self.study_dir / "manifests"
    @property
    def export_dir(self) -> Path: return self.study_dir / "export"
    def logs_dir(self, stage: str, condition_id: str) -> Path:
        return self.study_dir / "logs" / stage / condition_id
    def ensure(self) -> None:
        """mkdir -p study_dir, manifests_dir, export_dir, study_dir/'logs'."""

def read_parquet_or_empty(path: Path, schema: "pa.Schema") -> "pd.DataFrame":
    """pd.read_parquet(path) if it exists, else an empty DataFrame with
    schema.names columns (object dtype)."""

def upsert_parquet(path: Path, rows: "list[dict] | pd.DataFrame",
                   key: list[str], schema: "pa.Schema") -> int:
    """1) df_new from rows; add any missing schema columns as None; column
       order = schema.names.
       2) df = concat(existing, df_new); drop_duplicates(subset=key,
       keep='last'); sort_values(key, kind='mergesort').
       3) table = pa.Table.from_pandas(df[schema.names], schema=schema,
       preserve_index=False)  # casts float-with-NaN back to nullable int64
       4) atomic write: pq.write_table(table, tmp) then os.replace.
       Returns len(df_new). Raises StoreError on cast failure (message names
       the column)."""

def rel_to_study(paths: StudyPaths, p: str | Path) -> str:
    return os.path.relpath(str(p), str(paths.study_dir))
```

Notes: on-disk parquet schemas (below) are authoritative; in-memory pandas may
upcast nullable ints to float64 — round-trip safety is guaranteed by step 3
and covered by tests. Single-process CLI assumption: no file locking;
concurrent invocations on one study dir are unsupported (documented, §17).

Dtype shorthand below: `string` = `pa.string()`, `int32` = `pa.int32()`,
`int64` = `pa.int64()`, `float64` = `pa.float64()`, `bool` = `pa.bool_()`.
Every column is nullable unless marked **req**.

### 10.2 `store/_solutions.py`

`SOLUTION_KEY = ["condition_id", "item_id", "epoch"]`

| # | column | dtype | notes |
|---|--------|-------|-------|
| 1 | study | string **req** | |
| 2 | run_id | string **req** | generate run that wrote the row |
| 3 | condition_id | string **req** | gen condition |
| 4 | condition_slug | string **req** | |
| 5 | item_id | string **req** | |
| 6 | dataset_id | string **req** | |
| 7 | dataset_revision | string **req** | resolved SHA |
| 8 | epoch | int32 **req** | 1-based replication index |
| 9 | model | string **req** | requested inspect model id |
| 10 | prompt_name | string **req** | |
| 11 | prompt_hash | string **req** | 12 hex |
| 12 | model_config_name | string **req** | |
| 13 | temperature_requested | float64 | |
| 14 | temperature_effective | float64 | §8.3 |
| 15 | top_p_requested | float64 | |
| 16 | top_p_effective | float64 | |
| 17 | max_tokens_requested | int64 | |
| 18 | max_tokens_effective | int64 | |
| 19 | seed_requested | int64 | |
| 20 | reasoning_effort | string | requested (facet) |
| 21 | reasoning_effort_effective | string | |
| 22 | reasoning_tokens_requested | int64 | |
| 23 | solution | string | completion text; null when errored/empty |
| 24 | stop_reason | string | `sample.output.stop_reason` |
| 25 | error | string | sample error message; row kept |
| 26 | input_tokens | int64 | usage (excludes cached) |
| 27 | output_tokens | int64 | |
| 28 | total_tokens | int64 | |
| 29 | cache_read_tokens | int64 | `input_tokens_cache_read` |
| 30 | cache_write_tokens | int64 | `input_tokens_cache_write` |
| 31 | reasoning_tokens | int64 | usage `reasoning_tokens` |
| 32 | usd | float64 | null when model unpriced |
| 33 | latency_s | float64 | `sample.total_time` |
| 34 | log_file | string **req** | relative to study_dir |
| 35 | sample_uuid | string | |
| 36 | created_at | string **req** | UTC ISO |

```python
SOLUTIONS_SCHEMA: "pa.Schema"   # exactly the 36 columns above, in order
def read_solutions(paths: StudyPaths) -> "pd.DataFrame"
def upsert_solutions(paths: StudyPaths, rows: list[dict]) -> int
def items_to_run(df: "pd.DataFrame", condition_id: str, item_ids: list[str],
                 replications: int) -> list[str]:
    """Items (input order preserved) where NOT all epochs 1..replications have
    a row with error null for condition_id."""
```

### 10.3 `store/_gradings.py`

`GRADING_KEY = ["grade_condition_id", "gen_condition_id", "item_id", "epoch"]`

| # | column | dtype | notes |
|---|--------|-------|-------|
| 1 | study | string **req** | |
| 2 | run_id | string **req** | grade run |
| 3 | grade_condition_id | string **req** | |
| 4 | grade_condition_slug | string **req** | |
| 5 | gen_condition_id | string **req** | |
| 6 | item_id | string **req** | |
| 7 | epoch | int32 **req** | generation replication graded |
| 8 | grade_kind | string **req** | "judge" \| "verifiable" |
| 9 | grader_name | string | judge only |
| 10 | grader_model | string | judge only |
| 11 | rubric_name | string | judge only |
| 12 | rubric_hash | string | judge only, 12 hex |
| 13 | scorer_name | string | verifiable only |
| 14 | score | float64 | null when parse failed / errored |
| 15 | score_raw | string | raw extracted value |
| 16 | parse_ok | bool **req** | |
| 17 | parse_error | string | §9.2 codes |
| 18 | reasoning | string | judge reasoning text |
| 19 | judge_completion | string | full raw judge output |
| 20 | error | string | sample error message |
| 21 | input_tokens | int64 | judge usage; null for verifiable |
| 22 | output_tokens | int64 | |
| 23 | total_tokens | int64 | |
| 24 | cache_read_tokens | int64 | |
| 25 | cache_write_tokens | int64 | |
| 26 | reasoning_tokens | int64 | |
| 27 | usd | float64 | 0.0 for verifiable; null unpriced |
| 28 | latency_s | float64 | null for verifiable |
| 29 | log_file | string | null for verifiable |
| 30 | created_at | string **req** | |

```python
GRADINGS_SCHEMA: "pa.Schema"
def read_gradings(paths) -> "pd.DataFrame"
def upsert_gradings(paths, rows: list[dict]) -> int
def pending_solutions(solutions_df, gradings_df, grade_condition_id: str,
                      force: bool) -> "pd.DataFrame"   # rule in §9.4 step 6
```

### 10.4 `store/_logs.py` — raw `.eval` log index

Key: `["log_file"]`. Columns: `log_file` (string **req**, relative to
study_dir), `run_id`, `stage`, `condition_id`, `task_name`, `model`, `status`
(EvalLog.status), `started_at`, `completed_at` (from `log.stats`),
`total_samples` int64, `completed_samples` int64 (from `log.results`, null if
absent), `input_tokens`/`output_tokens`/`total_tokens` int64 (sum over
`log.stats.model_usage.values()`), `usd` float64, `created_at` string —
all string unless noted.

```python
LOG_INDEX_SCHEMA: "pa.Schema"
def upsert_log_index(paths, rows: list[dict]) -> int
def read_log_index(paths) -> "pd.DataFrame"
```

### 10.5 `store/_ledger.py` — cost ledger

Key: `["run_id", "stage", "condition_id", "model"]` (idempotent on retry).
Columns: `run_id` string **req**, `stage` string **req**
("generate"|"grade"), `condition_id` string **req**, `model` string **req**,
`calls` int64 **req** (n samples), `input_tokens`, `output_tokens`,
`total_tokens`, `cache_read_tokens`, `cache_write_tokens` int64, `usd`
float64 (sum of row usd; 0.0 for verifiable/unpriced-with-flag), `priced`
bool **req**, `batch` bool **req** (whether batch mode was enabled for the
run), `created_at` string **req**.

Invariant (tested at M4): for each `run_id`,
`ledger.usd == sum(solutions|gradings rows with that run_id).usd` to within
1e-9 (nulls treated as 0).

```python
LEDGER_SCHEMA: "pa.Schema"
def upsert_ledger(paths, rows: list[dict]) -> int
def read_ledger(paths) -> "pd.DataFrame"
```

### 10.6 `store/_items.py`

Key: `["item_id", "dataset_id"]`. Columns: `item_id` string **req**,
`dataset_id` string **req**, `dataset_revision` string **req**, `input`
string **req**, `target` string **req** (may be ""), `grading_scheme` string,
`metadata_json` string **req** (`canonical_json(item.metadata)`), `created_at`
string **req**.

```python
ITEMS_SCHEMA: "pa.Schema"
def upsert_items(paths, datasets: list[LoadedDataset]) -> int
def read_items(paths) -> "pd.DataFrame"
```

---

## 11. Budget

### 11.1 Pricing (`budget/_pricing.py`, `budget/pricing_seed.json`)

```python
class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float | None = None    # None -> 0.1 * input
    cache_write_usd_per_mtok: float | None = None   # None -> 1.25 * input

class PricingTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_at: str
    source: str            # "seed" | "openrouter" | "merged" | "file"
    models: dict[str, ModelPrice]
```

Packaged seed `src/itemeval/budget/pricing_seed.json` (exact content; prices
are deliberate June-2026 estimates, flagged for refresh before paid runs):

```json
{
  "updated_at": "2026-06-09T00:00:00Z",
  "source": "seed",
  "models": {
    "mockllm/*":                        {"input_usd_per_mtok": 3.0,  "output_usd_per_mtok": 15.0,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "openai/gpt-5":                     {"input_usd_per_mtok": 1.25, "output_usd_per_mtok": 10.0,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "openai/gpt-5-mini":                {"input_usd_per_mtok": 0.25, "output_usd_per_mtok": 2.0,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "anthropic/claude-haiku-4-5":       {"input_usd_per_mtok": 1.0,  "output_usd_per_mtok": 5.0,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "anthropic/claude-sonnet-4-5":      {"input_usd_per_mtok": 3.0,  "output_usd_per_mtok": 15.0,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "google/gemini-2.5-flash":          {"input_usd_per_mtok": 0.3,  "output_usd_per_mtok": 2.5,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null},
    "openrouter/deepseek/deepseek-v3.2":{"input_usd_per_mtok": 0.27, "output_usd_per_mtok": 0.4,
                                         "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null}
  }
}
```

`mockllm/*` is priced **nonzero on purpose**: it makes the cost ledger,
reconciliation tests, and the M5 gate demo exercise real dollar math at $0
actual spend.

```python
def seed_pricing() -> PricingTable:
    # importlib.resources.files("itemeval.budget").joinpath("pricing_seed.json")

def user_pricing_path() -> Path:
    # os.environ.get("ITEMEVAL_PRICING_PATH") or Path.home()/".cache/itemeval/pricing.json"

def load_pricing(explicit_path: str | None, base_dir: Path) -> PricingTable:
    """Precedence: explicit_path (resolved vs base_dir; missing -> BudgetError)
    -> user_pricing_path() if exists -> seed_pricing()."""

def refresh_pricing(timeout: float = 30.0) -> PricingTable:
    """GET https://openrouter.ai/api/v1/models via urllib.request (stdlib, no
    new deps). For each entry: per-token USD strings 'pricing.prompt' /
    'pricing.completion' * 1e6 -> per-Mtok. Write key 'openrouter/<id>'
    (overwrites seed openrouter keys); additionally write bare '<id>' only if
    absent from the seed (seed wins for native ids). Merged table
    (source='merged', updated_at=now) is written to user_pricing_path()
    (atomic) and returned. Network/JSON failure -> BudgetError."""

def lookup_price(table: PricingTable, model: str) -> ModelPrice | None:
    """Order: exact key; 'mockllm/*' if model.startswith('mockllm/');
    'openrouter/'+model; model without leading 'openrouter/'. Else None."""

def cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int,
             cache_read: int = 0, cache_write: int = 0) -> float:
    """(input*in + output*out + cache_read*(crp or 0.1*in)
        + cache_write*(cwp or 1.25*in)) / 1e6 ; None token values -> 0."""

BATCH_PROVIDERS = {"openai", "anthropic", "google", "grok", "together"}
def provider_of(model: str) -> str:   # model.split("/")[0]
```

### 11.2 Policies (`budget/_policies.py`)

```python
class EffectivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: str
    items_limit: int | None        # dev: budget.dev_items; else None
    replications: int              # dev: min(reps, dev_replications or reps); else reps
    batch: bool | int | None       # value passed to eval(batch=...); None = omit kwarg

def effective_plan(budget: BudgetConfig, replications: int) -> EffectivePlan:
    """batch resolution: budget.batch False -> None; True -> True; int -> int;
    'auto' -> True if policy == 'full-batch' else None.
    dev policy additionally forces batch to None (dev runs are interactive)."""

def apply_items_limit(items: list[Item], limit: int | None) -> list[Item]:
    """First N of the concatenated item list (datasets in config order)."""
```

### 11.3 Estimator (`budget/_estimator.py`)

```python
DEFAULT_OUTPUT_TOKENS_GENERATE = 1024   # used when max_tokens unset
DEFAULT_OUTPUT_TOKENS_JUDGE = 512

class ConditionEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str; slug: str; stage: str; model: str
    calls: int; input_tokens: int; output_tokens: int
    usd: float | None; priced: bool; batch_discount: bool

class StageEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str; calls: int; input_tokens: int; output_tokens: int
    usd: float                     # unpriced conditions contribute 0
    unpriced_models: list[str]
    conditions: list[ConditionEstimate]

class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: str; policy: str
    generate: StageEstimate; grade: StageEstimate
    total_usd: float

def estimate_study(prep: "PreparedStudy",
                   solutions_df: "pd.DataFrame | None" = None) -> Estimate:
```

Math (token heuristic = `estimate_tokens`, §2.2):

- **generate**, per condition: `calls = len(items_effective) * replications`;
  `input = sum(estimate_tokens(render_template(tpl.text, {"input": it.input,
  "id": it.id})) for it in items_effective) * replications`;
  `output = calls * (gen_params.max_tokens or DEFAULT_OUTPUT_TOKENS_GENERATE)`.
- **grade**, judge conditions only (verifiable = 0 calls, $0): one call per
  expected solution = `len(items_effective) * len(gen_conditions) *
  replications`. Per (item, gen condition, epoch): solution text = actual
  stored solution when present in `solutions_df`, else a placeholder of
  `4 * (gen max_tokens or DEFAULT_OUTPUT_TOKENS_GENERATE)` chars; `input =
  estimate_tokens(build_judge_input(item, solution_text, rubric))`;
  `output = grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE`.
- `usd = cost_usd(price, input, output)`; `price is None` → `usd=None`,
  `priced=False`, model appended to `unpriced_models`.
- Batch: if `plan.batch is not None` and `provider_of(model) in
  BATCH_PROVIDERS` → multiply condition usd by 0.5, `batch_discount=True`.
- The estimate always projects the **full policy-effective grid**, ignoring
  resume state (conservative; CLI prints a note). Target accuracy: within ~2×
  of actuals (M5 exit).

The same 0.5 batch multiplier is applied to **actual** per-row usd in stage
runners when the run had batch enabled (`batch` column in ledger records it);
provider invoices are authoritative — documented approximation (§17).

### 11.4 Gate (`budget/_gate.py`)

```python
class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proceed: bool
    exit_code: int        # 0 | 3 (declined / needs confirmation) | 4 (max_usd)
    reason: str

def check_gate(estimate_usd: float, budget: BudgetConfig, assume_yes: bool,
               interactive: bool | None = None) -> GateResult:
    """interactive default: sys.stdin.isatty().
    1. budget.max_usd is not None and estimate_usd > max_usd
       -> proceed=False, exit 4 (NEVER overridable by --yes).
    2. estimate_usd <= budget.confirm_above_usd -> proceed.
    3. assume_yes -> proceed (reason notes the override).
    4. interactive -> input('Estimated cost ${e:.2f} exceeds '
       'confirm_above_usd (${c:.2f}). Proceed? [y/N] ');
       strip().lower() in {'y','yes'} -> proceed else exit 3.
    5. else -> exit 3, reason: 're-run with --yes to confirm'."""
```

CLI calls the gate with the **stage-specific** estimate (`estimate.generate
.usd` for `generate`, `estimate.grade.usd` for `grade`).

---

## 12. Manifest (`src/itemeval/_manifest.py`)

```python
class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str; adapter: str; split: str; name: str | None = None
    revision_requested: str | None; revision_resolved: str
    n_items: int; items_hash: str        # sha256_hex(canonical_json(
                                          #   [[it.id, sha256_hex(it.input.encode())[:12]]
                                          #    for it in items]))[:12]  (loaded order)

class TemplateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str; path: str; sha256: str    # path relative to config.base_dir

class ConditionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str; slug: str; payload: dict

class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_version: int = 1
    run_id: str
    stage: Literal["generate", "grade"]
    study: str
    created_at: str
    itemeval_version: str                 # importlib.metadata.version("itemeval")
    python_version: str                   # platform.python_version()
    packages: dict[str, str]              # {"inspect-ai", "pandas", "pyarrow",
                                          #  "pydantic", "pyyaml", "datasets"}
                                          # via importlib.metadata.version; missing -> "unknown"
    config_path: str
    config_sha256: str                    # raw config file bytes
    config: dict                          # ExperimentConfig.model_dump(mode="json", by_alias=True)
    datasets: list[DatasetManifest]
    solver_templates: list[TemplateManifest]
    rubric_templates: list[TemplateManifest]
    models: list[str]                     # solver model ids
    graders: dict[str, dict]              # resolved GraderSpec.model_dump per used grader
    sampling_requested: dict              # SolversConfig.model_dump (minus models)
    seed: int | None                      # solvers.seed
    policy: str
    replications_requested: int           # facets.replications
    replications_effective: int           # plan.replications
    items_limit: int | None               # plan.items_limit
    batch: bool | int | None              # plan.batch
    grid_generate: list[ConditionManifest]
    grid_grade: list[ConditionManifest]
    conditions_run: list[str]             # condition ids selected for THIS run
    estimate_usd: float | None
    cache: bool

def build_manifest(prep: "PreparedStudy", stage: str, run_id: str,
                   conditions_run: list[str], estimate_usd: float | None) -> Manifest

def write_manifest(manifest: Manifest, paths: StudyPaths) -> Path:
    """atomic_write_bytes(paths.manifests_dir / f'{run_id}.json',
    (json.dumps(manifest.model_dump(mode='json'), indent=2,
    ensure_ascii=False) + '\n').encode())"""
```

Together with inspect's own `.eval` logs (which record `model_args`,
`GenerateConfig`, git revision, package versions) this satisfies the README
Reproducibility section: dataset ids + revisions, template content hashes,
model ids, requested+effective sampling params (effective live per-row in the
solutions store), seeds, package versions, condition grid.

---

## 13. Export (`store/_export.py`)

```python
class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: int
    gradings_parquet: str; gradings_csv: str; ledger_csv: str
    generation_usd: float; grading_usd: float
    reconciled: bool        # ledger vs row-sum check (tolerance 1e-6)

def export_study(config: ExperimentConfig) -> ExportResult:
```

Algorithm: read gradings (empty → `StoreError("nothing to export")`),
solutions, ledger. `long = gradings LEFT JOIN solutions ON
(gen_condition_id==condition_id, item_id, epoch)`. Rename/derive to the exact
column list below; write `export/gradings_long.parquet` (pyarrow, exact
schema) and byte-equivalent CSV mirror `export/gradings_long.csv`
(`df.to_csv(path, index=False)`); write `export/ledger.csv` mirroring
`ledger.parquet`. Reconciliation: per stage, `abs(sum(ledger.usd) -
sum(rows.usd)) <= 1e-6` (nulls = 0) → `reconciled`; mismatch prints a warning
(not an error).

**`gradings_long` exact columns, in order** (dtype in parens; from solutions
side prefixed `gen_`, from gradings side prefixed `grade_` for usage):

1. `study` (string) 2. `item_id` (string) 3. `dataset_id` (string)
4. `dataset_revision` (string) 5. `model` (string, solver model)
6. `prompt_name` (string) 7. `prompt_hash` (string)
8. `model_config_name` (string) 9. `replication` (int32, = epoch)
10. `gen_condition_id` (string) 11. `gen_condition_slug` (string)
12. `grade_condition_id` (string) 13. `grade_condition_slug` (string)
14. `grade_kind` (string) 15. `grader_name` (string) 16. `grader_model`
(string) 17. `rubric_name` (string) 18. `rubric_hash` (string)
19. `scorer_name` (string) 20. `score` (float64) 21. `score_raw` (string)
22. `parse_ok` (bool) 23. `parse_error` (string) 24. `reasoning` (string)
25. `solution` (string) 26. `judge_completion` (string)
27. `temperature_requested` (float64) 28. `temperature_effective` (float64)
29. `reasoning_effort` (string) 30. `gen_input_tokens` (int64)
31. `gen_output_tokens` (int64) 32. `gen_total_tokens` (int64)
33. `gen_reasoning_tokens` (int64) 34. `gen_usd` (float64)
35. `gen_latency_s` (float64) 36. `grade_input_tokens` (int64)
37. `grade_output_tokens` (int64) 38. `grade_total_tokens` (int64)
39. `grade_usd` (float64) 40. `grade_latency_s` (float64)
41. `gen_run_id` (string) 42. `grade_run_id` (string)
43. `gen_log_file` (string) 44. `grade_log_file` (string)
45. `created_at` (string, grading row timestamp)

One row per grading event — **never aggregated**; parse failures present with
`parse_ok=False`.

---

## 14. CLI (`src/itemeval/cli.py`) — argparse, no new deps

```
usage: itemeval {estimate,generate,grade,export,status} CONFIG [options]

estimate CONFIG [--stage {generate,grade,all}] [--refresh-pricing] [--json]
generate CONFIG [-y/--yes] [--force] [--condition F]... [--display {none,plain,rich,full}]
grade    CONFIG [-y/--yes] [--force] [--condition F]... [--grader N]... [--rubric N]...
                [--display {none,plain,rich,full}]
export   CONFIG [--json]
status   CONFIG [--json]
```

- `main(argv: list[str] | None = None) -> int`; `sys.exit(main())` under
  `if __name__ == "__main__":`. Subparsers `required=True`; each command is a
  `_cmd_<name>(args) -> int` function.
- `--condition` / `--grader` / `--rubric`: `action="append"` (repeatable).
- `--display` default `"none"`; passed straight to `eval(display=...)`.
- **Exit codes**: `0` success; `1` unexpected error or any condition-level
  eval failure; `2` config/template/adapter error (and argparse usage errors);
  `3` gate declined / confirmation required non-interactively; `4`
  `budget.max_usd` exceeded.
- Top-level handler: `except (ConfigError, TemplateError, AdapterError) as e:
  print(f"itemeval: error: {e}", file=sys.stderr); return 2`; other
  `ItemevalError` → 1 with same format. No tracebacks for ItemevalError.

Command flows (all reuse `prepare_study`, §15.1):

- **estimate**: `cfg = load_config; prep = prepare_study(cfg,
  refresh_pricing=args.refresh_pricing); est = estimate_study(prep,
  read_solutions_or_none)`. `--json` → `print(est.model_dump_json(indent=2))`;
  else fixed-width table: one line per stage (calls, input tokens, output
  tokens, usd), per-condition breakdown, `unpriced models:` line, and the
  note `(estimate projects the full policy-effective grid; completed work is
  not subtracted)`. Never prompts; **no model API calls**.
- **generate**: prepare → estimate → print generate-stage estimate →
  `check_gate(est.generate.usd, cfg.budget, args.yes)`; not proceed →
  print reason, return `gate.exit_code`. Else `run_generate(prep,
  force=args.force, condition_filter=args.condition, display=args.display,
  estimate_usd=est.generate.usd)`. Print one line per condition:
  `[i/N] <slug>--<digest>  items=K epochs=R  rows=+M errors=E usd=$X.XX`
  (or `skipped: complete`), then summary + manifest path. Return 1 if any
  condition `status=="error"` else 0.
- **grade**: same shape with `est.grade.usd`, `run_grade(...,
  graders=args.grader, rubrics=args.rubric)`; summary line includes
  `parse_failures=P`.
- **export**: `export_study(cfg)`; print row count, file paths, generation
  vs grading USD, `reconciled: yes|no`. `--json` → `ExportResult` JSON.
- **status**: `build_status(cfg)` (§15.2); `--json` → `StatusReport` JSON.
  Plain output (exact shape, values vary):

```
study: usamo_demo  (policy: dev)
config: configs/usamo_demo.yaml
items: 6 loaded (MathArena/usamo_2025@0a2c60f2: 6) | policy-effective: 2
replications: 2 (effective: 2)

GENERATE — 6 conditions x 2 items x 2 epochs = 24 expected
condition                              model             prompt    config    done   err
solver-a_minimal_default--xxxxxxxxxxxx mockllm/solver-a  minimal   default   4/4    0
... (one row per condition)

GRADE — 1 condition x 24 solutions = 24 expected
condition                              grader      rubric    done   err  parse_fail
mock-judge_standard--xxxxxxxxxxxx      mock_judge  standard  24/24  0    0

spend: generate $0.43 | grade $0.12 | total $0.55
manifests: 2 (latest: manifests/grade_20260609T120301Z_ab12cd34.json)
```

`status` makes **no model API calls** (HF Hub metadata/dataset loads via the
pinned/locked revision are allowed and locally cached). Table rendering: a
module-private helper `_fmt_table(headers: list[str], rows: list[list[str]])
-> str` (left-aligned, two-space gutters); no third-party table libs.

---

## 15. Orchestration & public API

### 15.1 `src/itemeval/_prepare.py`

```python
class DatasetOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    revision: str

@dataclass
class PreparedStudy:
    config: ExperimentConfig
    paths: StudyPaths
    datasets: list[LoadedDataset]
    items_all: list[Item]                 # concatenated, config order
    items_effective: list[Item]           # after plan.items_limit
    items_by_id: dict[str, Item]          # all items
    origins: dict[str, DatasetOrigin]     # item_id -> origin
    solver_templates: dict[str, Template] # name -> Template (facets.prompt only)
    rubric_templates: dict[str, Template] # facets.rubric only; {} when no graders
    grid: Grid
    plan: EffectivePlan
    pricing: PricingTable

def prepare_study(config: ExperimentConfig, *, refresh_pricing: bool = False) -> PreparedStudy:
    """1) paths = StudyPaths(config.study_dir); paths.ensure()
       2) datasets = adapters.load_items(config, paths.dataset_locks)
       3) items_all (uniqueness already enforced); origins
       4) plan = effective_plan(config.budget, config.facets.replications);
          items_effective = apply_items_limit(items_all, plan.items_limit)
       5) solver_templates = {n: solver_registry(config).get(n) for n in facets.prompt}
          rubric_templates = {n: rubric_registry(config).get(n) for n in facets.rubric}
          (rubrics loaded only if facets.grader non-empty)
       6) grid = expand_grid(config, solver_templates, rubric_templates)
       7) pricing = refresh_pricing() if refresh_pricing else
          load_pricing(config.budget.pricing_path, config.base_dir)"""
```

### 15.2 `src/itemeval/_status.py`

```python
class DatasetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str; revision: str; n_items: int

class ConditionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str; slug: str; stage: str
    detail: dict[str, str]      # generate: {model, prompt, model_config};
                                # grade: {grader, rubric} or {scorer}
    expected: int; completed: int; errors: int; parse_failures: int = 0

class StatusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: str; policy: str; config_path: str
    datasets: list[DatasetStatus]
    n_items_total: int; n_items_effective: int
    replications_requested: int; replications_effective: int
    generate: list[ConditionStatus]
    grade: list[ConditionStatus]
    spend_generate_usd: float; spend_grade_usd: float
    manifests: list[str]        # sorted filenames

def build_status(config: ExperimentConfig) -> StatusReport:
    """prepare_study(config); read solutions/gradings/ledger parquets
    (empty-tolerant). expected per gen condition = n_items_effective *
    replications_effective; completed = rows with error null; errors = rows
    with error non-null. Per grade condition: expected = count of gradable
    solution rows (error null, solution non-null, within policy scope);
    completed = gradings rows with error null; parse_failures = completed
    with parse_ok == False. Spend = ledger sums per stage."""
```

### 15.3 Public API — `src/itemeval/__init__.py` (exact)

```python
"""itemeval: item-level LLM evaluation over any API, with built-in budget control."""

from importlib.metadata import version

from itemeval._config import ExperimentConfig, load_config
from itemeval._item import Item

__version__ = version("itemeval")
__all__ = ["ExperimentConfig", "Item", "__version__", "load_config"]
```

Nothing else is public. Subpackage `__init__.py` files re-export internal
names **for intra-package convenience only** (no stability promise):

- `adapters/__init__.py`: `get_adapter, load_items, LoadedDataset, read_locks, write_locks`
- `design/__init__.py`: `Grid, GenCondition, GradeCondition, GenParams, expand_grid, make_condition_id, slugify, condition_digest, model_short`
- `generate/__init__.py`: `run_generate, build_generate_task, GenerateResult, ConditionRunReport, extract_effective_params`
- `grade/__init__.py`: `run_grade, GradeResult, parse_judge_output, ParsedGrade, VERIFIABLE_SCORERS, build_judge_task, JUDGE_FORMAT_SUFFIX, JUDGE_FORMAT_VERSION`
- `store/__init__.py`: `StudyPaths, upsert_parquet, read_parquet_or_empty, rel_to_study` + all schema constants and read/upsert functions + `export_study, ExportResult`
- `budget/__init__.py`: `PricingTable, ModelPrice, load_pricing, refresh_pricing, lookup_price, cost_usd, provider_of, BATCH_PROVIDERS, EffectivePlan, effective_plan, apply_items_limit, Estimate, StageEstimate, ConditionEstimate, estimate_study, DEFAULT_OUTPUT_TOKENS_GENERATE, DEFAULT_OUTPUT_TOKENS_JUDGE, GateResult, check_gate`

---

## 16. Work-unit decomposition, test plan, exit demos

Rules: each unit owns a **disjoint** set of files (sources AND tests) and
implements them exactly to this contract; cross-unit imports target the
signatures in this document. `pyproject.toml` edits belong to U5 only
(pytest `network` marker). `CHANGELOG.md` edits belong to U20 only.
`tests/conftest.py` belongs to U1 and contains exactly:

```python
import pytest

@pytest.fixture(autouse=True)
def _inspect_hermetic_env(tmp_path, monkeypatch):
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path / "inspect_cache"))
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "inspect_logs"))
    monkeypatch.delenv("INSPECT_EVAL_MODEL", raising=False)
```

Unit tests never call paid APIs: model interactions are `mockllm/*` (via
`_mockmodels` callables or explicit `get_model("mockllm/model",
custom_outputs=[ModelOutput...])`), pricing refresh is monkeypatched
`urllib`, and only U5's marked test touches the network (HF Hub, free).

### Units (M = milestone whose exit they serve)

| Unit | M | Owns (sources) | Owns (tests) |
|------|---|----------------|--------------|
| U1 core | M1 | `_errors.py`, `_util.py`, `_item.py` | `tests/conftest.py`, `tests/test_util.py`, `tests/test_item.py` |
| U2 config | M1 | `_config.py`, `__init__.py` (public API) | `tests/test_config.py` (incl. README-sketch verbatim), `tests/test_public_api.py` |
| U3 templates | M1 | `_templates.py`, `prompts/solver/minimal.md`, `prompts/solver/standard.md`, `rubrics/standard.md` | `tests/test_templates.py` |
| U4 design | M1 | `design/_ids.py`, `design/_grid.py`, `design/__init__.py` | `tests/test_condition_ids.py`, `tests/test_grid.py` |
| U5 adapter | M1 | `adapters/_base.py`, `adapters/_hf.py`, `adapters/__init__.py`, `pyproject.toml` (marker) | `tests/test_adapter_mapping.py` (offline), `tests/test_adapter_hf.py` (network, pinned rev) |
| U6 manifest | M1 | `_manifest.py` | `tests/test_manifest.py` |
| U7 orchestration+CLI | M1/M6 | `_prepare.py`, `_status.py`, `cli.py` | `tests/test_status.py`, `tests/test_cli.py` (stage fns monkeypatched; exit codes; --json) |
| U8 demo configs | M1 | `configs/usamo_demo.yaml`, `configs/usamo_demo_gate.yaml` | `tests/test_demo_configs.py` (load+validate, expected grid sizes vs §3.1) |
| U9 mock models | M2 | `_mockmodels.py` | `tests/test_mockmodels.py` (determinism, usage respected, resolve passthrough) |
| U10 store core | M2 | `store/_base.py`, `store/_layout.py`, `store/_items.py`, `store/_solutions.py`, `store/_logs.py`, `store/__init__.py` | `tests/test_store_base.py` (upsert/dedup/atomicity/int-roundtrip), `tests/test_store_solutions.py`, `tests/test_store_logs.py` |
| U11 generate | M2 | `generate/_task.py`, `generate/_params.py`, `generate/_run.py`, `generate/__init__.py` | `tests/test_generate_task.py`, `tests/test_generate_run.py` (mockllm e2e to tmp study; resume skip; epoch-aware cache: callable invocation count with INSPECT_CACHE_DIR — 2 epochs ⇒ 2 calls, re-run ⇒ 0 new) |
| U12 verifiable | M3 | `grade/_verifiable.py` | `tests/test_verifiable.py` (table-driven: ANSWER extraction, mc letters, numeric tolerance, failure codes) |
| U13 parse | M3 | `grade/_parse.py` | `tests/test_judge_parse.py` (fenced/no-fence/multiple blocks/bool score/str score/inf/garbage → exact error codes) |
| U14 judge+grade run | M3 | `grade/_judge.py`, `grade/_run.py`, `grade/__init__.py` | `tests/test_judge_task.py`, `tests/test_grade_run.py` (mock judge e2e; parse-failure rows kept; rerun per grader×rubric leaves solutions untouched; pending logic) |
| U15 gradings store | M3 | `store/_gradings.py` | `tests/test_store_gradings.py` |
| U16 ledger | M4 | `store/_ledger.py` | `tests/test_ledger.py` (idempotent upsert; reconciliation invariant vs fabricated rows) |
| U17 export | M4 | `store/_export.py` | `tests/test_export.py` (exact 45-column schema+order, CSV mirror equality, reconciliation flag) |
| U18 pricing | M5 | `budget/_pricing.py`, `budget/pricing_seed.json` | `tests/test_pricing.py` (seed loads; lookup precedence incl. mockllm/* and openrouter cross-keys; cost_usd incl. cache fallbacks; refresh with mocked urllib) |
| U19 estimator+policies+gate | M5 | `budget/_estimator.py`, `budget/_policies.py`, `budget/_gate.py`, `budget/__init__.py` | `tests/test_policies.py`, `tests/test_estimator.py` (math on demo config ⇒ deterministic totals), `tests/test_gate.py` (all 5 branches; input monkeypatched) |
| U20 integration | M6 | `tests/test_integration_pipeline.py`, `CHANGELOG.md` entry | full CLI pipeline in tmp dir via `cli.main([...])`: status→generate→grade→export→status; asserts 24/24 rows, parse failures 0, export reconciles, second generate run skips all conditions |

Build order for sequencing (units within a milestone are parallel):
M1 = U1–U8 → M2 = U9–U11 → M3 = U12–U15 → M4 = U16–U17 → M5 = U18–U19 →
M6 = U20. U7's CLI compiles against every other unit's contract; it is
written once and integration-tested by U20.

### Exit-criterion demo commands (run from repo root; zero paid API calls)

All demos use `./.venv/bin/itemeval` (equivalently
`./.venv/bin/python -m itemeval.cli` — add `if __name__ == "__main__"` main
guard so both work; console script `itemeval` already declared in
pyproject).

- **M1**: `./.venv/bin/itemeval status configs/usamo_demo.yaml`
  → prints 6 generate conditions, 1 grade condition, `items: 6 loaded`,
  all completion 0/24, exit 0.
- **M2**: `./.venv/bin/itemeval generate configs/usamo_demo.yaml --yes`
  → `studies/usamo_demo/solutions.parquet` with 24 rows (3 mock models × 2
  prompts × 2 items × 2 epochs), `log_index.parquet` with 6 logs, manifest
  written. Re-running prints `skipped: complete` for all 6 conditions.
- **M3**: `./.venv/bin/itemeval grade configs/usamo_demo.yaml --yes`
  → `gradings.parquet` with 24 rows, `grade_kind="judge"`, non-null `score`,
  `reasoning`, token counts and `usd` populated, `parse_ok` all True.
- **M4**: `./.venv/bin/itemeval export configs/usamo_demo.yaml`
  → `export/gradings_long.parquet` + `.csv` (45 columns, 24 rows),
  `export/ledger.csv`; output ends `reconciled: yes`.
- **M5**: `./.venv/bin/itemeval estimate configs/usamo_demo.yaml` → per-stage
  nonzero USD table, no prompts, exit 0. Gate:
  `./.venv/bin/itemeval generate configs/usamo_demo_gate.yaml < /dev/null`
  → prints confirmation-required reason, **exit 3**; with `--yes` it proceeds.
- **M6**: from a clean checkout (`rm -rf studies/usamo_demo`):
  `status` → `generate --yes` → `grade --yes` → `export` → `status`
  (CLI only) ends with all conditions 24/24 complete and spend totals printed;
  `tests/test_integration_pipeline.py` automates exactly this.

---

## 17. Open risks & explicit non-goals

1. **Effective-params extraction** relies on `ModelEvent.config` being present
   in sample events; if a future inspect version thins events, extraction
   falls back to requested values (never crashes). Pin discipline per
   DEVELOPMENT.md mitigates.
2. **Batch discount on actuals is an approximation** (flat 0.5 on
   `BATCH_PROVIDERS`); provider invoices are authoritative. Ledger records the
   `batch` flag so rows can be re-priced.
3. **inspect cache + mockllm**: cached mock outputs bypass `custom_outputs`
   callables; the autouse `INSPECT_CACHE_DIR` tmpdir fixture keeps tests
   hermetic. Real studies *want* this behavior (free re-runs).
4. **Pandas nullable-int round-trip**: in-memory upcasts to float64 are
   expected; `pa.Table.from_pandas(..., schema=...)` restores int64 on disk.
   Covered by U10 round-trip tests; any unsafe cast raises `StoreError`.
5. **Single-process assumption**: no file locks on parquet stores; concurrent
   CLI invocations against one study dir are unsupported (documented).
6. **HF first-run network dependency**: revision resolution needs the Hub
   once; `dataset_locks.json` + HF local cache make subsequent runs offline.
7. **README-sketch graders** (`judge_a` without a `graders:` entry) validate
   at load but fail with a clear `ConfigError` at expansion — intended
   behavior, covered by tests.
8. **Memory**: stage runners hold full `EvalLog`s in memory per condition;
   fine at pilot scale. Post-0.1: stream via `read_eval_log_samples`.
9. **Estimator accuracy** depends on the chars/4 heuristic and max_tokens
   fill; target is the M5 "within ~2×" bound, validated on the consuming
   study's pilot, not in unit tests.
10. **Non-goals for v0.1**: partial/nested crossing, GitHub/local adapters,
    grader replication, wide pivots, multimodal items (ROADMAP "Later").
