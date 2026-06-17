# itemeval Roadmap

Direction and near-term plan. What *shipped* lives in
[CHANGELOG.md](CHANGELOG.md); candidate features with design notes live in
[docs/BACKLOG.md](docs/BACKLOG.md). This file is curated by hand — it is the
big picture, not a feature ledger.

## Vision

itemeval turns an LLM benchmark into a measurement instrument: one row per
grading event (item × model × prompt × replication × grader × rubric), never
just an aggregate score. Two commitments shape every feature:

- **Never be surprised** — no silent side effects, a dry-run cost before any
  spend, and a hard dollar cap that can't be talked past
  (see [docs/UX-PATTERNS.md](docs/UX-PATTERNS.md)).
- **A façade over inspect_ai, not a fork** — wrap its execution engine, pass
  its knobs through unchanged, flatten to our own schema at the boundary
  (see [DEVELOPMENT.md](DEVELOPMENT.md)).

We build along three arcs:

- **Adoption on-ramps** — meet users where their data already is.
- **Measurement depth** — the analyses our audience can't get elsewhere.
- **Scale & breadth** — bigger studies, more modalities.

## Release plan

Detail decays with distance: the next release names specific features (by
[BACKLOG.md](docs/BACKLOG.md) key) and exit criteria; later releases stay at
theme level until scheduled.

### 0.3 — Adoption (next)

**Goal.** A new user runs the full pipeline on their own data with no
HuggingFace upload.

**Includes.** `local-adapter` · `github-adapter` · `item-sampling` ·
`scorer-plugins`

**Already landed** (in `[Unreleased]`, ships with 0.3): `model-sampling`.

**Exit criteria.** The quickstart runs from a local JSONL end-to-end; a GitHub
repo dataset loads pinned to a commit; subset sampling is recorded in the
manifest and exactly reproducible; a user scorer loads by import path and
hashes into condition ids.

### 0.4 — Measurement depth (themed)

The reliability/agreement report (`report-command`), judge-as-replicated-facet
(`judge-replication`), and human-vs-judge ratings (`human-ratings`). Exact
contents firmed up when 0.3 lands.

### Later (vision-level)

Scale & breadth (`multimodal-items`, `midcell-resume`, `reuse-savings`) and ops
(`pypi-approval-gate`). See [docs/BACKLOG.md](docs/BACKLOG.md); a feature is
promoted here with a goal + exit criteria when scheduled.

## History

- **0.2.0** (2026-06-12) — cost & provider caching, honest/delta-aware
  accounting, reproducibility (waves, snapshots, drift), full agent surface.
- **0.1.0** (2026-06-10) — first public release: M0–M7, the two-stage
  generate/grade pipeline, long-format export, budget layer, PyPI.

Per-change detail is in [CHANGELOG.md](CHANGELOG.md).
