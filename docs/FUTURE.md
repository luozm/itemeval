# FUTURE.md — feature backlog with design notes

Planning document for post-0.1 features. [ROADMAP.md](../ROADMAP.md) says
*what* is committed for which release; this file holds the *why and how* for
everything beyond that — one section per feature: motivation, design sketch,
implementation notes (which modules change), and open questions. Nothing here
is promised; sections graduate to ROADMAP when scheduled. Every design here
must comply with [UX-PATTERNS.md](UX-PATTERNS.md) (the binding UX contract:
side-effect announcements, hint framework, consent rules, knob buckets).

Tiers reflect adoption value, not effort:

- **Tier 1 — adoption blockers**: things a new user expects on day one.
- **Tier 2 — measurement depth**: features for the core item-response
  audience that competitors don't have.
- **Tier 3 — scale and breadth**: bigger studies, more modalities, polish.

---

## Tier 1 — adoption blockers

### 1.1 Local file adapter (`adapter: local`) — jsonl/csv/parquet

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

### 1.2 GitHub repo adapter (`adapter: github`)

**Motivation.** Benchmarks that live as files in a repo (competition archives,
org-internal sets) — already promised in the README feature list.

**Design sketch.** `{repo: org/name, ref: <sha|tag>, path: data/items.jsonl}`;
pin = resolved commit SHA at first run into `dataset_locks.json`. Fetch via
raw.githubusercontent (no API token for public repos; honor `GITHUB_TOKEN`
for private). Then delegate parsing to the local-adapter readers — build 1.1
first.

**Implementation notes.** `adapters/_github.py` (~100 lines) + config literal +
lock plumbing shared with 1.1. Cache downloads under `~/.cache/itemeval/`.

### 1.3 Item subset sampling — random / stratified, seeded

