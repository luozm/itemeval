# itemeval Roadmap

Each milestone is independently testable and ends with a working vertical
slice. The USAMO G-theory pilot (in the separate `g_theory` study repo) is the
acceptance test for M1–M6.

M1–M6 exits are demonstrated end-to-end on free `mockllm/*` models
(`configs/usamo_demo.yaml`; zero paid API calls). The paid parts of the
M4–M6 exits — provider-dashboard reconciliation, estimate-vs-actuals accuracy,
and the USAMO pilot itself — remain to be confirmed on the consuming study's
first real run.

## M0 — Scaffold ✅ (2026-06-09)

src layout, pyproject + uv, skeleton subpackages, CI-ready test stub, docs.

## M1 — Core data model & config ✅ (2026-06-09)

- [x] Canonical `Item` model (id, input, target, grading_scheme, metadata) — pydantic
- [x] Experiment config schema (benchmark / solvers / facets / budget) with validation
- [x] HF adapter: dataset id + revision pinning + field mapping → items
- [x] Run manifest writer (dataset revisions, template hashes, model + sampling params, package versions)
- [x] Facet grid expansion with stable condition ids (`design/`)
- Exit: `itemeval status <config>` prints the expanded grid and item count, no API calls

## M2 — Generate stage ✅ (2026-06-09)

- [x] inspect task builder per (model × prompt × model-config) cell; `epochs` = replications
- [x] Prompt template registry (content-hashed files)
- [x] Sampling params: request + record effective values (`temp_requested` / `temp_effective`)
- [x] Thinking/reasoning toggle as model-config facet (Anthropic extended thinking, OpenAI reasoning effort)
- [x] Solutions store: parquet + raw `.eval` log index; resumable; epoch-aware caching verified
- Exit: 3 cheap models × 2 items × 2 epochs end-to-end → solutions parquet

## M3 — Grade stage ✅ (2026-06-09)

- [x] Verifiable scorers: exact match, multiple choice, numeric (no LLM cost)
- [x] Judge-as-task: grading dataset built from stored solutions; grader model + rubric template; temperature 0
- [x] Structured score output (numeric + reasoning) with strict parsing; parse failures flagged, never dropped
- [x] Re-runnable per (grader × rubric) without touching the solutions store
- Exit: pilot solutions graded by 1 judge × 1 rubric → gradings parquet with judge reasoning + costs

## M4 — Export ✅ (2026-06-09)

- [x] Long-format gradings table (one row per grading event; full schema incl. tokens, USD, latency)
- [x] Cost ledger per run, attributed generation vs grading
- [x] CSV mirror of parquet exports
- Exit: export matches the documented schema; ledger totals reconcile with provider dashboards

## M5 — Budget layer ✅ (2026-06-09)

- [x] Pricing table (per-model $/Mtok, refreshable; OpenRouter pricing API as a source)
- [x] `estimate`: dry-run projection per stage from token estimates × grid size
- [x] `confirm_above_usd` gate; `dev` / `full-interactive` / `full-batch` policies
- [x] Batch-API mode wiring (OpenAI/Anthropic/Google/xAI/Together via inspect)
- Exit: estimate-before-run enforced in CLI; projection within ~2× of actuals on the pilot

## M6 — CLI polish ✅ (2026-06-09)

- [x] `estimate | generate | grade | export | status` complete with consistent UX
- [x] Resumability + grid-completion reporting across interrupted runs
- Exit: USAMO pilot pass criteria all green, driven only through the CLI

## M7 — Publish v0.1.0

- [x] Pip/uv install path: built-in template library (`builtin:` refs) +
  `itemeval init` scaffold; outputs anchored to the working directory, inputs to
  the config dir
- [x] Test coverage for adapters, grid expansion, parsing, export schema
  (96%→97% line coverage; the named areas at 100% bar export's defensive guards)
- [x] GitHub Actions CI (ruff + pytest, py3.11–3.13 matrix)
- [x] README quickstart against a public verifiable benchmark (no judge needed) —
  AIME 2025 + `numeric` scorer; validated live on real generate output
- [x] CHANGELOG.md; tag v0.1.0 — publish to PyPI (`uv build && uv publish`) is
  the final manual step

## Later (post-0.1)

- GitHub repo adapter; local jsonl adapter
- Partial / nested crossing designs (items-in-tests as first-class)
- Wide-pivot export helpers
- Grader replication (judge as replicated facet)
- Pricing auto-refresh; per-provider spend tracking
- Multimodal items
- Progress display on by default for `generate`/`grade` — pick a sensible
  `--display` default (instead of `none`) and surface live progress through the
  Python API too, not just the CLI
- Finer-grained resume — per-sample mid-cell checkpointing so a large cell that
  dies near the end doesn't restart from zero (cell-level resume already exists
  via the parquet + `.eval` store; lean on inspect's `eval_retry`/`.eval` logs).
  An explicit pause/break command is deliberately *not* planned — Ctrl-C + re-run
  already covers it
- Combine multiple runs on export — pool a small pilot with a larger follow-up
  run under the same setting into one export. Must verify the runs share a
  compatible grid/manifest (models, prompts, rubrics, dataset revisions) and
  refuse to merge incompatible runs rather than silently pooling them
- PyPI publish approval gate — optionally add a GitHub `pypi` Environment with a
  required-reviewer rule and reference it from `release.yml` (`environment: pypi`,
  plus the matching Environment field on the PyPI trusted publisher) so a release
  requires manual approval before the OIDC upload runs. Today `release.yml` has no
  environment gate: publishing a GitHub Release uploads to PyPI immediately.
