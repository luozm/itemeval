# BACKLOG.md — feature backlog with design notes

The single source of truth for candidate features **not yet built**.
[ROADMAP.md](../ROADMAP.md) says *what* is committed for which release and
*why*; this file holds the *why and how* per feature: motivation, design
sketch, implementation notes (which modules change), and open questions.
Nothing here is promised. Every design must comply with
[UX-PATTERNS.md](UX-PATTERNS.md) (the binding UX contract: side-effect
announcements, hint framework, consent rules, knob buckets).

**Each section carries a stable key.** The key
(`**Key:** \`slug\``) is the feature's identity everywhere else — the ROADMAP
commitment, the branch `feat/<slug>`, the plan `docs/plans/<slug>.md`, and the
CHANGELOG `Closes: <slug>` trailer when it ships.

Scheduling — which keys land in which release — lives in [ROADMAP.md](../ROADMAP.md), not here. Whether a feature is actively being
built lives in its plan file (`docs/plans/<slug>.md`: NOT STARTED → IN
PROGRESS → IMPLEMENTED). Being in BACKLOG just means "not yet built."

When a feature ships it **leaves this file** — its design record lives on in
`docs/plans/archive/<slug>.md` and the CHANGELOG entry. A shipped feature is
never a backlog item, so a key here can never also appear in a CHANGELOG
`Closes:` (the consistency check that keeps the two honest).

Tiers reflect adoption value, not effort:

- **Tier 1 — adoption blockers**: things a new user expects on day one.
- **Tier 2 — measurement depth**: features for the core item-response
  audience that competitors don't have.
- **Tier 3 — scale and breadth**: bigger studies, more modalities, polish.

---

## Tier 1 — adoption blockers

### Local file adapter (`adapter: local`) — jsonl/csv/parquet
**Key:** `local-adapter`

**Motivation.** The single most common first question for any eval tool:
"my benchmark is a JSONL file on disk, not on HuggingFace." Today the answer
is "upload it to the Hub", which loses many users at hello. This is the
highest-leverage feature in the backlog.

**Design sketch.** New adapter type in the existing registry:

```yaml
benchmark:
  adapter: local
  datasets:
    - path: data/my_items.jsonl     # .jsonl | .csv | .parquet by extension
  mapping: {id: qid, input: question, target: answer}
```

Reuses the `mapping` spec unchanged. The "revision pinned at first run"
guarantee maps to a **content hash**: `dataset_locks.json` records the file's
sha256; a changed file fails loudly (same spirit as HF revision pinning) until
the user bumps/clears the lock. `path` resolves relative to the config file
(input-path intent rule).

**Implementation notes.** `adapters/_base.py` already defines the protocol +
registry for exactly this; add `adapters/_local.py` (~80 lines, pandas
readers), extend the `benchmark.adapter` literal and per-dataset model in
`_config.py` (`id` → `path` for local), and teach the lock logic hash-vs-
revision. Estimator/manifest unchanged (they consume canonical Items).
Tests are hermetic by construction (tmp files).

**Open questions.** Glob support (`path: data/*.jsonl`)? Probably later; one
file per entry first.

**CI follow-on.** Ship a hermetic end-to-end CLI smoke alongside this:
mock models + a tiny committed JSONL fixture (zero network, deterministic) —
the run the HF adapter can't give CI today (ephemeral runners start with no
HF cache, so the dataset fetch needs network). Chosen over caching a Hub
dataset in CI, which would only be best-effort offline. See the note in
`.github/workflows/ci.yml`.

### GitHub repo adapter (`adapter: github`)
**Key:** `github-adapter`

**Motivation.** Benchmarks that live as files in a repo (competition archives,
org-internal sets) — already promised in the README feature list.

**Design sketch.** `{repo: org/name, ref: <sha|tag>, path: data/items.jsonl}`;
pin = resolved commit SHA at first run into `dataset_locks.json`. Fetch via
raw.githubusercontent (no API token for public repos; honor `GITHUB_TOKEN`
for private). Then delegate parsing to the local-adapter readers — build
`local-adapter` first.

**Implementation notes.** `adapters/_github.py` (~100 lines) + config literal +
lock plumbing shared with `local-adapter`. Cache downloads under `~/.cache/itemeval/`.

### Item subset sampling — random / stratified, seeded
**Key:** `item-sampling`

**Motivation.** `dev` runs the *first N* items; serious piloting needs an
unbiased subset ("random 50, stratified by topic, seed 7"), and measurement
users expect it. Also the building block for pilot→full pooling (the
subset-superset case).

**Design sketch.**

```yaml
benchmark:
  sample: {n: 50, seed: 7, stratify_by: category}   # category = a metadata column
```