**Motivation.** `dev` runs the *first N* items; serious piloting needs an
unbiased subset ("random 50, stratified by topic, seed 7"), and measurement
users expect it. Also the building block for pilot→full pooling (3.2's
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

### 1.4 Custom scorer plugin point + more built-in verifiable scorers

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

### 1.5 Reliability & agreement report (`itemeval report`)

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

### 1.6 Cache-aware execution scheduling (maximize provider prompt-cache discounts)

> **Status: SHIPPED in 0.2 (2026-06-11)** and validated in a live pilot
> (~$2.2 via OpenRouter). What landed: cache observability in run summaries;
> judge dataset ordering; `solvers.cache_prompt`; `budget.cache_schedule`
> (warm-then-fan-out gate); `graders.<name>.split_rubric` and
> `solvers.split_prompt` (shared head → system message, where the provider
> breakpoint lands); provider-aware cache-write pricing + OpenRouter cache
> rates in `--refresh-pricing`. Pilot findings that amended the design:
> (1) single-block text prompts get **no** Anthropic cache marker through
> inspect→OpenRouter — the split layouts are *required* there, and they
> halved a real judge bill (78% input-side discount vs 0% monolithic);
> (2) the shared head must clear the per-model minimum (4096 tokens for
> Haiku 4.5) or caching silently no-ops; (3) provider path matters for the
> gate: on the DIRECT OpenAI/Anthropic APIs the warm-then-fan-out gate
> roughly halved cost vs concurrent bursts (direct OpenAI 0%→90% hit rows,
> direct Anthropic 40%→80-90%), while through OpenRouter the proxy's stagger
> and sticky routing made bursts cache well on their own; the gate also
> routes byte-identical duplicate judge calls into the free local response
> cache; on direct Anthropic, monolithic prompts auto-cache (split optional
> there, required via OpenRouter); (4) OpenRouter may route Anthropic models
> to Bedrock, which ignores the markers — pin the provider for cached runs.
>
> **Tail completed 2026-06-12** (plan: `docs/plans/archive/cache-tail.md`):
> `provider_routing` (solvers + graders) pins the OpenRouter upstream, with
> the `openrouter-unpinned-cache` hint productizing finding (4); the
> `split-head-below-min` hint + per-provider minimums table productize
> finding (2); the estimator projects the per-group cache split so the money
> gate sees the discounted cost; direct-OpenAI runs automatically attach
> `prompt_cache_key` (per study+condition) and surcharge-free 24h retention
> (the Phase-1 investigation below: supported, via `get_model` model_args).
> Still open: per-cache-group OpenAI keys (needs upstream GenerateConfig
> support), prefix-keyed gating refinements, Anthropic 1h-TTL markers
> (inspect hardcodes the marker shape — upstream issue, wrap don't fork).

**Motivation.** Provider-side prompt caching (input-prefix KV reuse) discounts
repeated input tokens by ~75–90% — but only when calls are *scheduled* to hit
it. A cache entry becomes readable only after the first request's response has
begun, so N identical-prefix calls fired concurrently (inspect's default) all
miss and pay full price. Replication designs and judge fan-outs are exactly
the repeated-prefix workloads this discount was built for; today itemeval
captures it only by accident. Unlike the local response cache ($0 replay of
identical calls), the provider cache discounts *fresh sampling* — so it is the
only discount available to replications, which must be independent draws.

**Provider facts the design must respect** (verified 2026-06; per-token rates
live in the pricing table, refreshed from OpenRouter):

| | activation | granularity | min prefix | write cost | read cost | lifetime |
|---|---|---|---|---|---|---|
| Anthropic | opt-in `cache_control` breakpoints | **block boundary** | 1k–4k tokens by model | 1.25× input | 0.1× | 5 min, refreshed on use |
| OpenAI | automatic | token prefix (~128-token steps) | 1k | free | ~0.1× (GPT-5 family) | 5–10 min (24 h opt-in) |
| Gemini | implicit (2.5+) / explicit `CachedContent` | token prefix | 2k–4k | free / storage-billed | ~0.1× | minutes / chosen TTL |
| DeepSeek, Grok, … (via OpenRouter) | automatic | token prefix | varies | free | ~0.25–0.5× | minutes |

Two structural consequences: (a) on token-prefix providers, *ordering and
staggering* alone unlock partial-prefix reuse; (b) on Anthropic, partial
reuse additionally requires the shared content to end at a **block boundary
with a breakpoint** — inspect places one on the last *system* block, but
itemeval's judge prompt is currently one monolithic user block, so same-item
judge calls share no cacheable boundary on Anthropic today (byte-identical
replications are fine: the full block is the shared prefix).

**Design — four phases, each independently shippable:**

*Phase 0 — observability (no behavior change).* Print a per-condition cache
line in `generate`/`grade` summaries and `status`: cache-read/write token
totals and hit rate (rows with `cache_read_tokens > 0` / rows). The columns
already exist; this makes the win (or its absence) visible before and after
each later phase. Files: `generate/_run.py`, `grade/_run.py`, `_status.py`.

*Phase 1 — ordering and config (cheap, low risk).*
- Sort the judge dataset by `item_id` (then gen-condition, epoch) so
  same-prefix calls are adjacent in the schedule — with bounded
  `max_connections`, adjacency alone converts much of each group's tail into
  cache reads on token-prefix providers. File: `grade/_judge.py` (the
  `pending` iteration order).
- Opt the generate stage into `cache_prompt` when `replications > 1`
  (currently grade-only) — identical epochs share the full prompt, so
  Anthropic replications become cacheable with no prompt restructuring.
  New config `solvers.cache_prompt: auto|on|off` (default `auto` = on when
  replications > 1). Files: `generate/_task.py`, `_config.py`.
- Investigate passing OpenAI's `prompt_cache_key` (per-group routing) and
  `prompt_cache_retention: "24h"` through inspect's config; if supported,
  set the key per cache group and retention for multi-phase runs.

*Phase 2 — warm-then-fan-out gate (the core).* A `cache_gate` solver inserted
before `generate()` in both stages' solver chains. Samples carry a
`cache_group` key in metadata (generate: `item_id` when replications > 1;
grade: `item_id`). Within the eval's asyncio loop the solver does per-group
leader election: the first arrival runs immediately; followers `await` the
group's `asyncio.Event` (set when the leader's call returns, with a timeout +
error fallback so a failed leader never blocks the group), then proceed
concurrently. Leaders of different groups still run in parallel, so wall-time
cost is roughly one extra call-latency per group — negligible once group
count exceeds `max_connections`. Because the leader→follower gap is seconds,
TTL expiry within a group is a non-issue and no chunking machinery is needed.
Config: `budget.cache_schedule: auto|off` (default `auto` = gate only when
the provider caches, the estimated shared prefix ≥ the provider minimum, and
group size ≥ 2). Disabled under batch mode (batch processing reorders calls
anyway; cache hits there are best-effort bonus). New file
`generate/_cachegate.py` (~80 lines) + wiring in both task builders.

*Phase 3 — block-structured judge prompts (Anthropic partial-prefix reuse).*
Split the rendered rubric at the `{solution}` placeholder: everything before
it (rubric header + problem + grading scheme + reference) becomes a *system*
message, the solution (+ format suffix) the user message. inspect already
puts an explicit cache breakpoint on the last system block, so the boundary
lands exactly at shared/varying — same-item judge calls then read the long
shared prefix at 0.1× on Anthropic, and token-prefix providers are unaffected
(system+user concatenate into the same token stream). Caveats: judge-behavior
shift (rubric as system vs user) must be validated on a pilot; the rendered
content is unchanged so template hashes stay stable, but record the render
mode in the manifest and condition payload so the change starts fresh
conditions rather than silently mixing. Files: `grade/_judge.py`
(`build_judge_input` → message list), `design/_grid.py` (payload),
`_manifest.py`.

*Phase 4 — provider-accurate cache pricing.* The default math (read 0.1×,
write 1.25×) is exact for Anthropic but overcharges OpenAI/Gemini writes
(free). Default `cache_write_usd_per_mtok` to 0 for non-Anthropic providers
unless the pricing table says otherwise, and surface "realized vs achievable"
cache savings in the export savings report using Phase-0 hit rates. Files:
`budget/_pricing.py`, `budget/_report.py`.

**Expected effect** (input-side, shared-prefix portion): a judge condition
with K solutions per item pays ≈ `(write + 0.1·(K−1))/K` of list — for K=12,
~80% off on Anthropic and ~82% on OpenAI, versus ~0% today under concurrent
misses. N=4 replications: ~61% (Anthropic) / ~67% (OpenAI) off the prompt
tokens. Output tokens are never discounted, so end-to-end impact is largest
for judge stages (long inputs, short outputs).

**Out of scope.** Gemini *explicit* `CachedContent` management (billable
storage objects with their own lifecycle — revisit only if implicit caching
proves insufficient); cross-run cache persistence (TTLs are minutes; cross-run
reuse is the local response cache's job).

---

## Tier 2 — measurement depth

### 2.1 Grader replication + judge sampling configs

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

### 2.2 Import human ratings as a grade condition

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

### 2.3 Pairwise / comparative judging

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

### 2.4 Partial / nested crossing designs

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

Nested item structure rides on item metadata (`form`, `testlet`) + 1.3's
stratified sampling rather than a separate mechanism; the export table already
carries metadata for grouping. Condition ids are unchanged (they hash cell
content, not the grid shape).

**Implementation notes.** `_config.py` (crossing model), `design/_grid.py`
(filter the full cross — simplest correct implementation), `_status.py`
(expected counts), estimator (counts). ~100 lines. The grid stays explicit in
the manifest so partial designs are auditable.

**Open questions.** Validation UX — refuse empty cells loudly; warn on
unreferenced facet values.

### 2.5 Combine multiple runs on export

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

### 2.6 Wide-pivot export helpers

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

---

## Tier 3 — scale and breadth

### 3.1 Multimodal items

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

### 3.2 Finer-grained resume (mid-cell checkpointing)

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

### 3.3 Savings report: count resume / response-cache reuse

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

### 3.4 Study card generator

**Motivation.** Reproducibility marketing: one command that renders a study
into a shareable Markdown "study card" — design grid, dataset pins, template
hashes, spend, completion, headline per-condition table. Useful as a paper
appendix, a PR comment, or a repo README; every shared card advertises the
provenance discipline.

**Design sketch.** `itemeval card CONFIG > STUDY_CARD.md` — pure read of
manifests + status + ledger + export. No network, no new deps. Optionally
later: `--push hf:org/dataset` to publish the card + export parquet as a HF
dataset.

**Implementation notes.** `report/_card.py` + CLI subcommand; templated
f-string Markdown. ~120 lines. Push-to-Hub is separate and opt-in (new dep
surface: `huggingface_hub` is already transitively present via `datasets`).

---

## Ops / release

### 4.1 PyPI publish approval gate

Add a GitHub `pypi` Environment with a required-reviewer rule; reference it
from `release.yml` (`environment: pypi`) and mirror it on the PyPI trusted
publisher. Today, publishing a GitHub Release uploads immediately; the gate
adds a manual approval click between release and upload. Pure CI config — no
code. (Carried over from ROADMAP post-0.1 list.)

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
