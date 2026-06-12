# Changelog

All notable changes to itemeval are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com); versioning follows
[SemVer](https://semver.org) (pre-1.0: minor bumps may break APIs).

## [Unreleased]

### Added
- `--policy {dev,full-interactive,full-batch}` on
  `estimate`/`generate`/`grade`/`status`: override `budget.policy` for one
  invocation without editing the config — the zero-edit pilot flow
  (`generate cfg.yaml --policy dev`, inspect, `generate cfg.yaml`). Python
  parity: `prepare_study(cfg, policy=...)`. The run manifest and the
  estimate/status JSON record the effective policy and its source
  (`policy_source: "config" | "override"`, append-only).
- `pilot-available` hint: when a first paid run (no completed rows for the
  selected conditions) hits the money gate, one stderr hint points at the
  `--policy dev` pilot flow; under `--json` it rides the `hints` array,
  including in the gate-stop document.

### Added
- **Hint framework** (`docs/UX-PATTERNS.md`): commands may end with up to two
  dim `hint:` lines on stderr — one observed fact from this run plus a wiki
  pointer; hints never change behavior and never block. `ITEMEVAL_HINTS=off`
  silences the text rendering; in `--json` the full list always rides as a
  `hints` array on the result (`Estimate`, `GenerateResult`, `GradeResult`,
  `ExportResult`). Initial coded hints (stable, append-only):
  `cache-zero-reads` (same-prefix calls scheduled but no provider cache
  discount engaged), `empty-solutions` (completions with no API error and no
  gradable text), `unpriced-models` (replaces the inline `unpriced models:`
  lines on estimate/export).

### Changed
- The `grade` empty-solutions summary line is now fact-only
  (`empty solutions: 21 excluded from grading [model_length×21] —
  on_empty=skip`); the remediation advice moved to the wiki
  (Error-Handling#empty-completions), pointed to by the `empty-solutions`
  hint.

### Added
- `--json` on `generate` and `grade` (every command now has it): stdout
  carries exactly one JSON document — the run result extended with `pricing`,
  `estimate_usd`, and a `gate` outcome object — and inspect's live display is
  silenced unless `--display` is passed explicitly. A gate stop under
  `--json` still emits a JSON document (projected cost, gate reason, rerun
  command, `hints`) before exiting 3/4, so an agent gets structure even on a
  stop. New JSON keys are append-only; exit codes unchanged.
- **Cache-aware execution scheduling** (docs/FUTURE.md §1.6, validated in a
  live pilot): maximize provider prompt-cache discounts (~75–90% off repeated
  input tokens).
  - Cache observability: `generate`/`grade` per-condition summaries report
    provider cache reads/writes and hit rate; `ConditionRunReport` gains
    `cache_read_tokens` / `cache_write_tokens` / `cache_hit_rows`.
  - `graders.<name>.split_rubric`: render the rubric as a system message
    (shared head: rubric + problem + scheme + reference) plus a user message
    (the solution), placing the provider cache breakpoint exactly at the
    shared/varying boundary. In the validation pilot this **halved the judge
    bill** on an Anthropic judge via OpenRouter (78% input-side discount;
    the monolithic layout cached nothing). Changes grade condition ids when
    enabled.
  - `solvers.split_prompt`: the analogous split for solver prompts at
    `{input}` (static template head → system message). Required for
    Anthropic-style caching of generate calls through OpenRouter; 66–78%
    input-side discount on replications in the pilot.
  - `solvers.cache_prompt` (`auto`/`on`/`off`, default `auto` = on when
    replications > 1): provider prompt caching for the generate stage.
  - `budget.cache_schedule` (`auto`/`off`): warm-then-fan-out gating of
    same-prefix call groups (leader writes the cache, followers read). Also
    routes byte-identical duplicate judge calls into inspect's local response
    cache ($0). Judge datasets are now sorted by item so same-prefix calls
    are adjacent.
  - Pricing: cache write defaults to $0 for non-Anthropic-style models
    (OpenAI/Gemini/DeepSeek writes are free; Anthropic keeps the 1.25×
    surcharge); `--refresh-pricing` now also pulls per-model cache read/write
    rates from OpenRouter.

### Documentation
- New wiki page **Cost Savings**: every saving option in plain language with
  measured price/time trade-offs, defaults, and direct-API-vs-OpenRouter
  guidance; developer-depth counterpart in `docs/COST-OPTIMIZATION.md`.
- Five step-by-step tutorials in the wiki, each a complete runnable use case:
  score a verifiable benchmark (~2¢), grade with an LLM judge, compare models ×
  prompts with replications (+ pandas/mixed-model analysis), add a second
  judge/rubric at $0 generation, and scale up under the budget layer.
- New wiki **Agent Guide**: a contract-style page for driving itemeval from an
  AI agent — command/exit-code contract, hard budget guardrails, standard
  operating procedure, failure-triage table, and a drop-in block for a study
  repo's `CLAUDE.md`/`AGENTS.md`.
- README rewritten value-first: leads with what the data looks like, adds a
  "Who is this for" section and a documentation hub linking the tutorials and
  agent guide.
- `docs/FUTURE.md`: the post-0.1 feature backlog with per-feature design notes
  (motivation, sketch, implementation plan); ROADMAP's "Later" section is now a
  tiered summary pointing at it.
- `docs/UX-PATTERNS.md`: the binding UX contract for development — two
  operators (human/agent), eight laws (no silent side effects, advice never
  acts, native consent, …), the hint framework, a normative side-effect
  ledger, and a nine-question per-feature checklist. Referenced from
  CLAUDE.md, DEVELOPMENT.md, and FUTURE.md.

### Added
- Per-run savings report: `export` now reports spend against a plain-API list
  price (every input token at full rate, no batch discount) and breaks the
  savings into a prompt-cache component and a batch-discount component, plus a
  per-provider spend table. Exposed on `ExportResult.cost` (a `CostReport`).
  Local response-cache / resume reuse is not represented (cache hits carry no
  token usage), so the figure covers the prompt-cache and batch discounts only.
- Pricing auto-refresh: `budget.pricing_max_age_days` (default `None` = off)
  refreshes the cached OpenRouter pricing table when it is at least that many
  days old. Best-effort — network/parse failures keep the existing table and
  never break a run; ignored when `budget.pricing_path` pins an explicit table.
- Pricing provenance: `estimate`, `generate`, `grade`, `export`, and `status`
  print which pricing table the dollar figures came from (`source`, age, and
  whether a refresh just ran). Exposed programmatically on `Estimate.pricing`
  and `ExportResult.pricing` (a `PricingProvenance`) and on
  `PreparedStudy.pricing_refreshed`.

### Changed
- Live progress display is now on by default for `generate` and `grade`. The
  `display` argument of `run_generate`/`run_grade` and the CLI `--display` flag
  now default to inspect's `rich` live progress (inline bars; honoring
  `INSPECT_DISPLAY` and degrading off-TTY/Jupyter/background-thread) instead of
  `none`; progress is surfaced through the Python API as well as the CLI. Pass
  `display="none"` (API) or `--display none` (CLI), or set `INSPECT_DISPLAY=none`,
  to silence it.

## [0.1.0] - 2026-06-10

First public release. Item-level LLM evaluation over any inspect_ai-supported
provider, with a two-stage generate/grade pipeline, long-format item-response
export, and a budget layer.

### Added
- Core data model and config (M1): canonical `Item` model; full pydantic
  experiment-config schema validating the README YAML sketch as-is
  (`load_config`); content-derived stable condition ids; facet grid expansion
  with full crossing.
- HuggingFace benchmark adapter (M1): field-mapping spec → canonical items,
  revision pinned at first run via a per-study `dataset_locks.json`.
- Run manifests (M1): dataset revisions, template content hashes, model ids,
  requested sampling params (effective values backfilled per condition after
  each run), package versions, full condition grid — one JSON per run.
- Generate stage (M2): one inspect task per (model × prompt × model-config)
  cell, `epochs` = replications, thinking/reasoning toggles as model-config
  facets, requested vs effective sampling params recorded per row, resumable
  solutions parquet store + raw `.eval` log index.
- Grade stage (M3): verifiable scorers (exact match / multiple choice /
  numeric, $0) and judge-as-task (grading dataset built from stored solutions,
  judge temperature pinned to 0, prompt caching enabled); strict structured
  score parsing with parse failures flagged in-table, never dropped;
  re-runnable per (grader × rubric) without touching solutions.
- Export (M4): long-format gradings table (45 columns: scores, judge
  reasoning, tokens, USD, latency, full provenance), parquet + CSV mirrors,
  per-run cost ledger attributed generation vs grading with internal
  reconciliation check.
- Budget layer (M5): packaged pricing seed + OpenRouter pricing refresh,
  per-stage dry-run estimator, `confirm_above_usd` gate (exit 3) and
  non-overridable `max_usd` cap (exit 4), `dev`/`full-interactive`/
  `full-batch` policies, batch-API wiring with documented ~50% discount
  approximation.
- CLI (M6): `estimate | generate | grade | export | status` with consistent
  UX, `--json` output, repeatable `--condition/--grader/--rubric` filters,
  resumability and grid-completion reporting.
- `mockllm/*` pass-through: any mock model id runs the full pipeline free and
  deterministically (used by all demos and tests; `configs/usamo_demo.yaml`).
- Public Python API: the pipeline is drivable programmatically as well as via
  the CLI — `prepare_study`, `estimate_study`, `run_generate`, `run_grade`,
  `export_study`, `build_status` exported from `itemeval` (lazily, so
  `import itemeval` stays light). The budget confirmation gate remains a
  CLI-layer feature.
- Dependency: `datasets` (HuggingFace) for the HF adapter.
- Built-in template library: prompts `minimal`/`standard` and rubric `standard`
  ship inside the package and are referenced as `builtin:<name>`. A bare name
  still resolves to a local file under `prompts_dir`/`rubrics_dir`; the two
  namespaces are distinct and never silently shadow each other — each template
  is recorded in the run manifest with its `source` (`local`/`builtin`) and
  content hash, and built-in templates record a machine-independent path.
- `itemeval init DIR [--with-templates] [--force]`: scaffold a runnable starter
  study (`config.yaml`). `--with-templates` also copies the referenced built-in
  prompts/rubrics locally as editable starters. Makes `pip install itemeval`
  usable without cloning the repo.
- `solvers.on_empty` policy (`skip` default / `rerun` / `grade`) for completed
  generations that produced no gradable text (empty/blank `solution`, no API
  error — e.g. a reasoning model whose token budget was spent entirely on
  hidden reasoning). Empty no-error completions are a distinct channel from API
  errors (re-attempted) and parse failures (final): `skip` excludes them from
  grading, `rerun` also makes them eligible for regeneration on the next
  `generate`, `grade` sends them to the judge as-is. They are always surfaced —
  `grade` reports the count and stop-reason breakdown, and `status` gains an
  `empty` column — never silently folded into a green "complete".
- Provider/endpoint provenance for cost attribution: `ledger.parquet` gains a
  `provider` column (the inspect prefix of `model`), and run manifests gain
  `endpoints_effective` per condition (`{provider, base_url, served_model}`,
  backfilled after the run) — recording which provider, endpoint, and
  provider-returned model snapshot actually answered. `base_url` is null on the
  provider's default endpoint; a non-null value flags traffic routed elsewhere
  (Azure/proxy/gateway).

### Changed
- **Path resolution split by intent** (behavior change). Inputs (`prompts_dir`,
  `rubrics_dir`, `budget.pricing_path`) still anchor to the config file's
  directory; outputs (`output_dir`, i.e. the study tree) now anchor to a **work
  directory** defaulting to the current directory, never the config dir or the
  installed package. New `-C/--base-dir` (CLI) and `load_config(work_dir=...)`
  (Python) override the output anchor. The example configs drop their `../`
  prefixes accordingly.
- Default `facets.prompt` / `facets.rubric` are now `[builtin:standard]`
  (were `[default]`, which referenced a template that never existed).
- Template references and validation moved ahead of study-directory creation:
  an unresolved template now fails before any output directory is written.

### Packaging
- Provider-SDK optional extras (`openai`, `anthropic`, `google`, `all`),
  mirroring inspect_ai's lazy provider imports. Install the extra for the
  provider you run, e.g. `pip install itemeval[openai]` — the `openai` extra
  also covers OpenRouter and other OpenAI-compatible providers. The base
  install stays SDK-free; running a real provider without its extra raises
  inspect_ai's `PrerequisiteError` with the install hint.
- Ship a `py.typed` marker (PEP 561): downstream type checkers now see
  itemeval's annotations. Added the `Typing :: Typed` and Python 3.11/3.12
  classifiers.
- Relaxed the `pyarrow` (`>=24` → `>=15`) and `datasets` (`>=5` → `>=3`)
  lower bounds to the oldest versions whose APIs we actually use, easing
  co-installation; dev/CI still pin the latest via `uv.lock`. The full test
  suite passes at both the floor and the locked versions.
- Expanded `[project.urls]` (Homepage, Documentation → wiki, Changelog, Issues)
  and switched the README's PyPI-facing links to absolute GitHub URLs.
- Minimum Python is now 3.11 (was 3.10). The tested dependency stack resolves
  pandas 3.x, which requires Python >=3.11, so 3.10 could only ever install a
  different (pandas 2.x) stack that was never tested. Floor now matches the
  tested stack; `uv.lock` reconciled to a single resolution (dropped the
  3.10-only `exceptiongroup`/`tomli`/`async-timeout`/`pytz` backports).

[Unreleased]: https://github.com/luozm/itemeval/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/luozm/itemeval/releases/tag/v0.1.0
