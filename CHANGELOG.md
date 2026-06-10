# Changelog

All notable changes to itemeval are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com); versioning follows
[SemVer](https://semver.org) (pre-1.0: minor bumps may break APIs).

## [Unreleased]

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
- Minimum Python is now 3.11 (was 3.10). The tested dependency stack resolves
  pandas 3.x, which requires Python >=3.11, so 3.10 could only ever install a
  different (pandas 2.x) stack that was never tested. Floor now matches the
  tested stack; `uv.lock` reconciled to a single resolution (dropped the
  3.10-only `exceptiongroup`/`tomli`/`async-timeout`/`pytz` backports).
