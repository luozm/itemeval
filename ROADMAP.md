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

- [ ] Test coverage for adapters, grid expansion, parsing, export schema
- [ ] GitHub Actions CI (ruff + pytest, py3.10–3.13 matrix)
- [ ] README quickstart against a public verifiable benchmark (no judge needed)
- [ ] CHANGELOG.md; tag v0.1.0; publish to PyPI (`uv build && uv publish`)

## Later (post-0.1)

- GitHub repo adapter; local jsonl adapter
- Partial / nested crossing designs (items-in-tests as first-class)
- Wide-pivot export helpers
- Grader replication (judge as replicated facet)
- Pricing auto-refresh; per-provider spend tracking
- Multimodal items
