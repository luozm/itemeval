# Architecture: what each module does and why it exists

itemeval is ~2,950 effective lines across 28 modules — a few percent of the
size of inspect_ai, which it deliberately does **not** duplicate. "Thin"
here means: inspect_ai keeps the hard runtime problems (async execution,
~20 providers, rate limiting, retries, response/prompt caching, batch APIs,
`.eval` transcripts); itemeval only adds the experiment-design layer that
inspect_ai explicitly does not have. Every module below traces to one of the
five README features.

## Naming convention

Every internal module is `_`-prefixed (PEP 8 convention for non-public API,
mandated by this repo's conventions). Python has no enforced visibility; the
underscore is the contract: **only** `itemeval.__init__` (4 names) and the
CLI are public, so everything else can be refactored freely pre-1.0 without
breaking users. `cli.py` has no underscore because it is the console-script
entry point declared in `pyproject.toml`.

## Module map

Sizes are total lines (incl. docstrings). ◆ = candidate for future
consolidation — kept separate for clarity/test-ownership, not necessity.

### Foundation (leaf modules, ~100 lines)

| Module | Lines | Why it exists |
|---|---|---|
| `_errors.py` | 25 | One exception hierarchy → deterministic CLI exit codes (2 vs 1). |
| `_util.py` | 46 | Canonical JSON + sha256 (condition ids, manifests), atomic writes, the token heuristic. Used by nearly every module. |
| `_item.py` ◆ | 29 | The canonical `Item` model — the interface between adapters and both stages. Could live in `_config.py`; separate because it's exported. |

### Feature 1 — benchmark adapters (`adapters/`, ~180 lines)

inspect_ai's `hf_dataset()` loads rows into `Sample`s for one eval; it has no
revision-pinning policy, no lock file, no `grading_scheme`/metadata mapping
contract, no cross-dataset id-uniqueness check.

| Module | Lines | Why |
|---|---|---|
| `_base.py` | 98 | Adapter protocol + registry (ROADMAP post-0.1: github/local adapters), `dataset_locks.json` ("revision pinned at first run"), multi-dataset orchestration. |
| `_hf.py` | 85 | The one concrete adapter: `datasets.load_dataset(revision=...)` + the exact column→Item mapping rules. |

### Feature 2 — design grids (`design/`, ~200 lines)

inspect_ai has tasks, not experiment designs. Nothing in it represents "3
models × 2 prompts × 2 model-configs, fully crossed, with stable cell ids".

| Module | Lines | Why |
|---|---|---|
| `_ids.py` ◆ | 25 | Slug + content-hash id algorithm (tiny, but the stability contract deserves its own tested unit). |
| `_grid.py` | 178 | Facet crossing → `GenCondition`/`GradeCondition` lists; param resolution (facet over solver defaults); template placeholder validation. |

### Feature 3 — the two-stage pipeline (`generate/` + `grade/` + glue, ~1,070 lines)

This is the package's reason to exist. inspect's model-graded scorers run
*inside* the generating eval: judge calls would share the solver's logs, get
no separate batching/caching/cost accounting, and adding a rubric later would
re-run generation. Decoupling requires exactly what these modules do.

| Module | Lines | Why |
|---|---|---|
| `generate/_task.py` | 61 | items + condition → inspect `Task` (samples, epochs, GenerateConfig, cache policy). |
| `generate/_params.py` ◆ | 50 | Requested-vs-effective sampling params from model events (ROADMAP M2 checkbox: provider-forced values must be visible). |
| `generate/_run.py` | 370 | The stage orchestrator: resume computation, serial `eval()` calls, log→rows extraction, usage→USD, ledger/log-index writes, error containment. Biggest module because it owns the inspect↔store boundary; also exports helpers `grade/_run` reuses. |
| `grade/_verifiable.py` | 84 | exact/MC/numeric scorers as pure $0 functions over stored text (inspect scorers want a live `TaskState`, not a parquet row). |
| `grade/_parse.py` | 94 | The strict judge-output contract with exact failure codes; "flagged, never dropped" lives here. |
| `grade/_judge.py` | 93 | Stored solutions + rubric → a fresh inspect `Task` (judge-as-task), format suffix, prompt-cache hint. |
| `grade/_run.py` | 304 | Grade orchestrator: pending computation, verifiable vs judge dispatch, grader×rubric filters, solutions store never written. |
| `_mockmodels.py` | 68 | `mockllm/*` pass-through: deterministic outputs + fabricated usage so demos/tests/CI run the entire pipeline for $0. Dev affordance — the only module a user never needs. |

### Feature 4 — item-response store & export (`store/`, ~700 lines)

inspect logs are per-eval `.eval` files. Cross-run accumulation, keyed
upserts, resume predicates, and the long-format join do not exist there.

| Module | Lines | Why |
|---|---|---|
| `_base.py` | 59 | The one upsert engine: concat → dedup-on-key → schema-cast → atomic replace. |
| `_layout.py` ◆ | 47 | Single source of truth for every path in a study dir. |
| `_solutions.py` | 70 | Solutions schema (36 cols) + `items_to_run` resume predicate. |
| `_gradings.py` | 80 | Gradings schema (30 cols) + `pending_solutions` (parse-failures final, errors retry). |
| `_items.py` ◆ | 47 | Items snapshot (analysis joins need item text without re-downloading). |
| `_logs.py` ◆ | 38 | Raw-log index: store row ↔ `.eval` transcript audit trail. |
| `_ledger.py` ◆ | 36 | Cost ledger schema. |
| `_export.py` | 183 | The 45-column long-table join + CSV mirrors + internal reconciliation. |

The five small schema modules could be one file; they are split so each
table's schema+predicates are independently owned and tested.

### Feature 5 — budget layer (`budget/`, ~430 lines)

inspect_ai has no notion of dollars at all — no pricing, estimation, or
spending gates.

| Module | Lines | Why |
|---|---|---|
| `_pricing.py` | 131 | Pricing table model, packaged seed, OpenRouter refresh, model→price lookup, token→USD math. |
| `_policies.py` ◆ | 40 | dev / full-interactive / full-batch → effective plan (items limit, replications, batch flag). |
| `_estimator.py` | 187 | Per-stage projection: heuristic tokens × grid × prices; uses stored solutions for judge sizing. |
| `_gate.py` | 69 | confirm_above_usd / max_usd / --yes / interactive decision table → exit codes 3/4. |

### Orchestration & UX (~770 lines)

| Module | Lines | Why |
|---|---|---|
| `_config.py` | 209 | The YAML contract: every config model, strict validation, `load_config`. Shared by everything; designed first. |
| `_templates.py` | 79 | Content-hashed prompt/rubric registry + brace-safe rendering (`str.format` would explode on LaTeX/JSON). |
| `_manifest.py` | 183 | The README reproducibility promise as a pydantic schema + writer + post-run effective-params backfill. |
| `_prepare.py` | 83 | `prepare_study()`: config → datasets+templates+grid+plan+pricing, computed once, shared by all five commands. |
| `_status.py` | 153 | Completion matrix: expected vs done vs errors vs parse-failures per condition (M1/M6 exit). |
| `cli.py` | 325 | argparse wiring, gate enforcement, output formatting, exit-code mapping. |

## Could it be fewer files?

Yes — merging the ◆ modules would give ~14 files with identical behavior; the
split optimizes for one-concern-per-file and per-module tests, not because
each file is architecturally load-bearing. What you should **not** expect to
shrink is the behavior itself: each module maps to a checked ROADMAP box or a
README promise, and the total is still under 3k lines on top of a >100k-line
runtime.
