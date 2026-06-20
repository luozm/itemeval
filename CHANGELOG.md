# Changelog

All notable changes to itemeval are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com); versioning follows
[SemVer](https://semver.org) (pre-1.0: minor bumps may break APIs).

## [Unreleased]

### Added
- **Provider-aware reroute for soft failures** (`output-validity-reroute`): a new
  opt-in knob `solvers.max_reroutes` (int, default `None` = off) makes `generate`
  automatically re-issue a **soft-failed** solution — one that completed with no API
  error but that the provider marked failed (`native_finish_reason="error"`, or the
  flattened `stop_reason="unknown"`) — on a different OpenRouter backend, adding the
  one that failed to `provider:{ignore:[…]}`, up to `max_reroutes` rounds
  (accumulating the bad backends). Motivation: OpenRouter load-balances each call,
  and a flaky backend returns HTTP 200 + `finish_reason=error` + empty/truncated
  content that `allow_fallbacks` and inspect both treat as final — a false floor at
  full cost (real cases: `glm-5.1`→GMICloud, `kimi-k2.6`→DigitalOcean,
  `qwen3.5`→Phala, each clean via another backend). A recovered cell replaces the
  bad row in place (same epoch); a cell still failing after the cap keeps its honest
  soft-failure row (no fake score) and is surfaced in the run summary
  (`reroute: N re-issued · M recovered · K still invalid`), the
  `reroute-residue` hint, and new `GenerateResult` fields
  (`rerouted`/`reroute_recovered`/`reroute_unresolved`, `--json` parity). Re-issues
  are fresh (cache off) and the extra spend folds into `total_usd`. Detection reads
  the `provider-finish-capture` columns; the reroute is the verbatim
  `provider_routing` object with `ignore` extended. `max_reroutes` is an
  operational retry policy — non-identity (it never enters condition ids or the
  `experiment_id` digest, so a recovery run converges and cleans up). Skipped under
  a batch plan and for wave/offset runs; single-provider models cannot be rerouted
  (the residue names them). `None` is a pure no-op.

  Closes: output-validity-reroute
- **Serving provider + native finish_reason captured in the stores/export**
  (`provider-finish-capture`): every solver and judge call now records two raw
  provenance columns — `served_provider` (the OpenRouter backend that actually
  answered, e.g. `GMICloud`/`Fireworks`) and `native_finish_reason` (the provider
  `finish_reason` *before* inspect flattens it into `stop_reason`) — on
  `solutions.parquet` and `gradings.parquet`, flowing to the export as
  `gen_served_provider` / `gen_native_finish_reason` (solver) and
  `grade_served_provider` / `grade_native_finish_reason` (judge). Motivation:
  OpenRouter load-balances each call across backends, and a flaky one returns a
  "soft failure" — HTTP 200 with `finish_reason=error` and empty/truncated content
  — that inspect's `stop_reason` flattens to `unknown`, hiding both the cause and
  the backend. These columns make that diagnosable straight from the export
  instead of by hand-reading every `.eval`. Pure provenance: no new knob, hint,
  gate, status line, or result field; null when the provider/cache/mock did not
  return the fields. Additive-with-default on both stores (an older store reads the
  columns as null).

  Closes: provider-finish-capture
- **Grade-time skip for over-long solutions** (`oversized-solution-skip`): a new
  per-grader knob `graders.<name>.max_solution_chars` (int, default `None` = off)
  makes `grade` auto-score 0 — **without a judge call** — any stored solution whose
  visible text exceeds the threshold. Motivation: weak models emit
  repetition-loop outputs (real cases: 128k–376k chars) that are not valid proofs;
  paying the judge to grade them is waste, and they score 0 anyway. Skipped rows
  are written with `score=0`, `parse_ok=False`, `parse_error="oversized_skip"`,
  `judge_completion=None` (mirroring the empty-solution skip — recorded, not
  graded, never a parse failure), and the count surfaces in the run summary
  (`oversized solutions: N scored 0 without grading (over max_solution_chars)`)
  and the new `GradeResult.oversized_skipped` field (`--json` parity). Empty
  handling applies first, so a solution is never counted as both empty and
  oversized. Identity treatment matches `solvers.on_empty`: the knob enters the
  `experiment_id` digest (it changes what gets graded) but never a grade
  condition id. `None` threshold is a pure no-op — no behavior change unless set.

  Closes: oversized-solution-skip
- **Live-run heartbeat + `--json` liveness** (`live-tracker`): a `generate`/`grade`
  stage no longer goes dark when inspect's live display is silenced. Under `--json`
  (which forces `display=none`), `--display none`, or any non-TTY run, the stage now
  emits a throttled plain-text **heartbeat to stderr** as samples complete —
  `[itemeval] generate · exp a7b3c9d2/a1 · 142/400 (35%) · 11/min · ~3m left · 2
  errors · 8 in-flight` — carrying live counts, a **throughput-based ETA** (refining
  the static pre-flight prior), error and in-flight counts, and the
  experiment_id/attempt (`recovery-run-identity`). It is gated to exactly the dark
  cases (inspect's rich bars carry liveness otherwise; a notebook is unaffected),
  throttled so it never slows the run, and **relay-safe** — a plain line an agent can
  quote, unlike a progress bar that never rendered off-TTY (UX-PATTERNS Law 8). The
  pre-flight ETA, previously printed only on the human stdout path, is now also
  echoed to **stderr** under `--json` (a one-line `starting generate — ~3m …`), so a
  `--json`/backgrounded run shows intent before its first sample lands. stdout under
  `--json` stays **exactly one JSON document**: the heartbeat is a single inspect
  `SampleEnd`/`SampleStart` hook (the published extension point — wrap, don't fork)
  that **pre-latches inspect's hook-startup banner** before registering, so
  registering it never leaks inspect's "hooks enabled" line onto stdout. Pure
  liveness — **no new config knob, gate, exit code, JSON field, or hint**, and no
  fact of record lives only in the ephemeral output (final counts/spend stay in the
  summary block and result JSON). This **closes the "`--json` goes dark" gap**: the
  Agent-Guide's "run the paid stages without `--json`" carve-out is relaxed back to
  "`--json` everywhere is safe — liveness rides stderr." The same `SampleEnd` hook is
  the heartbeat the deferred "live store during the run" idea would have used; that
  idea is **dropped** — a per-sample parquet flush is redundant (a separate `status`
  already projects a running stage's incremental `.eval` via `recoverable-harvest`)
  and harmful (the hook is awaited inside `eval()` and parquet is whole-file rewrite
  → O(N²)). No new dependency.

Closes: live-tracker

- **Pre-flight response-cache projection** (the `estimate`/`generate`/`grade`
  pre-gate line gains a `cache: N of M remaining calls already in the local
  response cache ($0) → ~$X real of $Y projected` clause). A re-run's true cost was
  invisible up front: a call missing from the *store* may still replay from
  inspect's **local response cache** at $0 — inspect writes that cache inside
  `Model.generate`, right after the API returns, *before* the sample reaches the
  `.eval`/store, so a crash leaves a window where the response cache has a call the
  store doesn't. (Recovery re-runs are the key case, alongside `--force` and
  `replications` bumps; an ordinary first-run/resume correctly shows nothing
  cached.) itemeval now **probes that cache before the gate**: it reconstructs the
  identical `CacheEntry` inspect builds (same rendered messages, `GenerateConfig`,
  resolved model → `base_url`, epoch) and checks whether its key file exists —
  *reusing inspect's own `CacheEntry`/`cache_path`* rather than re-deriving the
  md5 (a round-trip guard test pins the reconstruction to the installed inspect, so
  an inspect cache-key change goes red instead of silently mis-counting). Both
  stages are probed (a materializing rubric's judge calls are conservatively
  counted fresh — never a false $0). New append-only `StageEstimate` fields
  `cache_hits` / `cache_misses` / `real_remaining_usd` (the latter prices only the
  fresh remainder). **The money gate is unchanged** — it keeps comparing the
  ceiling `remaining_usd` (UX-PATTERNS Law 2); `real_remaining_usd` is
  informational, beside the expected-cost figure. Read-only and lazy: the probe
  resolves models / imports inspect only when `config.cache` is on **and** the
  response cache is non-empty, so an ordinary cold-cache estimate stays
  engine-free and untouched, and any probe failure (e.g. a model that can't be
  resolved without a key) silently yields no projection rather than breaking the
  estimate. No new knob, no new dependency.

Closes: cache-projection

- **Truncation as a first-class signal** (`truncated` status channel + export
  column + a `truncated-completions` hint). A solver that stops on a length cap
  (`max_tokens`, or the model's own `model_length`) returns a **truncated-but-
  non-empty** string; itemeval recorded it with `error=None` and a non-blank
  `solution`, so `status` counted it `completed` and `grade` scored it as a
  finished answer — **a budget cut was silently scored as a content failure**, a
  validity bug. `stop_reason` was already stored per solution, so this surfaces it
  with **no store-schema change**: `status` gains a `trunc` column (a
  `ConditionStatus.truncated` count per generate condition), the `gradings_long`
  export gains an additive `truncated` boolean column, and `generate` ends with a
  coded `truncated-completions` hint (`N completion(s) stopped at a length cap …
  raise solvers.max_tokens or filter truncated rows`) plus an append-only
  `truncated_total` on `GenerateResult`. Truncation = `{max_tokens, model_length}`
  (length caps); it is the **disjoint complement** of the existing empty/
  `incomplete` channel (an *empty* length-cap stop stays `incomplete`), and an
  **informational sub-count of `completed`** — it never reclassifies a row, changes
  the money gate, grading eligibility, or `on_empty`. No new knob, no new gate.
  (`content_filter`/`unknown` stop reasons are a separate, upstream-rooted concern
  tracked in KNOWN-ISSUES.) No new dependency.

Closes: truncation-signal

- **Pre-flight model check** (`itemeval preflight CONFIG`): probe every distinct
  model in the grid with one ~1-token call and report roster health
  (`39 ok · 1 dead · 0 unverified`) **before** committing to a paid run, so a dead
  model (a `404` EOL, bad auth) is caught at sub-cent cost instead of failing
  mid-paid-run and flooding the log. It ships as a **separate staged command**
  (like `estimate`) rather than auto-running inside `generate`/`grade`: invoking
  it is the consent to its tiny spend, so the probe never spends model money the
  user did not ask for (the money gate stays the only *surprising* spend) and adds
  no per-run latency. The probe calls inspect's published `Model.generate`
  directly (`max_tokens=1`, `max_retries=0`) — wrapping, not forking — and writes
  no `.eval` log. Exit `1` when any model is **dead** (so `preflight && generate`
  short-circuits for agents/CI), else `0`; `--json` carries `ok`/`dead`/
  `unverified` counts and a per-model `{id, status, detail, http_status}` array.
  Mock (`mockllm/*`) ids probe ok with no network. New public `preflight_study(
  prep) -> PreflightReport` (Python surface; never prompts). It is built on, and
  ships, a reusable **terminal-vs-transient error classifier** (`_classify`): a
  pure function that labels a model-call failure `terminal` (dead/EOL/auth — fix
  the roster) vs `transient` (timeout/rate-limit/5xx — retryable) vs `unknown`,
  biased conservatively so a merely rate-limited model is never reported dead. The
  same classifier now prefixes each errored condition's one-line summary message
  in `generate`/`grade` (`terminal: 404 model not found` vs `transient: timeout`),
  so a run's failures say whether to edit the roster or just re-run. The shipped
  `request-timeout` feature's deferred "don't retry a terminal timeout" refinement
  will consume this classifier; suppressing inspect's per-sample retry on a
  terminal error is itself out of scope here (it needs an inspect retry hook). No
  new config knob, no new exit code, no new dependency.

Closes: preflight-check

- **Per-attempt request timeout** (`solvers.attempt_timeout` /
  `graders.<name>.attempt_timeout`): bound how long one model attempt may stall
  before it is abandoned and retried. Neither itemeval nor inspect set any request
  timeout, so a degraded stream ran **unbounded** — one stalled endpoint could hold
  a whole run hostage with no upper bound (the worst case in a flaky-routing
  setup). Both knobs pass straight through to inspect's
  `GenerateConfig.attempt_timeout` (seconds; "abandon attempt and retry"), so a
  timed-out attempt is retried and, through OpenRouter, may reroute to a healthier
  upstream. **Opt-in** (`None` = today's unbounded behavior) — chosen over an
  auto-applied default because a value picked without data could silently turn a
  slow-but-valid reasoning stream into a failure, and under a batch plan a timeout
  would bound the hours-long batch poll itself. A pure execution/robustness knob:
  it never enters a condition id or the `experiment_id` digest (so setting it never
  re-keys a study; popped in `_identity._NON_IDENTITY_SOLVERS`/`_NON_IDENTITY_GRADER`),
  rides the run manifest's config echo for provenance, and adds no new CLI flag,
  exit code, gate, or hint. The "don't retry a terminal error" refinement waits for
  the upcoming pre-flight model check's terminal-vs-transient classifier. No new
  dependency.

Closes: request-timeout

- **Crash-survivable progress: harvest `.eval` into the stores** (`itemeval
  harvest`, and auto-harvest on read/resume). Durable parquet was written
  all-or-nothing *after* a clean `eval()` return: a stage ran one
  `inspect_ai.eval()` over all conditions, then projected the **in-memory** logs
  into the solutions/gradings stores — never the on-disk `.eval`, with no
  `try/finally` salvage. So a hard mid-run death (SIGKILL/OOM, or a force-killed
  stuck SSL read) wrote **zero rows**: every store surface (`status`/`export`/
  `report`) went blind to the killed run, and a persistently flaky study that
  never completed one clean `eval()` produced no reportable store at all — though
  ~all the data already existed in inspect's `.eval` (its incremental write-ahead
  log) plus the response cache. itemeval now **reads that `.eval` back**. A new
  `itemeval harvest CONFIG [--json]` projects every unharvested generate + grade
  `.eval` into the stores through the **same** row builders the live run uses
  (factored into shared `persist_generate_condition` / `persist_grade_condition`
  helpers, so a recovered row is byte-identical to a live one), recovering a
  crashed run's epoch/wave identity from its manifest (written *before* the eval,
  so it survives the kill). It is **idempotent** two ways — a classifier skips
  logs already in the stores, and the content-keyed upserts dedup regardless — so
  it is safe to run repeatedly. `status`, `export`, `generate`, and `grade`
  **auto-harvest first** (announced: `recovered N solutions + M gradings from K
  interrupted run log(s) into the store …`; UX-PATTERNS Law 1 — a read/resume
  command that writes recovered rows), so the store reflects reality *whenever you
  look* and a re-run resumes (never re-pays) the recovered cells; `--no-harvest`
  opts out, and the harvest rides an append-only `harvested` object on each
  command's `--json`. The Python surface adds `harvest_study(prep) ->
  HarvestReport` (new public export); `prepare_study`/`build_status`/
  `export_study` stay pure reads (no hidden writes — a library never surprises a
  notebook). The live-during-run variant (an inspect hook flushing rows mid-run)
  is deferred to ship with the mid-run tracker (shared heartbeat hook). Owns the
  `.eval` harvested/unharvested lifecycle classifier that `recovery-run-identity`
  will consume. No new dependency.

Closes: recoverable-harvest

- **Safe re-bless + change briefing for a drifted sample lock** (`itemeval
  rebless`): when a pinned `solvers.sample` spec *genuinely* changes (a real
  `n`/`seed`/`stratify_by`/`where`/`universe` edit — additive fields are already
  normalized away by the `lock-spec-brick` fix), `generate`/`grade` no longer
  dead-end on the terse `clear model_locks.json`. They now print a **change
  briefing** — a field-level diff (`n: 2 → 3`, `where.provider: (none) →
  [openai]`), a note that the pinned panel was drawn under the old spec, and the
  two safe actions — and a new `itemeval rebless CONFIG` command **records the new
  spec while keeping the pinned panel** (no re-draw): the panel you already drew
  and have results for stays the scientific object, and the lock keeps **both** the
  spec it was drawn under (`sample`) and the re-blessed spec
  (`reblessed_spec`/`reblessed_at`, original `resolved_at` preserved), so later
  runs compare against the re-blessed spec. Re-blessing is announced (a pin write,
  UX-PATTERNS Law 1) and surfaces an append-only `reblessed` flag on `model_sample`
  in `--json` plus a `re-blessed` clause on the reuse provenance line. This is the
  `model_locks` surface's safe-reconcile path that `DEVELOPMENT.md`'s schema-
  evolution gate requires (never "delete the file and re-draw", which silently
  changes the panel). No new dependency.

Closes: lock-rebless

- **Output-modality filter for sampled rosters**
  (`solvers.sample.where.output_text_only`): drop image/audio/video **generators**
  from a `pricing-table` draw. The runnable-text gate only checks that a model
  *can* emit text (`"text" in output_modalities`), so a model that emits text
  **and** image/audio still qualifies and pollutes a closed-text frame (10 of
  ~309 drawable OpenRouter ids today: the `*-image` / `gpt-audio*` models).
  `--refresh-pricing` now persists the raw `output_modalities` list on
  `ModelPrice` (additive, optional — `None` for the seed / pinned tables, no
  schema bump); the opt-in `where.output_text_only: true` keeps only models whose
  output set is exactly `{"text"}` (and `false`, the symmetric inverse, keeps only
  the generators), parallel to the input-side `where.multimodal`. Opt-in rather
  than a default universe gate — a multimodal-*output* model is a legitimate
  target for a study that *means* to sample it, unlike a non-reproducible alias.
  Roster-only (rejected for list/file universes, which are already curated);
  enters the `model_locks.json` `where` spec (a changed value fails loudly — clear
  the lock to re-draw). Builds on `model-sampling`. No new dependency.

Closes: sample-output-modality

- **Two-stage rubric materialization** (`rubrics:` with `materialize:`): grade
  with a per-item rubric *generated from the item's reference and frozen* — the
  ProofBench/RefGrader protocol — instead of a single static rubric template. A
  new top-level `rubrics:` mapping (parallel to `graders:`) declares a named
  rubric with a `grade_template` (which receives a `{rubric}` placeholder) and a
  `materialize: {model, template, max_tokens?, reasoning_effort?}` block; a
  `facets.rubric` name found there materializes, while a bare/`builtin:` name
  stays a plain template, byte-identical to today. Before grading, a pre-pass
  runs the materializer model **once per item** (rendering the build template
  over `{input, target, grading_scheme, id}` — never `{solution}`), content-hashes
  the result, and stores it in a new `materialized_rubrics.parquet`; every judge
  call for that item then reuses the frozen rubric verbatim — shared across
  graders, solutions, replications, and resumed runs (reuse is $0). The grade
  **condition id carries the materialize spec** (materializer model + build-
  template hash), so a changed build template or model re-derives, like changing
  a rubric does; the per-item rubric text + hash live in the store (the
  reproducibility record, copied into `export --snapshot`). Materialization is a
  **design declaration** (it changes condition ids). Its spend is **estimated in
  the grade stage and covered by the single existing money gate** — no new
  command, no second prompt: `estimate`/`grade` show the per-item materializer
  calls (priced under the materializer model, surfaced for unpriced detection),
  resume-aware so only un-frozen items cost. `grade` prints a `materialized: N
  rubrics (model) · $X · M reused` summary line and exposes append-only
  `materialized_rubrics` / `materialized_reused` / `materialize_usd` /
  `materialize_empty` / `materialize_model` on `GradeResult`. A new coded hint
  `empty-materialized-rubrics` fires when the materializer returns no text (an
  item graded against a blank rubric). The build prompt is study-authored — no
  built-in materialize template ships. The split-rubric cache layout composes
  (the materialized rubric is solution-independent, so it caches in the shared
  head). No new dependency.

Closes: rubric-materialization

- **Pre-flight cost-lever status line.** `estimate`, `generate`, and `grade` now
  print a `cost levers:` line stating whether batch, native batch routing,
  prompt caching, and the local response cache are engaged — with a one-clause
  reason for each that is off (e.g. `batch off (dev policy) · native-routing off
  (needs batch plan) · prompt-cache provider-default (auto, reps=1) ·
  response-cache on ($0 replays on re-run)`). Previously levers were announced
  only when *active*, so a `--policy dev` run — which turns most of them off —
  said nothing, leaving operators unable to tell what was on.

- **Composite / templated item ids** (`mapping.id`): pool datasets that share a
  natural key (a per-split row index, a per-release problem number repeated each
  year) into one crossed study without tripping the global item-id uniqueness
  guard. `mapping.id` now accepts a **list of segments** joined with `:`
  (multi-column natural keys) or a **template** segment — any segment containing
  `{` is rendered over the record's columns plus a synthetic `{dataset}` token
  (the dataset basename), so `id: ["{dataset}", problem_idx]` on `org/set_2026`
  yields `set_2026:6` (the template-string spelling `"{dataset}:{problem_idx}"`
  is equivalent). A single plain column is **byte-for-byte unchanged** — the id
  is the cross-store join key, so existing studies' ids never move. An unknown
  `{placeholder}` or a missing column fails loudly listing the valid names, a
  malformed segment (unbalanced brace) is rejected, and the duplicate-id guard
  now names the composite knob as the fix. `mapping.id` is a design declaration
  (always explicit; never enters condition ids).

Closes: composite-item-id

- **Candidate-model sampling** (`solvers.sample`): draw the model facet from a
  universe instead of listing it, with the draw recorded so the study card can
  attest how models were chosen. `solvers.sample` (mutually exclusive with
  `solvers.models`) takes `n`, `seed`, an optional `stratify_by`, and a
  `universe` — one of `pricing-table` (the `openrouter/*` roster itemeval's
  pricing table already tracks; refresh it with `--refresh-pricing` to sample
  today's roster), a file of model ids (one per line), or an inline list.
  `stratify_by` balances the draw across `provider`, or — for a `pricing-table`
  universe — `reasoning` / `multimodal` / `price_tier` / `context_tier`. A roster
  universe can be narrowed with `where:` — a `provider` allowlist,
  `max_output_usd_per_mtok` and `min_context_length` ceilings/floors, and
  `reasoning` / `multimodal` booleans (rejected for list/file universes, which
  are already curated). The draw is deterministic given `(seed, sorted
  universe)`, optionally stratified (Hamilton apportionment), and pinned in a new
  `model_locks.json` (sibling of `dataset_locks.json`): later runs reuse the
  frozen draw, a drifting roster only **warns** (the draw stands), and a changed
  sample spec **fails loudly** (clear the lock to re-draw). Provenance surfaces
  in every rendering (UX-PATTERNS Law 1/6): a `models: sampled N of M …` line on
  estimate/generate/grade/status, a `model_sample` object on each command's
  `--json` (append-only on `Estimate`/`GenerateResult`/`GradeResult`/the status
  report), a `model_sample` block in the run manifest, and a Design line +
  front-matter in `STUDY_CARD.md`; `export --snapshot` copies `model_locks.json`.
  The `pricing-table` universe is restricted to OpenRouter's **runnable text
  models** — text in and out, with generation parameters — so it never samples
  embedding or meta/router entries, and **excludes free (`$0` output) models**
  (rate-limited `:free` endpoints, not representative of the paid models a frame
  samples; name one directly in `solvers.models` if you want it — it still
  prices). `--refresh-pricing` now records the per-model
  roster metadata these features read (`ModelPrice.text_model` / `reasoning` /
  `multimodal` / `context_length`), so a `pricing-table` sample needs a refreshed
  table (the empty-universe error says so). Tier edges are fixed: price (output
  $/Mtok) `free` ≤`1` ≤`10` `high`; context `short` ≤32k ≤128k ≤400k `xlong`.

Closes: model-sampling

- **Expressive model-sample composition** (`solvers.sample`): three levers on the
  candidate-model draw for balanced, current, purposive panels —
  - **Recency.** `where.released_after: "2025-01-01"` keeps only models released
    on/after an **absolute** date (never wall-clock age, so a pinned table draws
    identically), and `stratify_by: recency` balances the draw across release
    **years** (UTC). Both read a new `ModelPrice.created` release timestamp that
    `--refresh-pricing` now captures from OpenRouter; undated models are dropped
    by `released_after`, and a `recency` draw against a table that predates
    `created` fails loudly pointing at `--refresh-pricing`.
  - **`allocation: equal | proportional`** (default `proportional`, unchanged):
    `equal` balances `n` across strata instead of by stratum size, so
    large-roster vendors stop dominating and small ones stop dropping to zero
    (capped at each stratum's available models; requires `stratify_by`).
  - **`include: [ids]`** pins must-have models, counted against `n`; the seeded
    draw fills the rest. Pins bypass `where` and universe membership (purposive).
    When also stratified, pins **count toward** their stratum's balanced share
    (not added on top), so pinning a vendor doesn't over-represent it; pins
    exceeding a stratum's share are all kept and the remainder rebalances.
  Provenance surfaces wherever model-sampling already reports it: the
  `models: sampled N of M …` line gains `(equal)` / `K via include` clauses, the
  `model_sample` object on each command's `--json`, the run manifest, and
  `STUDY_CARD.md` gain `allocation`/`include`. `model_locks.json` pins the new
  knobs in its spec (a changed value fails loudly — clear the lock to re-draw).
  `ModelPrice.created` is an additive optional field (no pricing-table schema
  version). Builds on `model-sampling`.

Closes: model-sample-composition

- **Exclude model ids from a sample draw** (`solvers.sample.exclude`): a
  top-level `exclude: [ids]` list drops exact model-ids from the universe before
  the draw — the inverse of `include`, and like `include` it works for **every**
  universe type (`pricing-table`, file, inline list), not only the roster `where`
  narrows (an id blocklist is not roster metadata). The case it unblocks is
  rater–object independence: remove the judge model-ids so a judge can't be drawn
  as a solver. Absent ids are a no-op; an id cannot be both included and excluded
  (rejected at load, with duplicate/empty-string entries); `exclude` enters the
  `model_locks.json` spec (a changed value fails loudly — clear the lock to
  re-draw) and the provenance surfaces wherever `include` does (a `K excluded`
  clause on the `models: …` line, an `exclude` array on each command's `--json`,
  the run manifest, and `STUDY_CARD.md`). A **design declaration**. Pairs with
  the now non-free `pricing-table` roster (see model-sampling above), which
  together replace an earlier `where.free` idea. Builds on `model-sampling`.

Closes: sample-exclude

- **Expected (calibrated) cost projection alongside the ceiling**: `estimate`
  (and the `generate`/`grade` pre-gate line) now reports a second, **expected**
  figure next to the deliberate upper bound. The ceiling assumes every
  generation emits `max_tokens`, every judge call `grader_max_tokens`, and stubs
  each un-generated solution at `4 × max_tokens` chars; the expected pass swaps
  each for an **observed mean** read from the stores (`output_tokens` on
  solutions/gradings, real solution length) — no new model calls. After a cheap
  `--policy dev` pilot it predicts the real bill instead of the 2–3× ceiling.
  Per-model means use a coverage fallback by sample count — own mean (≥ K
  samples) → reasoning-group mean (`ModelPrice.reasoning`) → global pooled mean →
  ceiling — recorded in a `calibration` block (calibrated / group / pooled /
  uncalibrated model counts + observed-row count) so a *borrowed* estimate is
  never shown as *measured*. **The money gate is unchanged**: `usd` /
  `remaining_usd`, `confirm_above_usd`, and `max_usd` keep comparing the ceiling
  (UX-PATTERNS Law 2 — the gate is never driven by an under-estimate); the
  expected figure is informational. The projection line gains an always-on
  `ceiling: output at max_tokens` clause and an `expected ~$X (calibrated from N
  observed …)` line when calibratable. New append-only fields:
  `expected_usd` / `expected_remaining_usd` / `calibration` per `StageEstimate`
  (in `estimate --json`), `expected_estimate_usd` on `GenerateResult` /
  `GradeResult` and the gate-stop document. At cold start (no observations yet)
  the new coded hint `estimate-is-ceiling` fires on `estimate`/`generate`/`grade`,
  pointing at `--policy dev`. No new config knob, no new dependency.

Closes: expected-cost

- **Native-provider batch routing** (`budget.prefer_native_batch`, default off):
  under a batch plan, route OpenRouter-sampled models to their native provider
  API so the dominant stage actually receives the ~50% batch discount —
  OpenRouter has no batch API, so an `openrouter/anthropic/*` judge otherwise
  forgoes it. Opt-in and **never silent** (UX-PATTERNS Law 1): switching the
  serving endpoint can change outputs, so it stays an explicit optimization knob
  (like `provider_routing`). A model routes only under a batch plan with the knob
  on, an inner provider whose native API batches
  (`anthropic`/`openai`/`google`/`x-ai`→`grok`/`together`), and a native API key
  in the environment; the decision is made once at prepare time (resume-safe),
  all-or-nothing per model. The sampled `openrouter/*` id stays the model's
  **scientific identity** everywhere it is one — condition ids, `model_locks`,
  the `model` column — while the native id is recorded as the **execution id**:
  costs are read under the sampled id (the roster id the pricing table carries),
  only the batch-discount eligibility and the served endpoint follow the native
  id. Provenance (Law 1/6): a `native batch routing: N model(s) → native API …`
  line on estimate/generate/grade, `routed_models` on `GenerateResult`/
  `GradeResult`, `execution_model`/`routed` on each manifest
  `endpoints_effective` entry, and the ledger `provider` column = the billing
  (native) provider. New coded hint `native-batch-available` fires when a batch
  run leaves the lever unused. Also an **estimate-time dual projection**: for
  each routable model `estimate` shows native-batch vs OpenRouter-cache
  **expected** cost (remaining scope) with the cheaper verdict — `routes` on
  `Estimate` (each a `NativeRoute` with `batch_usd`/`cache_usd`/`cheaper`) and a
  comparison block in the text rendering — so the batch-vs-cache choice is
  visible per run. New append-only fields; no new dependency. The money gate is
  unchanged (it already compares the discounted projection).

Closes: native-batch-routing

### Changed
- **Recovery-aware run identity: `experiment_id` + `attempt` replace `run_id`.**
  itemeval kept two artifact classes under two identity rules: **data**
  (solutions/gradings) is content-keyed, so re-runs **converge** into one store;
  **provenance** (the per-invocation `run_id`, its manifest, its `.eval` pile)
  was invocation-keyed, so re-runs **forked** — a second `run_id`/manifest for
  what is *one experiment*, with nothing marking them as attempts of the same
  intent and a `solutions.run_id` column that mixed ids after recovery. Flaky
  endpoints make recovery the *common* path, so the fragmentation bit constantly.
  Run identity is now **experiment-scoped with attempts**: `run_id` is replaced
  by a deterministic `experiment_id` (`sha256(config_digest : study : stage)[:12]`)
  plus an `attempt` integer, across the solutions / gradings / ledger / log_index
  / materialized-rubrics stores, the manifests, and the long export (the export's
  `gen_run_id`/`grade_run_id` columns become `gen_experiment_id`+`gen_attempt` /
  `grade_experiment_id`+`grade_attempt`). The `config_digest` is the **semantic**
  config — re-parsed through the pydantic model, identity-bearing fields only — so
  comments, whitespace, key order, and pure execution/cost knobs (`output_dir`,
  `cache`, the whole `budget` block, `provider_routing`, `cache_prompt`) never
  change identity; `config_sha256` is redefined to this digest. A re-run of an
  **unchanged config recovers the same `experiment_id`** (next attempt, converging
  into existing results — recovery never re-pays a completed cell); a real design
  edit forks a new experiment; `--new-run` on `generate`/`grade` forces a fresh
  one. Grown items / roster drift under an unchanged config stay a **soft warning,
  not a fork**. `generate`/`grade` announce the decision (`recovery: attempt 3 of
  experiment a7b3c9d2 — converging into existing results`, or `experiment: … ·
  attempt 1 (new)`) and surface append-only `experiment_id`/`attempt`/`run_kind`
  on `GenerateResult`/`GradeResult` (`--json`). A persisted **per-experiment
  index** (`manifests/experiments/<stage>.<experiment_id>.json`) rolls up an
  experiment's attempts + the current one; `status` surfaces it
  (`experiments: a7b3c9d2 (generate) — 3 attempts, current a3`) and gains an
  append-only `experiments` array. Builds on `recoverable-harvest` (a recovered
  `.eval` is projected back through the same row builders, byte-identical to a
  live row) and gates the future mid-run tracker, which reads run state as
  experiments-and-attempts. No new dependency.

  **Study migration.** This is a non-additive rename of a study-facing column
  across five parquet stores + the manifests + the export. itemeval does not read
  old-schema stores: a command meeting one (a store with `run_id` and no
  `experiment_id`) fails loudly with a briefing rather than crashing opaquely.
  The safe, **result-preserving** migration is a clean break — delete
  `manifests/`, `logs/`, and the parquet stores under the study, then re-run:
  content keys are unchanged and cached generations replay at ~$0, so results are
  identical. (Per the pre-1.0 carve-out in `DEVELOPMENT.md`.)

Closes: recovery-run-identity

- **Conditions now run concurrently within a stage.** `generate` and `grade`
  previously called `inspect_ai.eval()` once per condition in a serial loop, so
  model #2 waited for model #1 to finish. Each stage now builds every
  condition's task — each carrying its own model — and runs them in a single
  eval bounded by the number of distinct execution models, so independent models
  execute in parallel (a single-model stage is unchanged). Per-sample
  retry/error semantics are identical (`retry_on_error`/`fail_on_error` still
  apply per sample); inspect isolates a failing model to its own condition while
  the others proceed, and a model-construction failure is still reported per
  condition. Each stage's logs now share one `logs/<stage>/` directory (was
  `logs/<stage>/<condition_id>/`); readback is unaffected (keyed by condition in
  the parquet stores). Generate/grade/estimate also print a coarse pre-flight
  wall-clock estimate (`~Nm at concurrency K` — rough, seeded from this study's
  observed latency when available, else a default prior), so a run can be sized
  before it starts.

Closes: parallel-conditions

### Fixed
- **`solvers.attempt_timeout` no longer retries a stalled call forever.** inspect
  abandons a timed-out attempt and retries it "according to `max_retries`" — but
  with neither `max_retries` nor a total timeout set (itemeval set neither), its
  stop condition is `stop_never`, so a genuinely hung backend timed out and
  re-issued **indefinitely** (each attempt waiting the full `attempt_timeout`),
  burning wall-clock and money with no progress. Now, when `attempt_timeout` is set
  and `max_retries` is not, the attempt cap defaults to **2** so the call gives up
  and the cell is left as an honest error (a later re-run re-attempts it, likely on
  a fresh backend). A new pass-through knob **`solvers.max_retries`** /
  **`graders.<name>.max_retries`** sets the cap explicitly (it also bounds
  transient-HTTP-error retries); both are operational knobs (non-identity, excluded
  from the response-cache key). Applies to `generate`, `grade`, and the reroute
  path.
- **Unmapped provider finish-reasons are no longer indistinguishable.** inspect's
  `as_stop_reason` collapses any provider `finish_reason` it doesn't recognize
  (including `error`) to `stop_reason="unknown"`, so a provider soft failure was
  indistinguishable from a genuinely unmapped stop. The new
  `native_finish_reason` column (see `provider-finish-capture` under Added)
  recovers the raw reason where the provider supplies it. The upstream flatten
  itself is unchanged (root is inspect); this captures the raw value as a wrapper
  column. Closes the KNOWN-ISSUES "unmapped provider finish-reasons collapse to
  `unknown`" defect.
- **The live-run heartbeat no longer prints its closing line twice.** When the
  final sample's `SampleEnd` emitted an unthrottled heartbeat, `tracking()`'s
  closing force-emit repeated the identical terminal line (e.g. `… · 6/6 (100%)`
  appeared twice on stderr at the end of a `generate`/`grade` run). The closing
  emit now fires only when the terminal `ended` count was not already shown.
  Cosmetic — the heartbeat carries no fact of record (UX-PATTERNS Law 8).
- **A pinned `solvers.sample` study no longer bricks after a package update that
  grows the sample spec.** The model-sample lock (`model_locks.json`) compared its
  stored spec against the current one by raw-dict equality, so an itemeval update
  that added an *additive* `solvers.sample` field — e.g. `where.output_text_only`
  (default `None`), or the `allocation` / `include` / `exclude` knobs — made a
  pre-update lock mismatch, and **every** command (estimate / generate / grade /
  export / status) exited with `solvers.sample spec changed … clear
  model_locks.json` though nothing in the study changed. Clearing the lock was the
  wrong remedy — it would re-draw a *different* panel over the pinned one. The two
  specs are now compared **normalized through the current schema**, so an absent
  additive field defaults in and compares equal, while a genuine change (n / seed
  / stratify_by / where / …) still fails loudly as before. The written lock format
  is unchanged.
- **Read-only commands no longer brick when the sample spec genuinely changed.**
  Building on the normalized comparison above: when `solvers.sample` is pinned and
  the config's spec really did change since the pin (n / seed / stratify_by / where
  / …), `estimate`, `status`, and `export --snapshot` now **warn and proceed on
  the pinned panel** instead of exiting — a pinned study can always be inspected,
  audited, or snapshotted, even mid-edit. The hard stop stays on the draw/write
  path (`generate` / `grade`), where running a panel other than the pinned one
  would mix results (clearing the lock there re-draws a *different* panel). The
  text rendering prints `warning: solvers.sample spec differs from
  model_locks.json — showing the pinned panel; clear model_locks.json to re-draw
  at the current spec`, and an append-only `spec_drift` flag rides `model_sample`
  in `--json`. Python: `prepare_study(allow_spec_drift=True)`.
- **Non-reproducible routing aliases are no longer drawable in a `pricing-table`
  sample.** OpenRouter lists `-latest` / `:latest` and `~`-prefixed *variant
  routes* (e.g. `openrouter/~anthropic/claude-opus-latest`) that resolve to a
  *moving* target — so a draw could pin one in `model_locks.json` yet silently
  run a different served model on each run, defeating the lock's reproducibility
  guarantee (the live roster carries ~10 such ids). The default `pricing-table`
  universe now drops them before the draw, exactly as it already drops free
  (`$0` output) models: they stay in the pricing table (so `lookup_price` still
  prices one named directly in `solvers.models`), they are just not *drawable*.
  Name one explicitly in `solvers.models` if you accept the non-reproducibility.
- **A small-context model in a mixed roster no longer 400s on every call.** A
  global `solvers.max_tokens` larger than a model's context window made every
  request to that model a guaranteed HTTP 400 (`input + max_tokens > context`),
  so small-context models in a sampled roster produced only errors while the
  rest of the roster ran. `generate` now clamps each condition's `max_tokens` at
  request time to fit the model's own `context_length` (from the pricing/roster
  table), leaving a margin for the estimated input; large-context models are
  untouched (byte-identical). The clamp is **runtime-only** — the condition id
  keeps the requested design value, so store keys never move and never churn
  when the roster's `context_length` refreshes — and the adjusted value is
  recorded as the per-row effective `max_tokens` and announced in the run summary
  (`warning: max_tokens clamped to fit context window for N model(s) …`, carried
  on `warnings[]` under `--json`). A model with no known `context_length` is left
  as-is. Grade is unaffected (a single judge model, not a roster).
- **The `max_tokens` clamp now fits the *routed* endpoint window, not just the
  model-level max.** OpenRouter's `context_length` (in the pricing table) is the
  maximum across all providers serving a model, but a request can be routed to a
  floor provider with a smaller window — so the clamp above could still let a
  guaranteed HTTP 400 through. Reproduced live: `openrouter/qwen/qwen-2.5-7b-instruct`
  advertises `context_length` 131072 yet the served endpoint capped at 32768
  (`157 + 32768 > 32768` → 400, every call errored). `generate` now fetches each
  roster model's per-endpoint windows from OpenRouter
  (`/models/:slug/endpoints`, **only** for the models about to run) and clamps
  `max_tokens` against the **smallest** endpoint window — the one any routing can
  land on. The lookup is cached in `~/.cache/itemeval/endpoints.json` (warm runs
  cost zero calls), announced in the run summary (`endpoint windows: fetched N
  from OpenRouter — cache dir: …`; `endpoint_windows_fetched`/`_reused`/
  `endpoint_cache_dir` under `--json`), and degrades to the model-level value if
  the fetch fails (never blocks). Still runtime-only — the condition id keeps the
  requested design value. `Closes: endpoint-context-clamp`
- **`grade` and the grade cost estimate now scope to the current config's gen
  grid.** Both previously selected gradable solutions by item (and epoch) alone,
  so a solution still in the append-only store but produced by a gen-condition no
  longer in the config — e.g. after a config change rehashed the condition ids,
  stranding the previous roster — was (re-)graded and (re-)priced. The fallout
  was silent overspend, cross-roster mixing in `gradings.parquet`, and a grade
  `remaining_usd` that grew with the store and could exceed the full-grid ceiling
  `usd` (the very figure the money gate enforces on) while ignoring `--policy`.
  `grade` and `estimate` now count only solutions whose gen-condition is in the
  current grid — the scope `status` already used, so the runner, the estimate,
  and the status completion matrix finally agree. The decoupled workflows are
  unchanged: grading with the same config (add a grader/rubric later, or generate
  today / grade tomorrow) still scores every stored solution, because the grid
  ids match — only solutions orphaned by a config change are excluded. To grade
  an old roster, restore the config that produced it.
- **Real (non-mock) models no longer crash instantly under the new concurrent
  stage execution.** With a stage's conditions now running in a single eval
  (parallel-conditions), each condition carries its own resolved model and
  inspect reads `task.model.model_args` per task — so the model must be an
  inspect `Model`. For the common case of a model with no extra request args
  (no provider routing / cache keys), `resolve_model` returned the bare model-id
  *string*, so every `generate` and `grade` condition failed at $0 with
  `AttributeError: 'str' object has no attribute 'model_args'` — no real-model
  run could start. `resolve_model` now always returns a `Model` (its contract is
  narrowed accordingly; the dead string branch is gone). A regression in the
  unreleased parallel-conditions change, missed because every test drove the
  concurrent path with `mockllm/*` ids, which always resolved to a `Model`.
- **An errored or empty generation no longer crashes the whole `generate`
  stage.** A sample that errored (or completed with no choices) carries a
  `ModelOutput` whose `choices` list is empty; reading `stop_reason` off it
  raised `IndexError`, so a single bad solver aborted log→row conversion for the
  entire run (a multi-model screen could never finish if any one model erred).
  The `stop_reason` extraction now guards for empty `choices` like its siblings
  already do — an errored row becomes `{error: <msg>, solution: None,
  stop_reason: None, …}`, exactly what `status`/`export` expect.
- **A schema-stale pricing cache no longer dead-ends a `pricing-table` sample.**
  A cached `~/.cache/itemeval/pricing.json` written before the roster-metadata
  fields (`text_model`/`reasoning`/`multimodal`/`context_length`) existed reads
  as *fresh* by its `updated_at` stamp, so `budget.pricing_max_age_days` could
  not see it was stale — and a `solvers.sample` with `universe: pricing-table`
  then found zero runnable models and aborted with a `ConfigError`. `prepare`
  now detects the missing roster metadata and refreshes the table once (the
  existing `pricing: … — just refreshed from OpenRouter` provenance line
  announces it); offline, it still falls through to the same actionable error.
  Only the default (unpinned) pricing path auto-recovers — a `budget.pricing_path`
  pin is honored as-is.
- **`openrouter-unpinned-cache` hint no longer misfires under native batch
  routing.** When `prefer_native_batch` routes an `openrouter/anthropic/*` model
  to its native batch API, the call never goes through OpenRouter, so the
  "ran cached via OpenRouter without `provider_routing`" caveat does not apply —
  but the hint fired anyway (keyed on the sampled `openrouter/*` id regardless of
  the active route), in both `generate` and `grade`. The hint now excludes routed
  models. Surfaced by the native-batch-routing live smoke.
- **Agent-Guide no longer steers agents into a blind paid run.** The guide's
  "prefer `--json` on every command" advice contradicted its own quickstart:
  `--json` silences both the pre-flight ETA line and the live progress display,
  so following it on `generate`/`grade` left a long paid run with no visible
  progress. The `--json` guidance now scopes to the no-cost commands
  (`estimate`/`status`/`export`) and points agents to run the paid stages
  without it (parsing the printed summary + `manifest:` path for structured
  results instead).

## [0.2.0] - 2026-06-12

This release is largely about **cost** and **honest accounting**: provider
prompt-cache scheduling that can halve a judge bill, a cache-aware estimator
and money gate that quote the discounted, delta-aware figure you will
actually pay, re-observation over time (waves) and immutable snapshots for
reproducibility, drift/provenance detection, and a full agent-facing surface
(`--json` on every command, a hint framework, Python-side budget consent).

### Added
- **Cache-aware execution scheduling** (validated in a live pilot): maximize
  provider prompt-cache discounts (~75–90% off repeated input tokens).
  - Cache observability: `generate`/`grade` per-condition summaries report
    provider cache reads/writes and hit rate; `ConditionRunReport` gains
    `cache_read_tokens` / `cache_write_tokens` / `cache_hit_rows`.
  - `graders.<name>.split_rubric`: render the rubric as a system message
    (shared head: rubric + problem + scheme + reference) plus a user message
    (the solution), placing the provider cache breakpoint exactly at the
    shared/varying boundary. In the validation pilot this **halved the judge
    bill** on an Anthropic judge via OpenRouter (78% input-side discount;
    the monolithic layout cached nothing). Changes grade condition ids when
    enabled.
  - `solvers.split_prompt`: the analogous split for solver prompts at
    `{input}` (static template head → system message). Required for
    Anthropic-style caching of generate calls through OpenRouter; 66–78%
    input-side discount on replications in the pilot.
  - `solvers.cache_prompt` (`auto`/`on`/`off`, default `auto` = on when
    replications > 1): provider prompt caching for the generate stage.
  - `budget.cache_schedule` (`auto`/`off`): warm-then-fan-out gating of
    same-prefix call groups (leader writes the cache, followers read). Also
    routes byte-identical duplicate judge calls into inspect's local response
    cache ($0). Judge datasets are now sorted by item so same-prefix calls
    are adjacent.
  - Pricing: cache write defaults to $0 for non-Anthropic-style models
    (OpenAI/Gemini/DeepSeek writes are free; Anthropic keeps the 1.25×
    surcharge); `--refresh-pricing` now also pulls per-model cache read/write
    rates from OpenRouter.
- **Cache-aware estimator**: when a run will be scheduled into provider
  prompt caches (cache scheduling on, not batch, provider minimum known and
  met), projections model the same per-group split the runtime schedules —
  one leader writes the shared prefix (1.25× surcharge Anthropic-style, plain
  input on free-write providers), followers read it at the cache-read rate.
  `ConditionEstimate` gains `cache_read_tokens` / `cache_write_tokens` /
  `cache_discount_usd` (negative when projected writes exceed reads — tiny
  Anthropic groups are shown costing *more*, honestly); `StageEstimate` sums
  them and adds `remaining_cache_discount_usd`. **`usd` and `remaining_usd`
  are now the discounted figures** — the money gate and `max_usd` cap
  therefore compare the discounted projection. Delta-aware estimates apply
  the split to remaining groups only (a group with ≥1 completed row is warm:
  followers-only). The projection line states the discount when nonzero
  (`projected generate cost: $4.10 (includes −$1.30 provider prompt-cache
  discount; confirm_above_usd: $5.00)`), and `estimate`'s stage lines do the
  same. Best-case projection by design (assumes scheduled hits); the post-run
  `cache-zero-reads` hint is the corrective feedback loop.
- **OpenAI keyed caching, automatic** (no new knob): when cache scheduling is
  active (`budget.cache_schedule` on, not batch), direct `openai/*` requests
  carry `prompt_cache_key: itemeval/<study>/<condition_id>` — stable across
  runs and phases of the same study+condition, so a pilot warms the full run
  and routing affinity holds — plus `prompt_cache_retention: "24h"`, which is
  surcharge-free on OpenAI pricing (verified 2026-06-12). Names pass through
  verbatim. `openrouter/openai/*` is excluded (OpenRouter does not document
  forwarding these fields); cache effectiveness stays observable via the
  existing cache-read columns and the `cache-zero-reads` hint.
- **`provider_routing`** (on `solvers:` and per grader spec): a verbatim
  OpenRouter provider-routing object (e.g. `{order: [anthropic],
  allow_fallbacks: false}`) sent with every `openrouter/*` request — pins the
  upstream so cached runs don't silently land on a marker-ignoring host
  (Bedrock/Vertex, the live cache_read=0 footgun). Pass-through, never
  renamed; never enters condition ids (the manifest's config echo records the
  requested routing, `endpoints_effective` what answered). Setting it in a
  section with no `openrouter/*` model warns (inert knob — never blocks).
  New hint `openrouter-unpinned-cache` fires when an `openrouter/anthropic/*`
  model runs cached without it. Estimator stage projections now carry their
  stage-relevant warnings (`StageEstimate.warnings`, append-only); `grade`
  now relays grade-stage estimator warnings pre-gate like `generate` always
  did. The `model_factory` callback (Python API) now receives a third
  `model_args` dict argument.
- **OpenRouter upstream provenance**: for `openrouter/*` models, the
  manifest's `endpoints_effective` entry now records `upstream` — the host
  OpenRouter actually routed to (the response's `provider` field, e.g.
  `"Anthropic"` vs `"Amazon Bedrock"`; distinct values within one run are
  comma-joined, None when no recorded response carried the field). Verifying
  a `provider_routing` pin is now a one-look manifest check instead of
  reading raw eval logs, and a change of upstream across runs of the same
  model raises an endpoint-drift warning naming `provider_routing` as the
  fix — upstreams differ in caching and pricing (Bedrock ignores cache
  markers), so a silent reroute is a silent price change.
- **`anthropic-openrouter-no-split` hint + honest projection for the layout
  it flags** (estimate-time): an Anthropic-style model running *monolithic*
  prompts through OpenRouter can never engage the provider cache — inspect's
  openrouter provider places no `cache_control` breakpoint on a single
  string-content user message (verified live 2026-06-12 on inspect 0.3.239:
  `cache_write=0` on every call at full price). The estimator no longer
  projects a discount for these conditions — the money gate compares the
  full price they will actually cost — and the new hint names the model and
  the fix (`split_prompt` / `split_rubric`, or the direct API). Direct
  Anthropic monolithic and split-via-OpenRouter projections are unchanged
  (both verifiably cache).
- **`split-head-below-min` hint** (estimate-time): when `split_prompt` /
  `split_rubric` is on but the shared head's token estimate (chars/4) falls
  below the provider's minimum cacheable prefix, one hint line names the
  count, model, and minimum — the silent-no-op gotcha observed live (e.g.
  `7/40 judge heads under anthropic/…'s ~4096-token cache minimum`). Backed
  by a new per-provider minimums table
  (`itemeval._endpoints.MIN_CACHEABLE_PREFIX_TOKENS`, model-aware for
  Anthropic and Gemini, numbers checked 2026-06-12 against provider docs;
  providers documenting no minimum are omitted — never guessed). Estimate
  stage projections carry their stage's hints (`StageEstimate.hints`,
  append-only), and `generate`/`grade` now surface estimate-time hints too —
  merged into the run's hints, and emitted with the stop document on a gate
  stop.
- **Delta-aware estimates**: each stage projection now carries
  `remaining_usd`/`full_usd`/`remaining_calls`/`completed_cells`/
  `total_cells`/`rows_replaced` alongside the unchanged `usd` (full grid,
  append-only). `generate`/`grade` print
  `projected … cost: $4.10 remaining of $11.30 full grid (63% complete)`
  on partially complete studies, and run manifests record both figures
  (`estimate_usd` = remaining, new `estimate_full_usd`).
- **Replacement statement at the money gate**: when a planned run would
  overwrite existing rows (`--force`, epoch extension, `on_empty: rerun`),
  the pre-gate block states `this run replaces N existing rows (…)` as part
  of the single confirmation; `rows_replaced` rides the estimate and run
  JSON.
- **Python-surface consent: `max_usd=`** on `run_generate`/`run_grade` —
  when the stage's *remaining* projection exceeds it, the run raises the new
  `itemeval.BudgetExceededError` **before any API call**; never prompts
  (UX-PATTERNS Law 3). The config's `budget.max_usd` hard cap is now
  enforced on the Python path the same way, so the cap holds on every
  surface. `BudgetExceededError` and `ItemevalError` are new public exports;
  the `import itemeval` docstring no longer tells users to gate themselves.
- **Waves — re-observation over time** (`generate --wave LABEL` /
  `run_generate(prep, wave=...)`, and the matching `grade --wave`): re-run
  the same design scope as a new **epoch block** (wave *w* with
  `replications: R` occupies epochs `w·R+1…(w+1)·R`), keeping both
  observations — the substrate for drift / model-downgrade detection. New
  waves are new store keys (never replacements); the offset eval runs with
  the local response cache **off** (announced) so re-observations are fresh
  draws; mid-wave crashes resume by label. Schema change (minor bump):
  solutions/gradings/export gain additive `wave`/`wave_label` columns (old
  stores read as wave 0, no rewrite); the ledger gains `epoch_offset`;
  manifests and run results record `wave`/`wave_label`/`epoch_offset`.
  `status` reports per-wave completion (generate and graded counts) only
  when >1 wave exists; the main completion matrix stays scoped to the
  current grid at wave-0 scope on both sides of done/expected, so wave or
  drift-stranded rows can never show >100%. Substrate:
  `epochs_to_run` (epoch-range-aware resume; `items_to_run` now delegates to
  it) and `resolve_wave` in the solutions store;
  `build_generate_task(epoch_offset=)`.
- **Snapshots** (`export --snapshot NAME` / `export_study(cfg,
  snapshot="NAME")`): freeze an immutable named copy of the just-written
  export under `export/snapshots/NAME/` — tables, `dataset_locks.json`,
  every manifest covering included rows, `snapshot.json` (run ids, counts,
  spend), and a `STUDY_CARD.md`. Existing names are refused (exit 2);
  snapshots are never read by any compute path. `status` lists snapshots
  (text line + `snapshots[]` in JSON); export JSON gains `snapshot` /
  `snapshot_path`.
- **STUDY_CARD.md**: a self-describing record written into every snapshot —
  versioned YAML front-matter (`itemeval_study_card: 1`) plus Design /
  Execution (incl. `served_model` per condition) / Results (descriptive) /
  Costs / Reproduce sections, every number derived from existing stores.
- **Drift warnings** on `generate`/`grade` (one line each in the summary
  block; `warnings[]` on the run results — never blocking): *config drift*
  when a facet name matches stored rows but its content hash differs, or an
  unchanged slug maps to a new condition id (changed sampling param) — names
  the facet, the hash change, and the affected row count; *endpoint drift*
  when past manifests recorded inconsistent `served_model` snapshots for a
  model this run uses, or the last run is >30 days old (best-effort proxy).
- **Batch announcement**: when a run goes through a provider batch API,
  `generate`/`grade` print
  `batch: enabled (<providers>) — provider-side jobs created; resume with
  the same command`; run results gain `batch`/`batch_providers`
  (append-only). Best-effort: inspect manages the jobs internally and does
  not expose job ids — none are faked.
- **Local response-cache reuse is announced**: when any calls are answered
  from inspect's local response cache, `generate`/`grade` print one summary
  line (`12 calls answered from local cache ($0) — cache dir: …`); JSON
  parity via `local_cache_rows`/`local_cache_dir` on the run results and
  `local_cache_rows` per condition report (append-only).
- **Dataset provenance announcements** (UX-PATTERNS Law 1): every
  `estimate`/`generate`/`grade`/`status` prints one line per dataset —
  revision, downloaded-vs-reused from the HF cache (with best-effort size on
  first use), and a pin clause when this run wrote `dataset_locks.json`.
  JSON parity via a new `DatasetProvenance` model: `datasets[]` on
  `Estimate`, `GenerateResult`, `GradeResult`, and extended fields on the
  status report's `DatasetStatus` (`split`, `revision_source`, `cache`,
  `cache_dir`, `download_bytes`, `pinned_now` — all append-only).
  `LoadedDataset` carries the same facts for Python callers.
- **Hint framework** (`docs/UX-PATTERNS.md`): commands may end with up to two
  dim `hint:` lines on stderr — one observed fact from this run plus a wiki
  pointer; hints never change behavior and never block. `ITEMEVAL_HINTS=off`
  silences the text rendering; in `--json` the full list always rides as a
  `hints` array on the result (`Estimate`, `GenerateResult`, `GradeResult`,
  `ExportResult`). Initial coded hints (stable, append-only):
  `cache-zero-reads` (same-prefix calls scheduled but no provider cache
  discount engaged), `empty-solutions` (completions with no API error and no
  gradable text), `unpriced-models` (replaces the inline `unpriced models:`
  lines on estimate/export).
- `--json` on `generate` and `grade` (every command now has it): stdout
  carries exactly one JSON document — the run result extended with `pricing`,
  `estimate_usd`, and a `gate` outcome object — and inspect's live display is
  silenced unless `--display` is passed explicitly. A gate stop under
  `--json` still emits a JSON document (projected cost, gate reason, rerun
  command, `hints`) before exiting 3/4, so an agent gets structure even on a
  stop. New JSON keys are append-only; exit codes unchanged.
- `--policy {dev,full-interactive,full-batch}` on
  `estimate`/`generate`/`grade`/`status`: override `budget.policy` for one
  invocation without editing the config — the zero-edit pilot flow
  (`generate cfg.yaml --policy dev`, inspect, `generate cfg.yaml`). Python
  parity: `prepare_study(cfg, policy=...)`. The run manifest and the
  estimate/status JSON record the effective policy and its source
  (`policy_source: "config" | "override"`, append-only).
- `pilot-available` hint: when a paid run with no completed rows for the
  selected conditions hits the money gate, one stderr hint points at the
  `--policy dev` pilot flow; under `--json` it rides the `hints` array,
  including in the gate-stop document.
- Per-run savings report: `export` now reports spend against a plain-API list
  price (every input token at full rate, no batch discount) and breaks the
  savings into a prompt-cache component and a batch-discount component, plus a
  per-provider spend table. Exposed on `ExportResult.cost` (a `CostReport`).
  Local response-cache / resume reuse is not represented (cache hits carry no
  token usage), so the figure covers the prompt-cache and batch discounts only.
- Pricing auto-refresh: `budget.pricing_max_age_days` (default `None` = off)
  refreshes the cached OpenRouter pricing table when it is at least that many
  days old. Best-effort — network/parse failures keep the existing table and
  never break a run; ignored when `budget.pricing_path` pins an explicit table.
- Pricing provenance: `estimate`, `generate`, `grade`, `export`, and `status`
  print which pricing table the dollar figures came from (`source`, age, and
  whether a refresh just ran). Exposed programmatically on `Estimate.pricing`
  and `ExportResult.pricing` (a `PricingProvenance`) and on
  `PreparedStudy.pricing_refreshed`.

### Changed
- **The money gate now operates on the remaining figure** — what the run can
  actually spend; completed work is never re-paid or re-gated. A study with
  a $100 full grid and $1 remaining passes a $5 `confirm_above_usd` without
  prompting; `--force` restores gating on the full selection.
- **The gate never prompts under `--json`** (closes the documented
  UX-PATTERNS gap): proceed under threshold or with `--yes`, otherwise exit
  3 after emitting the JSON document. `check_gate` gains a `machine` flag.
- `export` now states the side effect honestly:
  `export: rewrote export/ — gradings_long.parquet + .csv, ledger.csv
  (disposable view)`.
- The `grade` empty-solutions summary line is now fact-only
  (`empty solutions: 21 excluded from grading [model_length×21] —
  on_empty=skip`); the remediation advice moved to the wiki
  (Error-Handling#empty-completions), pointed to by the `empty-solutions`
  hint.
- Live progress display is now on by default for `generate` and `grade`. The
  `display` argument of `run_generate`/`run_grade` and the CLI `--display` flag
  now default to inspect's `rich` live progress (inline bars; honoring
  `INSPECT_DISPLAY` and degrading off-TTY/Jupyter/background-thread) instead of
  `none`; progress is surfaced through the Python API as well as the CLI. Pass
  `display="none"` (API) or `--display none` (CLI), or set `INSPECT_DISPLAY=none`,
  to silence it.

### Documentation
- New wiki page **Cost Savings**: every saving option in plain language with
  measured price/time trade-offs, defaults, and direct-API-vs-OpenRouter
  guidance; developer-depth counterpart in `docs/COST-OPTIMIZATION.md`.
- Five step-by-step tutorials in the wiki, each a complete runnable use case:
  score a verifiable benchmark (~2¢), grade with an LLM judge, compare models ×
  prompts with replications (+ pandas/mixed-model analysis), add a second
  judge/rubric at $0 generation, and scale up under the budget layer.
- New wiki **Agent Guide**: a contract-style page for driving itemeval from an
  AI agent — command/exit-code contract, hard budget guardrails, standard
  operating procedure, failure-triage table, and a drop-in block for a study
  repo's `CLAUDE.md`/`AGENTS.md`.
- README rewritten value-first: leads with what the data looks like, adds a
  "Who is this for" section and a documentation hub linking the tutorials and
  agent guide.
- `docs/FUTURE.md` (now `docs/BACKLOG.md`): the post-0.1 feature backlog with
  per-feature design notes (motivation, sketch, implementation plan); ROADMAP's
  "Later" section is now a tiered summary pointing at it.
- `docs/UX-PATTERNS.md`: the binding UX contract for development — two
  operators (human/agent), eight laws (no silent side effects, advice never
  acts, native consent, …), the hint framework, a normative side-effect
  ledger, and a nine-question per-feature checklist. Referenced from
  CLAUDE.md, DEVELOPMENT.md, and BACKLOG.md.

## [0.1.0] - 2026-06-10

First public release. Item-level LLM evaluation over any inspect_ai-supported
provider, with a two-stage generate/grade pipeline, long-format item-response
export, and a budget layer.

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
- `solvers.on_empty` policy (`skip` default / `rerun` / `grade`) for completed
  generations that produced no gradable text (empty/blank `solution`, no API
  error — e.g. a reasoning model whose token budget was spent entirely on
  hidden reasoning). Empty no-error completions are a distinct channel from API
  errors (re-attempted) and parse failures (final): `skip` excludes them from
  grading, `rerun` also makes them eligible for regeneration on the next
  `generate`, `grade` sends them to the judge as-is. They are always surfaced —
  `grade` reports the count and stop-reason breakdown, and `status` gains an
  `empty` column — never silently folded into a green "complete".
- Provider/endpoint provenance for cost attribution: `ledger.parquet` gains a
  `provider` column (the inspect prefix of `model`), and run manifests gain
  `endpoints_effective` per condition (`{provider, base_url, served_model}`,
  backfilled after the run) — recording which provider, endpoint, and
  provider-returned model snapshot actually answered. `base_url` is null on the
  provider's default endpoint; a non-null value flags traffic routed elsewhere
  (Azure/proxy/gateway).

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

### Packaging
- Provider-SDK optional extras (`openai`, `anthropic`, `google`, `all`),
  mirroring inspect_ai's lazy provider imports. Install the extra for the
  provider you run, e.g. `pip install itemeval[openai]` — the `openai` extra
  also covers OpenRouter and other OpenAI-compatible providers. The base
  install stays SDK-free; running a real provider without its extra raises
  inspect_ai's `PrerequisiteError` with the install hint.
- Ship a `py.typed` marker (PEP 561): downstream type checkers now see
  itemeval's annotations. Added the `Typing :: Typed` and Python 3.11/3.12
  classifiers.
- Relaxed the `pyarrow` (`>=24` → `>=15`) and `datasets` (`>=5` → `>=3`)
  lower bounds to the oldest versions whose APIs we actually use, easing
  co-installation; dev/CI still pin the latest via `uv.lock`. The full test
  suite passes at both the floor and the locked versions.
- Expanded `[project.urls]` (Homepage, Documentation → wiki, Changelog, Issues)
  and switched the README's PyPI-facing links to absolute GitHub URLs.
- Minimum Python is now 3.11 (was 3.10). The tested dependency stack resolves
  pandas 3.x, which requires Python >=3.11, so 3.10 could only ever install a
  different (pandas 2.x) stack that was never tested. Floor now matches the
  tested stack; `uv.lock` reconciled to a single resolution (dropped the
  3.10-only `exceptiongroup`/`tomli`/`async-timeout`/`pytz` backports).

[Unreleased]: https://github.com/luozm/itemeval/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/luozm/itemeval/releases/tag/v0.2.0
[0.1.0]: https://github.com/luozm/itemeval/releases/tag/v0.1.0