Sampling happens after adapter load, before the grid; the manifest records
`(n, seed, stratify_by)` plus the resulting item-id list, so a sample is
exactly reproducible and `status`/resume see a stable item set. Deterministic
given (seed, sorted item ids) — independent of row order.

**Implementation notes.** `_config.py` (new `sample` model), `_prepare.py`
(apply after `load_items`), `_manifest.py` (record), `budget/_policies.py`
(dev's first-N and `sample` compose: sample first, then dev truncates).
~60 lines + tests.

**Open questions.** Interaction with multi-dataset configs (sample per
dataset or over the union? — default: union).

### Crash-survivable progress (harvest `.eval` into the stores)
**Key:** `recoverable-harvest`

**Motivation.** Durable parquet is written **all-or-nothing after a clean
`eval()` return**: `generate/_run.py` runs one `inspect_ai.eval()` over all
conditions, then harvests per-condition from the **in-memory** logs — never the
on-disk `.eval`, no `try/finally` salvage. A hard mid-run death (SIGKILL/OOM, or
the force-kill of a stuck SSL read) writes **zero rows**; progress survives only
in inspect's `.eval`, which our code never reads back. So every store surface
(`status`/`export`/`report`) is blind to a killed run, and a persistently flaky
study that never completes one clean `eval()` yields no reportable store at all —
though ~all the data already exists in cache + `.eval`.

**Design sketch.** inspect already writes `.eval` incrementally (it's the WAL);
we just never project it back. Add a `_harvest.py` that reads stage `.eval` files
from disk (`read_eval_log`/`list_eval_logs`) and runs the **existing** row
builders into the stores — harvesting a stray `.eval` is self-describing
(`log.eval.metadata["itemeval"]["condition_id"]`) and idempotent (content-key
dedup). Trigger it on **every read** (`status`/`export`/`prepare`) and via an
explicit `itemeval harvest` command, announced (a read command that writes — Law
1), with `--no-harvest` to opt out. A live-during-run variant via inspect's hooks
API is deferred to ship with the mid-run tracker (shared heartbeat hook).

**Implementation notes.** Full brief in
[docs/plans/recoverable-harvest.md](plans/recoverable-harvest.md): 2 workstreams
(disk-harvest projection / classifier + integration), file:line grounded, no
fundamental blockers. **Gates `recovery-run-identity`** — R's "recovery fills
holes" assumes harvested rows, and R's per-experiment index consumes this
feature's `.eval` lifecycle classifier (harvested/unharvested) rather than owning
it.

**Open questions.** Resolved-with-recommendation in the brief: ship W1+W2
(read-triggered), defer the live hook to C; auto-harvest defaults on (announced)
with `--no-harvest`. Confirm at the brief-review gate.

### Recovery-aware run identity (experiment-scoped, with attempts)
**Key:** `recovery-run-identity`

**Motivation.** itemeval keeps two artifact classes under two identity rules:
**data** (`solutions`/`gradings.parquet`) is content-keyed, so re-runs
**converge** into one store; **provenance** (`run_id`, `manifests/*.json`, the
`.eval` pile in the shared stage dir) is invocation-keyed, so re-runs **fork**.
A *deliberately different* experiment should fork — that's the reproducibility
guarantee. But a **recovery re-run** (same config, finishing error/interrupt
holes) converges the data while forking the provenance anyway: a second
`run_id`/manifest/`.eval` pile for what is *one experiment*, with nothing
marking them as attempts of the same intent. Flaky endpoints make recovery the
*common* path, so the fragmentation bites constantly — and a `solutions.run_id`
column that mixes ids after recovery is hard to read.

**Design sketch.** Make run identity **experiment-scoped with attempts**;
default for an unchanged config = **recovery (converge + supersede)**, fresh
provenance only for genuinely new/changed work. R's real job is to make
*provenance* converge the way *data* already does: **rename** the `run_id`
column to `experiment_id` (derived) + `attempt` (int), where
`experiment_id = sha256(config_digest : study : stage)` and the digest is the
**parsed/normalized** config — identity-bearing fields only, so comments,
whitespace, and execution knobs (`max_concurrency`) never change identity (the
existing raw-bytes `config_sha256` is redefined to this digest and reused for
drift detection). Recovery is detected by the digest alone (an `experiment_id`
that already has a manifest); grown items / roster drift under an unchanged
config are a **soft warning, not a fork**. A persisted per-experiment index
(`manifests/experiments/<id>.json`) holds the attempts + current result set;
stale `.eval` is marked superseded (prune is opt-in). Foundational — gates the
mid-run tracker, which must read run state as experiments+attempts.

**Implementation notes.** Full brief in
[docs/plans/recovery-run-identity.md](plans/recovery-run-identity.md): 3
workstreams (identity / detection / convergence), file:line grounded, decisions
locked. **Non-additive** schema change (a `run_id` rename) — ships a `Study
migration` note + result-preserving clean-break tip per the pre-1.0 carve-out
in `DEVELOPMENT.md`. Owns the `_identity.py` normalization helper that P0's
lock-spec fix reuses (`normalized_spec()`). Optionally consumes **D**'s
terminal-vs-transient classifier to *label* missing cells (not needed for the
fork decision under Choice A).

### Response / attempt timeout
**Key:** `request-timeout`

**Motivation.** Neither itemeval nor inspect sets a total request timeout:
`GenerateConfig.timeout` and `attempt_timeout` both default `None`, and the
httpx client timeout doesn't catch a slow-but-alive trickle. So a degraded
stream runs **unbounded** — one stalled endpoint holds a whole run hostage with
no upper bound, the worst case in a flaky-routing setup.

**Design sketch.** Set a generous `attempt_timeout` default (high enough never
to cut a healthy stream) plus a `solvers.timeout` knob, so a stalled request is
abandoned and retried — likely onto a healthier route. Pass inspect's
`attempt_timeout` through unchanged (boundary rule: pass through, don't rename).

**Implementation notes.** Thread the knob into the `GenerateConfig` we build
(`generate/_task.py` / `grade/_judge.py`); `_config.py` `solvers` model gains
`timeout`. Orthogonal to the `max_tokens` context-fit clamp. A timeout = an
abandoned attempt, so it consumes `preflight-check`'s terminal-vs-transient
classifier (don't retry a terminal error).

### Truncation as a first-class signal
**Key:** `truncation-signal`

**Motivation.** A solution that stops on `max_tokens` is a truncated-but-
non-empty string we currently count `completed` and grade as finished — so a
**budget cut is scored as a content failure**, a validity bug that silently
corrupts measurement.

**Design sketch.** We already store `stop_reason` per solution. Surface it: add
a `truncated` channel to `status` (beside `incomplete`) and a `truncated`
export column, so a truncated cell is distinguishable from a genuine content
miss. (Unmapped provider finish-reasons collapsing to `"unknown"` is a separate,
upstream-rooted defect — tracked in KNOWN-ISSUES.)

**Implementation notes.** `_status.py` (new channel), export schema (new
column); `stop_reason` is already on the solutions row (`generate/_run.py`). No
new knob. ~60 lines + tests.

### Pre-flight model check
**Key:** `preflight-check`

**Motivation.** A dead model (e.g. `404` EOL) isn't caught until it fails
mid-paid-run, and per-condition failures flood the log. The user should see
roster health *before* committing spend.

**Design sketch.** Before the paid loop, fire one ~1-token call per distinct
model and surface a roster summary (`39 ok / 1 dead: <model> 404 EOL`), letting
the user fix the roster first. Don't retry **terminal** errors; collapse each
condition's failure to one concise line. Builds the **terminal-vs-transient
error classifier** — a shared primitive `request-timeout` also consumes.

**Implementation notes.** New pre-flight step before the money gate; the
classifier in a small shared module; `ConditionRunReport.message` already exists
for the concise per-condition line. Pairs with `cache-projection` as one
"before you spend, here's what will happen" pre-flight report.

### Pre-flight cache projection
**Key:** `cache-projection`

**Motivation.** A re-run's true cost is invisible up front: you can't see how
much will be served from inspect's response cache ($0) versus paid fresh, so the
pre-flight estimate over-states a cheap recovery re-run.

**Design sketch.** Probe inspect's response cache (its own dir,
`inspect_cache_dir("generate")`) before the gate and report `N cached / M fresh
→ ~$X real`. Distinct from `reuse-savings` (which reports reuse *post-hoc*); this
is a *pre-flight projection*. Pairs with `preflight-check` as one report.

**Implementation notes.** The risk is replicating inspect's cache key well
enough to predict hits without a live call. Read-only probe; no new spend, no
new knob.

### Custom scorer plugin point + more built-in verifiable scorers
**Key:** `scorer-plugins`

**Motivation.** Three built-in scorers cover integers, letters, and exact
strings; the long tail (regex extraction, normalized text, sympy-equivalence
for math, code-runs-tests) shouldn't require forking. A plugin point converts
users into contributors.

**Design sketch.** Two layers:

1. New built-ins where they're cheap and universal: `regex` (config-supplied
   pattern + group), `exact_match_normalized` (case/whitespace/punct folding).
2. User scorers: `facets.scorer: my_pkg.scorers:grade_sql` — an import-path
   reference to a callable `(solution: str, item: Item) -> ScoreOutcome`
   (same contract as `grade/_verifiable.py` internals). Recorded in the
   condition payload by import path **plus source-content hash** so a changed
   scorer changes condition ids, like templates.

**Implementation notes.** `grade/_verifiable.py` (new scorers + a
`resolve_scorer()` that handles `name` vs `module:attr`), `_config.py`
(scorer field accepts the reference form + optional `scorer_args` mapping),
`design/_grid.py` (payload hash), docs. ~120 lines. Keep judge-side
extensibility out of scope here (that's rubrics).

**Open questions.** Sandboxing/trust (it's the user's own code running
locally — document, don't sandbox). Whether `scorer_args` (e.g. the regex)
lives in the facet list to allow multiple scorer variants as conditions —
leaning yes: `scorer: [{name: regex, pattern: "..."}]`.

### Reliability & agreement report (`itemeval report`)
**Key:** `report-command`

**Motivation.** The target audience's first analysis is always the same:
judge agreement, score reliability, item difficulty spread, parse-failure
rates. Shipping it as a command turns "here's a parquet, good luck" into an
instant payoff — and it's the package's best demo artifact.

**Design sketch.** `itemeval report CONFIG [--json]` reads
`export/gradings_long.parquet` (running export first if stale) and prints:
per-condition score summaries; item difficulty distribution (mean score per
item + extremes); inter-grader agreement per rubric (pairwise Pearson/Spearman
+ exact-agreement rate) when ≥2 graders; replication consistency
(within-cell SD) when replications > 1; parse-failure/empty counts. Markdown
to stdout, full structured `--json`. **Descriptive only** — itemeval reports;
modeling (IRT, mixed-effects, variance components) stays in the user's
stats stack, with documented recipes in the wiki (Tutorial 3 pattern).

**Implementation notes.** New `report/_summary.py` (+ `build_report()` in the
public API), `cli.py` subcommand. Pure pandas over the export table — no new
deps (correlations via pandas; no scipy). ~200 lines.

**Open questions.** Krippendorff's alpha / ICC need either a small dep or
careful hand-rolling — start with correlations + exact agreement, add alpha
later if demand shows.

### Capability legibility for agents (discover before you run)
**Key:** `capability-legibility`

**Motivation.** A downstream agent writing a study config can't see what
itemeval already supports without reading the source: it hand-rolls a separate
pilot instead of `--policy dev`, guesses at valid `stratify_by`/`where` values,
or misses template placeholders. Post-hoc hints (emitted after a command runs)
are too late for an agent composing config and code up front — capabilities must
be discoverable *before* execution.

**Design sketch.** Two drift-resistant mechanisms in code plus a thin guide:
1. **Self-documenting scaffold + `--help`.** `init`'s config comments enumerate
   valid enum values inline (the `stratify_by` set, the `where` filters, the
   policies); each subcommand's `--help` lists its choices; `--policy dev` is
   named as *the* built-in pilot in the scaffold next-steps and in
   `estimate`/`status` output.
2. **Validation that teaches.** A wrong config errors with the valid set named
   (unknown `stratify_by` → lists the options; unknown `{placeholder}` → lists
   the supported placeholders).
3. **A short agent guide** (`AGENTS.md` / a wiki "writing a study" page) that
   points at 1–2 and the canonical commands and pitfalls.

Plus a **UX-PATTERNS contract** line: every capability must be discoverable
before execution (scaffold/help/guide), not only via post-hoc hints.

**Implementation notes.** `cli.py` (richer `--help` choices, scaffold
next-steps, the pilot line in `estimate`/`status`), the `init` scaffold (inline
enum comments), `_config.py`/`_templates.py` (teaching validation errors),
`docs/UX-PATTERNS.md` (the new contract row), `docs/wiki/` + `AGENTS.md` (the
guide). Mostly strings and validation messages; no behaviour change. ~120 lines
+ a doc page.

**Open questions.** Whether to also ship a machine-readable surface
(`itemeval schema` emitting the pydantic JSON Schema + adapters/policies/
placeholders) for full agent introspection — cleanest but more build; defer
unless the scaffold/help tier proves insufficient.

---

## Tier 2 — measurement depth

### Grader replication + judge sampling configs
**Key:** `judge-replication`

**Motivation.** Judge temperature is pinned to 0 and one judging pass is taken
as truth. For judge-reliability work the judge itself is a measurement
instrument: users need judge replications and controlled judge sampling
(temperature, reasoning effort) as **facets**.

**Design sketch.** `graders.<name>` gains `temperature` (default stays 0.0)
and `facets` gains `grade_replications` (default 1). Judge runs use inspect
epochs exactly like generation.

**Implementation notes.** The gradings key must grow: today it is
`(grade_condition_id, gen_condition_id, item_id, epoch)` where `epoch` is the
*generation* epoch. Add `grade_epoch` (default 0) to `store/_gradings.py`'s
key and schema, `grade/_judge.py` (epochs), `design/_grid.py` (payload gains
judge temperature), estimator (multiply judge calls), export (new column).
This is a **schema migration** — additive column with default, but bump the
minor version and note it. ~150 lines.

**Open questions.** None blocking; mostly cost-estimator accuracy for
high-variance judges.

### Import human ratings as a grade condition
**Key:** `human-ratings`

**Motivation.** The gold question in every LLM-as-judge study is "how does the
judge compare to humans?" If human scores can enter the gradings store as just
another grader, every downstream view (export, report, agreement) compares
human vs LLM for free. No mainstream harness does this well.

**Design sketch.**

```
itemeval import-gradings CONFIG ratings.csv --grader human_expert_1
```

CSV contract: `item_id, gen_condition_id (or condition slug), epoch, score`
(+ optional `reasoning`). Rows validate against the solutions store (must
reference existing solutions); imported rows get `grade_kind="human"`,
`usd=0.0`, a grade-condition id derived from the grader name + file hash.
Round-trip helper: `itemeval export --rating-sheet` emits a blank CSV of
solutions to hand to raters.

**Implementation notes.** New `grade/_import.py` + CLI subcommand;
`store/_gradings.py` allows `grade_kind="human"`; export untouched (rows look
like any grading). ~150 lines. The rating-sheet exporter is ~40 lines in
`store/_export.py`.

**Open questions.** Multiple raters per file vs one file per rater (start:
one grader name per invocation). Anonymizing solution provenance in the
rating sheet (blind grading) — probably a `--blind` flag that drops model/
prompt columns.

### Pairwise / comparative judging
**Key:** `pairwise-judging`

**Motivation.** Much of the judge literature uses pairwise preference
(A vs B → Bradley-Terry/Elo) rather than absolute rubric scores; position-bias
controls are standard. A natural extension of "grade fans out over stored
solutions" — the pair generator just walks the same store.

**Design sketch.** New grade mode: `facets.compare: {between: model, judge:
judge_a, order: both}` — for each (item, prompt, replication), build pairs of
stored solutions differing only in `model`, render a comparison template
(`{input}, {solution_a}, {solution_b}`), judge returns
`{"winner": "A"|"B"|"tie"}`, run both orderings when `order: both`. Results
land in a separate `comparisons.parquet` (different shape than gradings:
two gen-condition ids per row) and a separate export table.

**Implementation notes.** `grade/_pairs.py` (pair enumeration + task builder),
new store module `store/_comparisons.py`, parser extension in
`grade/_parse.py` (winner contract), estimator term, CLI surface (`grade
--compare` or a `compare` subcommand — leaning separate subcommand to keep
`grade` simple). The largest Tier-2 item; design doc first.

**Open questions.** Pair explosion control (all pairs vs one reference model
as baseline); whether ties are forced or allowed; whether this waits for
demand signal post-0.2.

### Partial / nested crossing designs
**Key:** `partial-crossing`

**Motivation.** `crossing: full` only. Real designs are often partial ("model
A only with prompt P1/P2, model B only with P3") or nested (items within
forms/testlets). Already on the README's feature list ("crossing structure").

**Design sketch.**

```yaml
crossing:
  mode: partial
  include:                       # explicit cells, or...
    - {model: openai/gpt-5-mini, prompt: [minimal, standard]}
  exclude:                       # ...full minus exclusions
    - {model: anthropic/claude-haiku-4-5, prompt: standard}
```

Nested item structure rides on item metadata (`form`, `testlet`) +
`item-sampling`'s stratified sampling rather than a separate mechanism; the export table already
carries metadata for grouping. Condition ids are unchanged (they hash cell
content, not the grid shape).

**Implementation notes.** `_config.py` (crossing model), `design/_grid.py`
(filter the full cross — simplest correct implementation), `_status.py`
(expected counts), estimator (counts). ~100 lines. The grid stays explicit in
the manifest so partial designs are auditable.

**Open questions.** Validation UX — refuse empty cells loudly; warn on
unreferenced facet values.

### Combine multiple runs on export
**Key:** `multi-run-export`

> Detailed, session-ready implementation plan for the grow-in-place UX
> (scale-up affordances · snapshots + study cards · waves):
> [docs/plans/archive/growth-ux.md](plans/archive/growth-ux.md).

**Scope decision (2026-06-11).** The pilot→full lifecycle's *primary* path is
**grow-in-place**, which already works and costs nothing extra: keep one
study, edit the config (raise `replications`, flip the policy, add models),
re-run — resume pays only the delta, the local response cache replays any
re-issued call free (extending replications 4→6 replays epochs 1–4 at $0),
and `dev`'s first-N items make the pilot a strict subset of the full run.
Combining stores cannot save money (both runs already paid); its honest niche
is pooling *organizationally separate* stores — a frozen pilot artifact,
different machines/collaborators, a re-run months later. Two consequences:
(1) a higher-priority sibling feature is a **config-drift warning** — when a
facet name matches stored rows but its content hash changed, say "prompt
'standard' changed; previous rows stay under the old condition, this run
starts fresh" before the gate, turning grow-in-place's one footgun into an
informed choice; (2) this feature stays narrow as specified below.

**Motivation.** Pool a small pilot with the full follow-up run under the same
setting into one analysis table — when the runs are genuinely separate
stores (see scope decision above; same-store growth needs no merging).

**Design sketch.** `itemeval export CONFIG --also studies/pilot_study`
(repeatable). Refuses to merge unless manifests are compatible: same dataset
ids + revisions, same template hashes, same model ids/sampling per shared
condition id — i.e. **shared condition ids must mean the same thing**, which
content-derived ids already guarantee. Incompatible → hard error listing the
differing fields; never silent pooling. Output gains a `source_study` column;
overlapping (condition, item, epoch) keys are an error (they'd be true
duplicates).

**Implementation notes.** `store/_export.py` (multi-store read + compatibility
check against `manifests/` + `dataset_locks.json`), CLI flag. ~120 lines.

### Wide-pivot export helpers
**Key:** `pivot-helpers`

**Motivation.** Long format is the contract, but most stats stacks want one
matrix per analysis (items × conditions). Users hand-roll the same pivots.

**Design sketch.** Keep the package thin: a documented `itemeval.pivots`
helper module (or just wiki recipes — decide by maintenance cost) with 3
canonical pivots: scores (item × gen-condition), judge agreement
(solution × grader), replication matrix (item × replication). Each returns a
DataFrame from the long table; no new file formats.

**Implementation notes.** Pure-pandas `store/_pivots.py` (~60 lines) exported
as `itemeval.pivot_scores(...)` etc., or documentation-only. Start as wiki
recipes in Tutorial 3/4; promote to code when the recipes stabilize.

### Auto flagship / latest-per-vendor selection
**Key:** `flagship-selection`

**Motivation.** Evaluating "the current state-of-the-art landscape" almost always
means *one flagship per vendor* (latest Anthropic × latest OpenAI × latest
Google …). Users want a **rule** for this so they don't hand-maintain a list as
new models ship — the natural ask alongside `model-sample-composition`'s recency
work.

**Why it's deferred (decided 2026-06-17).** There is **no reliable "flagship"
signal in the OpenRouter roster.** The obvious proxy — newest by `created` —
silently picks cheap variants released *after* the flagship (`gpt-5.1-nano` after
`gpt-5.1`; `:mini`, `:preview`, `:beta`, dated snapshots), the opposite of SOTA;
price-max is also only a loose proxy (breaks on reasoning models); and
name-parsing (`opus`>`sonnet`>`haiku`) is the fragile heuristic
`model-sampling` already rejected for `stratify_by: family`. A wrong pick
*silently invalidates* a capability study, so the honest path today is the
**`include:` recipe** (`model-sample-composition` ships it + a wiki recipe):
pin the flagships you mean, explicitly and reproducibly.

**Update (2026-06-19).** A live roster pull found OpenRouter now returns a
**`benchmarks`** block per model (per-arena Elo / win-rate / rank, ~38%
coverage) — a candidate for the "ranking field if one stabilizes" the open
question named. Still partial and third-party (not flagship-per-vendor, ~⅓
coverage), so the deferral stands; revisit if coverage broadens.

**Design sketch (when a signal exists).** A draw-time universe filter
`where: {latest_per_provider: true}` computed from `created` (never stored on
`ModelPrice` — it's a *relative* property), or a richer "top per vendor" once a
trustworthy flagship/tier signal is available. The `created` substrate from
`model-sample-composition` makes the `created`-based form ~15 lines.

**Open questions.** What counts as a trustworthy flagship signal (a curated
per-vendor flagship table? an OpenRouter usage/ranking field if one stabilizes?).
Whether to ship the honestly-caveated `latest_per_provider` (newest-by-date)
filter as a convenience despite the variant footgun, or wait for a real signal.

### Per-model generation config (heterogeneous rosters; structurally-missing cells)
**Key:** `per-model-config`

**Motivation.** A `model_config` facet applies one
`reasoning_effort`/`reasoning_tokens` to *all* solvers, so a thinking-on/off
facet can't be crossed with a heterogeneous roster: a single value can't toggle
reasoning per provider, non-reasoning models error or no-op, and there is no way
to mark a (model, config) cell as structurally absent. The cleanest "same base
model, two modes" comparison is currently inexpressible over a mixed object set.

**Design sketch.**

```yaml
facets:
  model_config:
    - name: think
      reasoning_effort: high
      overrides:                       # per-model reasoning settings
        openrouter/anthropic/claude-opus-4.8: {reasoning_tokens: 8000}
      skip_models: [openrouter/openai/gpt-3.5-turbo]   # cell recorded as missing
```

A facet keeps its global default and gains optional per-model `overrides` and a
`skip_models` set. Skipped (model, config) cells are recorded as structurally
missing (not run, not errored) so the grid and downstream analysis see an
explicit hole, not a silent gap.

**Implementation notes.** `_config.py` (`ModelConfigFacet.overrides`,
`.skip_models`), `design/_grid.py` (`resolve_gen_params` per (model, facet);
`expand_generate_grid` drops + records skipped cells), `generate/_params.py`,
the manifest/grid record. Pre-flight: a facet requesting reasoning on a model
the roster knows is non-reasoning fails at grid-build, not mid-run (folds in the
late-error robustness fix). ~120 lines + tests.

**Update (2026-06-19).** A live roster pull found OpenRouter now returns a
structured **`reasoning`** object per model (`supported_efforts`,
`default_effort`, `mandatory`; ~58% coverage) — a cleaner pre-flight answer to
"does this model support reasoning, and at what efforts" than the inferred
boolean, for the open question below.

**Open questions.** How "this model supports reasoning" is known pre-flight
(pricing-table `reasoning` flag vs. a runtime probe). Whether `skip_models` is
per-facet or a global (model, facet) exclusion matrix for many holes.

### Item covariates in the long export
**Key:** `item-covariates-export`

**Motivation.** `mapping.metadata: [cols]` is captured and written to
`items.parquet`, but `read_items()` is never called, so item covariates (and
`grading_scheme`) never reach the export — the long table has no per-item columns
to analyze against (difficulty by topic, score by max-points, year/split).
Studies must re-join the source dataset by hand. The capture/persist plumbing
exists; only read-back and projection are missing.

**Design sketch.** At export, join `items.parquet` into the long table by
`(item_id, dataset_id)` and project declared covariates — flattened columns for
declared `mapping.metadata` keys, plus `grading_scheme`, and an optional
`score_norm` (raw `score` ÷ a declared per-item max) for cross-rubric
comparability. The export schema stays stable for configs that declare no
metadata.

**Implementation notes.** Revive `read_items()` (`store/_items.py`), join in
`store/_export.py` (extend `EXPORT_SCHEMA` with declared covariate columns +
`grading_scheme` + optional `score_norm`). Reconcile stale `items.parquet` rows
on dataset change (the KNOWN-ISSUES item — fix here or before). ~90 lines +
tests.

**Open questions.** Flatten declared keys into columns vs. one `metadata_json`
column (lean: flatten declared, JSON the rest). Where the normalization
denominator is declared (a `mapping`-level `max_points: <col>` vs. an export
option). Modeling (variance components, IRT) stays in the user's stats stack —
this only widens the table.

---

## Tier 3 — scale and breadth

### Multimodal items
**Key:** `multimodal-items`

**Motivation.** Image-bearing benchmarks (charts, geometry, screenshots) are a
growing share of evaluation work; inspect_ai already supports content-part
messages.

**Design sketch.** `Item.input` gains an optional structured form: a list of
content parts (`text` / `image` with path-or-URL). Adapter mapping:
`mapping: {input: question, images: [image_col]}`. Generation renders prompt
templates around the text part and attaches images; judges grade text
solutions as today (judging *with* images is a later step). Estimator needs
per-image token pricing per provider — the hardest part.

**Implementation notes.** `_item.py` (content model), `adapters/_hf.py`
(image columns → cached local files), `generate/_task.py` (content-part
samples), `budget/_estimator.py` (image token heuristics), manifest (image
content hashes). Sizable; needs its own design pass before scheduling.

### Finer-grained resume (mid-cell checkpointing)
**Key:** `midcell-resume`

**Motivation.** Cell-level resume exists (parquet store + response cache); a
very large cell that dies near the end still re-walks its samples (cache makes
that cheap but not instant, and uncached samples re-pay nothing but time).

**Design sketch.** Lean on inspect's own machinery rather than building
checkpointing: on a failed/interrupted eval, locate the cell's last `.eval`
log and feed it to inspect's `eval_retry` semantics, then harvest rows as
usual. Explicitly **not** planned: a pause/break command (Ctrl-C + re-run
already covers it).

**Implementation notes.** `generate/_run.py` / `grade/_run.py` (retry path
selection), `store/_logs.py` (find the resumable log). Investigate inspect's
current retry API first — this may shrink to glue code.

### Savings report: count resume / response-cache reuse
**Key:** `reuse-savings`

**Motivation.** The 0.2 savings report covers prompt-cache + batch discounts;
local-cache hits carry no token usage, so "you re-ran this study for free"
shows up as $0 saved — undersells the package's own best feature.

**Design sketch.** A cache-served row (usd=0.0, empty usage) joins back to the
original paid row for the same (condition, item, epoch) — same study or a
prior run — and claims its token counts as "reuse savings", reported as a
third component next to cache/batch.

**Implementation notes.** `budget/_report.py` (the join + component),
`store/_solutions.py`/`_gradings.py` (identify cache-hit rows), docs caveat
(reuse savings are an attribution, not a discount). ~80 lines.

### Standalone study-card command (`itemeval card`)
**Key:** `card-command`

**Motivation.** 0.2.0 ships `STUDY_CARD.md` *inside* `export --snapshot` (the
`study-card` work — see CHANGELOG 0.2.0). What remains is rendering the same
card **without** taking a snapshot: one command that turns a study into a
shareable Markdown "study card" — design grid, dataset pins, template hashes,
spend, completion, headline per-condition table. Useful as a paper appendix, a
PR comment, or a repo README; every shared card advertises the provenance
discipline.

**Design sketch.** `itemeval card CONFIG > STUDY_CARD.md` — pure read of
manifests + status + ledger + export, reusing the snapshot card renderer. No
network, no new deps. Optionally later: `--push hf:org/dataset` to publish the
card + export parquet as a HF dataset.

**Implementation notes.** Factor the shipped snapshot card writer into
`report/_card.py` + a CLI subcommand; templated f-string Markdown. ~60 lines on
top of the existing renderer. Push-to-Hub is separate and opt-in (new dep
surface: `huggingface_hub` is already transitively present via `datasets`).

### Semantic dedup of roster routing variants
**Key:** `roster-dedup`

**Motivation.** OpenRouter lists the same base model under routing-variant
suffixes (`:nitro`, `:thinking`, `:extended`, `:online`, and the `:free`
duplicates). Two variants of one model in the drawable roster double-count that
model in a `pricing-table` sample. Today only exact-id dedup happens
(`sorted(set(...))` in `_build_universe`).

**Why it's deferred.** `sample-exclude`'s non-free-roster change already drops
the `:free` duplicates, which are the bulk; the survey behind
[docs/plans/archive/sample-exclude.md](plans/archive/sample-exclude.md) found
exactly **1** suffixed id left in the non-free ≥2023 roster, so exact-id dedup is sufficient
for now. Building this earns almost nothing until a real duplicate problem
appears.

**Update (2026-06-19).** A live roster pull confirmed the deferral holds —
still exactly **1** drawable duplicate (`qwen-plus-2025-07-28` + its `:thinking`
sibling, arguably a distinct variant to *keep*). It also showed OpenRouter now
returns a first-class **`canonical_slug`** per model (100% coverage): when this
is built, grouping by `canonical_slug` replaces the fuzzy suffix-stripping below
and largely answers the "which variant wins" open question.

**Design sketch (when needed).** A canonical-key collapse at the **roster layer**
(`_build_universe`, never `refresh_pricing` — the saved table must keep every
variant so `lookup_price` prices an explicitly-named `:nitro`/`:free`): strip a
known suffix set to a base id, keep one representative per base. **Open
questions.** Which suffixes are routing vs. genuinely distinct models; which
variant wins (base? cheapest? most-metadata?) given variants can carry different
price/context; whether to expose the suffix set as a knob (default: no — keep it
a fixed internal list per "don't over-engineer").

---

## Ops / release

### PyPI publish approval gate
**Key:** `pypi-approval-gate`

Add a GitHub `pypi` Environment with a required-reviewer rule; reference it
from `release.yml` (`environment: pypi`) and mirror it on the PyPI trusted
publisher. Today, publishing a GitHub Release uploads immediately; the gate
adds a manual approval click between release and upload. Pure CI config — no
code.

---

## Explicitly out of scope (and why)

- **Hosting/serving a leaderboard or dashboard UI** — itemeval produces
  analysis-ready tables; visualization belongs to the user's stack
  (`inspect view` already covers transcript browsing).
- **Statistical modeling (IRT/mixed-effects/variance components) inside the
  package** — recipes in the wiki, yes; a stats engine, no. The export format
  is the contract with the stats world.
- **Agentic/multi-turn task evaluation** — inspect_ai's solver chains can do
  it, but itemeval's design contract (item → single solution → grading
  events) would need rethinking; revisit only with strong demand.
- **A pause/break command** — Ctrl-C + re-run is already safe and complete.
