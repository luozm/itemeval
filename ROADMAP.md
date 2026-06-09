# itemeval Roadmap

Each milestone is independently testable and ends with a working vertical
slice. The USAMO G-theory pilot (in the separate `g_theory` study repo) is the
acceptance test for M1–M6.

## M0 — Scaffold ✅ (2026-06-09)

src layout, pyproject + uv, skeleton subpackages, CI-ready test stub, docs.

## M1 — Core data model & config

- [ ] Canonical `Item` model (id, input, target, grading_scheme, metadata) — pydantic
- [ ] Experiment config schema (benchmark / solvers / facets / budget) with validation
- [ ] HF adapter: dataset id + revision pinning + field mapping → items
- [ ] Run manifest writer (dataset revisions, template hashes, model + sampling params, package versions)
- [ ] Facet grid expansion with stable condition ids (`design/`)
- Exit: `itemeval status <config>` prints the expanded grid and item count, no API calls

## M2 — Generate stage

- [ ] inspect task builder per (model × prompt × model-config) cell; `epochs` = replications
- [ ] Prompt template registry (content-hashed files)
- [ ] Sampling params: request + record effective values (`temp_requested` / `temp_effective`)
- [ ] Thinking/reasoning toggle as model-config facet (Anthropic extended thinking, OpenAI reasoning effort)
- [ ] Solutions store: parquet + raw `.eval` log index; resumable; epoch-aware caching verified
- Exit: 3 cheap models × 2 items × 2 epochs end-to-end → solutions parquet

## M3 — Grade stage

- [ ] Verifiable scorers: exact match, multiple choice, numeric (no LLM cost)
- [ ] Judge-as-task: grading dataset built from stored solutions; grader model + rubric template; temperature 0
- [ ] Structured score output (numeric + reasoning) with strict parsing; parse failures flagged, never dropped
- [ ] Re-runnable per (grader × rubric) without touching the solutions store
- Exit: pilot solutions graded by 1 judge × 1 rubric → gradings parquet with judge reasoning + costs

## M4 — Export

- [ ] Long-format gradings table (one row per grading event; full schema incl. tokens, USD, latency)
- [ ] Cost ledger per run, attributed generation vs grading
- [ ] CSV mirror of parquet exports
- Exit: export matches the documented schema; ledger totals reconcile with provider dashboards

## M5 — Budget layer

- [ ] Pricing table (per-model $/Mtok, refreshable; OpenRouter pricing API as a source)
- [ ] `estimate`: dry-run projection per stage from token estimates × grid size
- [ ] `confirm_above_usd` gate; `dev` / `full-interactive` / `full-batch` policies
- [ ] Batch-API mode wiring (OpenAI/Anthropic/Google/xAI/Together via inspect)
- Exit: estimate-before-run enforced in CLI; projection within ~2× of actuals on the pilot

## M6 — CLI polish

- [ ] `estimate | generate | grade | export | status` complete with consistent UX
- [ ] Resumability + grid-completion reporting across interrupted runs
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
