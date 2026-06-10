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

## Later (post-0.1)

- GitHub repo adapter; local jsonl adapter
- Partial / nested crossing designs (items-in-tests as first-class)
- Wide-pivot export helpers
- Grader replication (judge as replicated facet)
- Pricing auto-refresh; per-provider spend tracking
- Multimodal items
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
