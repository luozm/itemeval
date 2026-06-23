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

**Includes.** `github-adapter` · `item-sampling` · `scorer-plugins`

**Already landed / in flight** (in `[Unreleased]`, ships with 0.3):
`model-sampling` · `composite-item-id`; and `model-sample-composition` (recency,
equal allocation, pinned include) building directly on `model-sampling`; plus
`expected-cost` — a calibrated expected-cost projection alongside the estimate
ceiling (the gate still uses the ceiling); and `native-batch-routing` — route
OpenRouter-sampled models to their native API under batch to capture the ~50%
discount, with a per-model native-batch-vs-OpenRouter-cache comparison at
estimate time (pulled forward from "Later"); and `sample-exclude` — a top-level
`exclude` id blocklist (the inverse of `include`, valid on any universe) that
also makes the `pricing-table` roster non-free by default, retiring the need for
a `where.free` filter; `rubric-materialization` — two-stage generate-then-grade
rubrics (a materializer LLM freezes a per-item rubric from the reference solution,
reused verbatim by every judge call), folded into `grade` under the single money
gate (pulled forward from 0.4); and `parallel-conditions` — a stage runs its
conditions concurrently in one eval (was one model at a time), plus a coarse
pre-flight wall-clock ETA and a cost-lever status line; and
`sample-output-modality` — an opt-in `where.output_text_only` that drops
image/audio generators (which pass the runnable-text gate by also emitting text)
from a sampled `pricing-table` roster; `recoverable-harvest` — a crashed run's
`.eval` is projected back into the stores (`itemeval harvest`, plus auto-harvest on
read/resume), so a hard-killed flaky run is no longer invisible to
`status`/`export` and resume never re-pays the recovered cells (the root of the
run-visibility cluster); and `recovery-run-identity` — run identity is now
experiment-scoped (`experiment_id` + `attempt` replace `run_id`, derived from a
semantic config digest), so a recovery re-run of an unchanged config **converges**
its provenance the way data already does instead of forking, with a per-experiment
attempt rollup, a recovery-vs-new announcement, and `--new-run` to fork
deliberately (a non-additive store rename — ships a clean-break `Study migration`);
and `preflight-check` — an `itemeval preflight` command that probes each distinct
model with a ~1-token call and reports roster health before a paid run, shipping
the reusable terminal-vs-transient error classifier that `request-timeout`'s
deferred "don't retry a terminal timeout" refinement will consume; and
`truncation-signal` — a `truncated` status channel + export column + hint that
flags non-empty length-cap (`max_tokens`/`model_length`) completions, so a budget
cut is no longer silently scored as a content failure; and `cache-projection` — a
pre-flight `cache: N cached / M fresh → ~$X real` line that probes inspect's local
response cache before the gate (reusing inspect's own `CacheEntry`), so a recovery
/ `--force` / replication re-run's true cost is visible up front (the gate still
compares the ceiling); and `live-tracker` — a live stderr heartbeat (counts,
throughput-based ETA, errors, in-flight) during a `generate`/`grade` run whose
display is silenced, plus the pre-flight ETA echoed to stderr under `--json`,
closing the "`--json` goes dark" gap so a long or backgrounded paid run shows it is
alive (liveness rides stderr; stdout stays pure JSON), extended by
`straggler-heartbeat` — a wall-clock timer that, when completions stall, names the
slowest in-flight cells (`model · item · elapsed`, with `try N` when retrying) so a
hung cell is visible instead of a frozen line; and `oversized-solution-skip`
— a per-grader `max_solution_chars` knob that auto-scores 0 (without a judge call)
any stored solution whose visible text exceeds the threshold, so a weak model's
repetition-loop output is no longer paid to the judge to grade as the zero it is;
and `provider-finish-capture` — raw `served_provider` + `native_finish_reason`
columns on the solver and judge calls (and the export), so an OpenRouter soft
failure (HTTP 200 + `finish_reason=error` + empty content) and the backend that
served it are diagnosable straight from the export instead of by hand-reading the
`.eval` (also discharges the unmapped-finish-reason known issue); and
`output-validity-reroute` — an opt-in `solvers.max_reroutes` that automatically
re-issues a soft-failed `generate` cell on a different backend (the failed one
added to `provider:{ignore:[…]}`), capped, replacing the bad row when a good draw
lands and leaving an honest residue when it doesn't — turning the manual
provider-blocklist stopgap into automatic, model-agnostic recovery; and
`local-adapter` — a benchmark loaded from a local `.parquet`/`.json`/`.jsonl`
file pinned by content hash instead of the Hub (the headline 0.3 adoption
blocker, so the quickstart runs from a file on disk with no Hub upload); and
`metadata-in-templates` — every `mapping.metadata` column exposed to rubric and
build templates as `{colname}` (canonical fields win on collision), so a rubric
can read a second per-item grading scheme alongside the built-in
`{grading_scheme}`.

**Exit criteria.** The quickstart runs from a local JSONL end-to-end; a GitHub
repo dataset loads pinned to a commit; subset sampling is recorded in the
manifest and exactly reproducible; a user scorer loads by import path and
hashes into condition ids.

### 0.4 — Measurement depth (themed)

The reliability/agreement report (`report-command`), judge-as-replicated-facet
(`judge-replication`), and human-vs-judge ratings (`human-ratings`).

Also being shaped for 0.4+ — the study-design depth real crossed designs need:
per-model generation config (`per-model-config`), item covariates in the export
(`item-covariates-export`), and capability legibility so agents discover all of
it before they run (`capability-legibility`). Exact contents firmed up when 0.3
lands.

### Later (vision-level)

Scale & breadth (`multimodal-items`, `midcell-resume`, `reuse-savings`)
and ops (`pypi-approval-gate`). See [docs/BACKLOG.md](docs/BACKLOG.md); a feature is
promoted here with a goal + exit criteria when scheduled.

## History

- **0.2.0** (2026-06-12) — cost & provider caching, honest/delta-aware
  accounting, reproducibility (waves, snapshots, drift), full agent surface.
- **0.1.0** (2026-06-10) — first public release: M0–M7, the two-stage
  generate/grade pipeline, long-format export, budget layer, PyPI.

Per-change detail is in [CHANGELOG.md](CHANGELOG.md).
