# itemeval Roadmap

Each milestone is independently testable and ends with a working vertical
slice. A downstream study's pilot — in its own separate repo — is the
acceptance test for M1–M6.

M1–M6 exits are demonstrated end-to-end on free `mockllm/*` models (the bundled
demo config; zero paid API calls). The paid parts of the M4–M6 exits —
provider-dashboard reconciliation, estimate-vs-actuals accuracy, and the
downstream pilot itself — remain to be confirmed on the consuming study's first
real run.

## Shipped (0.1.0)

Per-milestone exit criteria and what landed are detailed in CHANGELOG 0.1.0.

- **M0 — Scaffold** ✅ 2026-06-09 — src layout, pyproject + uv, skeleton
  subpackages, CI-ready test stub, docs.
- **M1 — Core data model & config** ✅ 2026-06-09 — canonical `Item` model,
  validated experiment-config schema, HF adapter (revision-pinned), run
  manifests, facet grid expansion with stable condition ids.
- **M2 — Generate** ✅ 2026-06-09 — inspect task per (model × prompt ×
  model-config) cell (`epochs` = replications), content-hashed prompt registry,
  requested vs effective sampling capture, thinking/reasoning facet, resumable
  solutions store (parquet + `.eval` index).
- **M3 — Grade** ✅ 2026-06-09 — verifiable scorers (exact / MC / numeric, $0)
  and judge-as-task (temperature 0), strict structured parsing (failures
  flagged, never dropped), re-runnable per (grader × rubric).
- **M4 — Export** ✅ 2026-06-09 — long-format gradings table (full schema),
  per-run cost ledger (generation vs grading), CSV mirror.
- **M5 — Budget layer** ✅ 2026-06-09 — pricing table, per-stage dry-run
  estimate, `confirm_above_usd` gate, `dev`/`full-interactive`/`full-batch`
  policies, batch-API mode.
- **M6 — CLI polish** ✅ 2026-06-09 — `estimate | generate | grade | export |
  status` with consistent UX, resumability + grid-completion reporting;
  downstream pilot pass criteria green, driven only through the CLI.
- **M7 — Publish v0.1.0** ✅ 2026-06-10 — pip/uv install path, built-in template
  library + `itemeval init` scaffold, test coverage (97% line), CI matrix
  (py3.11–3.13), README quickstart validated live on AIME 2025 (5/5 correct,
  ~$0.014 generation), PyPI trusted publishing (OIDC, no token).

## 0.2 (in progress)

- ✅ 2026-06-10 — Progress display on by default for `generate`/`grade`: the
  `--display` flag and the `run_generate`/`run_grade` `display` argument now
  default to inspect's `rich` live display (honoring `INSPECT_DISPLAY`,
  degrading off-TTY/Jupyter/threads) instead of `none`, surfacing live progress
  through the Python API as well as the CLI; `none` is still available to
  silence it.
- ✅ 2026-06-10 — Pricing auto-refresh + per-provider spend tracking + savings
  report + pricing provenance: `budget.pricing_max_age_days` refreshes the
  cached OpenRouter pricing table when stale (best-effort, opt-in); `export`
  reports per-provider spend and the savings vs a plain-API list price, split
  into prompt-cache and batch-discount components (`ExportResult.cost`); every
  cost-bearing command (CLI + Python) states which pricing table it used
  (`source`/age/refreshed). Resume / response-cache reuse is not yet counted
  (cache hits carry no usage).

## Later (post-0.1)

Design notes — motivation, sketch, implementation plan per feature — live in
[docs/FUTURE.md](docs/FUTURE.md); this is the high-level list. Items graduate
to a versioned section above when scheduled.

**Tier 1 — adoption blockers** (front of the line for 0.2/0.3):

- Local file adapter (jsonl/csv/parquet) — the most-requested on-ramp
- GitHub repo adapter (pinned commit)
- Item subset sampling — random/stratified, seeded, recorded in the manifest
- Custom scorer plugin point + more built-in verifiable scorers (regex,
  normalized exact match)
- Reliability & agreement report (`itemeval report`) — descriptive judge
  agreement / item difficulty / replication consistency over the export table
- Cache-aware execution scheduling — order, group, and stagger calls
  (warm-then-fan-out) so replications and judge fan-outs actually hit provider
  prompt caches (~75–90% off repeated input tokens), plus cache-hit-rate
  reporting and provider-accurate cache pricing

**Tier 2 — measurement depth:**

- Grader replication + judge sampling configs (judge as a replicated facet)
- Import human ratings as a grade condition (human-vs-LLM-judge for free)
- Pairwise / comparative judging (preference pairs over stored solutions)
- Partial / nested crossing designs (items-in-tests as first-class)
- Combine multiple runs on export (pilot + full run; refuse incompatible
  manifests rather than silently pooling)
- Wide-pivot export helpers

**Tier 3 — scale and breadth:**

- Multimodal items
- Finer-grained resume (mid-cell; lean on inspect's `eval_retry`/`.eval` logs;
  an explicit pause/break command is deliberately *not* planned)
- Savings report: count resume / response-cache reuse (needs a join back to
  the original run's tokens)
- Study card generator (shareable Markdown provenance card per study)

**Ops:**

- PyPI publish approval gate — GitHub `pypi` Environment + required reviewer
  referenced from `release.yml`; today a published GitHub Release uploads to
  PyPI immediately.
