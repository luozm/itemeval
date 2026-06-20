# Error Handling and Reporting

itemeval's rule is **flag, never silently drop**: every failure mode during a
run leaves a durable, inspectable record, and re-running is always safe. This
page is the single reference for what can go wrong, how it's recorded, how it's
reported, and what re-running does.

A failure is one of two kinds:

- **Setup errors** raise an exception and stop the command before any model is
  called (bad config, missing template, dataset load failure, cost gate).
- **Run-time failures** never abort the stage — they are captured per row,
  written to the store, and summarized. inspect runs with `fail_on_error=False`,
  so one bad sample never sinks a whole condition.

## Run-time failures: the three row-level channels

During `generate`/`grade`, each sample resolves into exactly one of these. All
three keep the row — none is ever dropped.

| Channel | What it is | Recorded as | Reported as | Re-run behavior |
|---|---|---|---|---|
| **Sample error** | Provider/API failure: timeout, rate-limit exhaustion, 5xx, content filter, refusal | `error` set; `solution`/`judge_completion` null | `errors=N` (run summary; `status` **err** column) | **Re-attempted** — the row is pending again |
| **Parse failure** *(grade only)* | Judge replied, but no valid `{"score": <number>}` block | `parse_ok=false`, `parse_error=<code>`, `judge_completion`=raw text, `score` null | `parse_failures=N` (run summary; `status` **parse_fail** column) | **Final** — not retried; use `grade --force` |
| **Empty completion** | Generation finished with no API error but blank text (typically a reasoning model whose `max_tokens` was spent entirely on hidden reasoning) | `error` null, `solution` blank, `stop_reason` usually `max_tokens` | `empty=N` (`status`); `grade` prints a count + stop-reason breakdown | Governed by [`solvers.on_empty`](Configuration.md) |

### Sample errors

inspect retries each erroring sample once in-eval (`retry_on_error=1`) before the
error is recorded. A recorded error row has `error` set and no `solution`. On
the next `generate`/`grade` of the same command, errored rows are treated as
incomplete and re-attempted (already-succeeded samples are served from inspect's
response cache, so they are not re-paid).

### Parse failures (judge output)

Judge parsing is strict — fenced ```json blocks last-to-first, then any raw JSON
object — with exact failure codes in `parse_error`:

| Code | Meaning |
|---|---|
| `no_json_object` | No JSON object found anywhere in the completion |
| `no_score_in_json` | A JSON object was found, but it has no `score` key |
| `score_not_numeric` | `score` is present but not coercible to a finite number (or is a bool) |
| `score_not_finite` | `score` parsed to `NaN`/`inf` |

Parse failures are **results, not errors**: the row is kept with `parse_ok=false`
and the raw `judge_completion` for inspection, and is **final** — re-running
`grade` will not retry it. To re-grade, either change the rubric (its hash
changes, starting a fresh grade condition) or run `grade --force`. A sample-level
*error* during grading (the judge call itself failed) is the **sample error**
channel instead — `error` set, `parse_ok=false`, `parse_error` null — and is
re-attempted, not final.

### Empty completions

A completed generation with no error but no gradable text. This is a distinct
channel from both errors and parse failures, controlled by `solvers.on_empty`:

| Policy | Effect |
|---|---|
| `skip` *(default)* | Excluded from grading; surfaced in the `grade` summary and the `status` **empty** column |
| `rerun` | Also treated as not-done by `generate`, so a later `generate` re-attempts them (raise `max_tokens` / lower `reasoning_effort` first — an identical request hits the response cache and stays empty) |
| `grade` | Sent to the judge as-is (an empty answer, usually scored low) |

The usual cause is too small a `max_tokens` for a reasoning model — size the cap
for hidden reasoning **plus** the visible answer. See
[Configuration](Configuration.md) for details.

### Empty materialized rubrics

For a [two-stage (materialized) rubric](Configuration.md#two-stage-materialized-rubrics),
the materializer can complete with no error but produce **no rubric text**. The
item is still graded — against a *blank* `{rubric}` — never silently dropped, and
the run reports it: `GradeResult.materialize_empty` counts them, and the
`empty-materialized-rubrics` hint names the count and the materializer model. The
empty rubric is still frozen in `materialized_rubrics.parquet` (so it is not
re-materialized on resume); to re-derive, raise the materializer's `max_tokens`
or edit the build template (a new build-template hash re-materializes), or delete
the store. The usual cause is too small a `max_tokens` for a multi-section
marking scheme — the default is 2048.

## Truncation

A solver that stops on a **length cap** returns a *truncated-but-non-empty*
answer: it completed with no API error and produced gradable-looking text, but it
was **cut short**, not finished. Left unflagged it counts as `completed` and is
scored by the judge as a finished answer — so **a budget cut reads as a content
failure**, quietly corrupting the measurement.

itemeval flags it. A completed row is **truncated** when `error` is null, the
solution is **non-empty**, and `stop_reason` is a length cap —
**`max_tokens`** (the requested `solvers.max_tokens` budget) or **`model_length`**
(the model's own context limit). This is the disjoint complement of an
[empty completion](#empty-completions): an *empty* length-cap stop is `empty`, a
*non-empty* one is `truncated`. (`content_filter` is a refusal, and `unknown`
conflates unmapped provider reasons — neither is truncation.)

Where it shows up:

- **`status`** — a `trunc` column on the GENERATE table (and
  `ConditionStatus.truncated` in `--json`). It is an *informational sub-count* of
  `done`/`completed`: a truncated row is still counted complete and is still
  graded — the flag never reclassifies it, changes the money gate, or alters
  `solvers.on_empty`.
- **export** — a `truncated` boolean column in `gradings_long.parquet`; filter it
  out of a content-validity analysis (`df[~df.truncated]`).
- **`generate`** — the `truncated-completions` hint fires when any row was cut:
  `21 completion(s) stopped at a length cap … raise solvers.max_tokens or filter
  truncated rows`; `GenerateResult.truncated_total` carries the count in `--json`.

The fix is a study decision, not an automatic one: **raise `solvers.max_tokens`**
(or a per-model-config value) and re-generate — grow-in-place resume re-runs only
the affected cells, replaying the rest from the response cache at $0 — or keep the
truncated rows and exclude them in analysis. There is no `on_truncated` knob: the
signal is surfaced; what to do with it is yours.

## Oversized solutions

Weak models sometimes fall into a **repetition loop** and emit an enormous,
degenerate output — 100k+ characters of the same phrase — that is not a valid
answer. Sending it to the LLM judge is pure waste: the input is huge (expensive),
and the output scores 0 anyway.

The per-grader knob **`graders.<name>.max_solution_chars`** (default `null` =
off) skips it. When set, `grade` checks each stored solution's visible text
length **before** calling the judge: any solution over the threshold is **not
judged** — it is recorded as a grading row with `score=0`, `parse_ok=false`,
`parse_error="oversized_skip"`, and `judge_completion=null`. This mirrors the
empty-solution skip ([above](#empty-completions)): the row is *recorded, not
graded*, and is **not** a parse failure. Empty handling runs first, so a blank
completion is counted as empty (not oversized) and never both.

Where it shows up:

- **`grade` summary** — `oversized solutions: N scored 0 without grading (over
  max_solution_chars)`, with `GradeResult.oversized_skipped` carrying the count
  in `--json`.

The threshold is a **design declaration** (it changes what gets graded), so it
enters the experiment_id digest like `solvers.on_empty` — but never a grade
condition id. Choose it well above a legitimate long proof (a realistic loop is
100k+ chars; a real proof is rarely past a few tens of thousands). `null` leaves
every solution graded as before.

## Serving provider and native finish reason

When you route through OpenRouter (`openrouter/…` models), each call is
load-balanced across provider **backends**, and a flaky backend can return a
**soft failure**: HTTP 200 with `finish_reason=error` (or another non-standard
reason) and empty/truncated content. inspect's `stop_reason` flattens any reason
it doesn't recognize — `error` included — to **`unknown`**, so the stored
`stop_reason` alone can't tell a soft failure apart from a genuinely unmapped
stop, and it never records *which* backend served the call.

itemeval captures two raw-provenance columns on every solver and judge call:

- **`served_provider`** — the backend that actually answered (OpenRouter's
  routed `provider`, e.g. `GMICloud`, `Fireworks`, `Anthropic`).
- **`native_finish_reason`** — the provider's raw `finish_reason` *before* the
  flatten, recovering what `stop_reason` collapses to `unknown`.

Where they show up:

- **`solutions.parquet` / `gradings.parquet`** — `served_provider,
  native_finish_reason` per row.
- **export** — `gen_served_provider, gen_native_finish_reason` (solver call) and
  `grade_served_provider, grade_native_finish_reason` (judge call) in
  `gradings_long.parquet`.

Both are **null** when the response didn't carry them — mock models, cache
replays, and providers that don't return the fields. They are pure diagnostics:
no hint, gate, or status line, and they never change a score, a content key, or
`stop_reason`/`truncated`. Use them to spot a misbehaving backend — e.g.
`df[df.gen_native_finish_reason == "error"].gen_served_provider.value_counts()`
— and exclude or re-route those cells in your analysis. To steer OpenRouter away
from a bad backend on the next run, see
[`provider_routing`](Configuration.md#field-notes) (`{ignore: [...]}` /
`{order: [...]}`).

## Soft failures and reroute

A manual `provider_routing: {ignore: [...]}` blocklist (above) is whack-a-mole — a
new item or model surfaces a backend the list doesn't have. The opt-in knob
**`solvers.max_reroutes`** (default `null` = off) automates it: after a `generate`
run, any **soft-failed** cell (no API error, but `native_finish_reason == "error"`
or `stop_reason == "unknown"`) is **re-issued on a different backend** — the one
that failed added to `provider: {ignore: [...]}` — up to `max_reroutes` rounds,
accumulating the bad backends across rounds.

- A **recovered** cell replaces the bad row **in place** (same `condition_id,
  item_id, epoch`); the re-issue is a fresh call (the response cache is bypassed,
  since its key doesn't vary on routing).
- A cell **still soft-failed after the cap** keeps its honest soft-failure row (no
  fake score) and is reported by the `reroute-residue` hint.

Where it shows up:

- **`generate` summary** — `reroute: N cell(s) re-issued · M recovered · K still
  invalid`, with `GenerateResult.rerouted` / `reroute_recovered` /
  `reroute_unresolved` in `--json`.

Notes and limits:

- **Spend.** A reroute re-issues paid calls *beyond* the pre-flight estimate —
  bounded by `max_reroutes` × the soft-failure count (a minority), folded into the
  reported spend. The pre-flight money gate stays the only gate.
- **Detection runs the whole in-scope store**, so a resume also cleans up soft
  failures from a prior run (re-issuing only the still-bad cells, never touching
  good ones). `max_reroutes` is an operational knob — it never changes condition
  ids, so toggling it on a re-run converges into the same experiment.
- **Single-provider models can't be rerouted** (no alternate backend) — they show
  up in the unresolved count; substitute the model (a deeper preflight can flag
  these before a paid run). Reroute is also **skipped** under a batch plan (a batch
  job can't re-issue mid-flight) and for `--wave`/offset runs (fresh observations).
- This targets the **provider soft failure** only — a legitimate
  [truncation](#truncation) (a budget cut) or an [empty completion](#empty-completions)
  keeps its own handling and is never rerouted.

## Eval-level (whole-condition) failures

If an entire `inspect_ai.eval(...)` raises — a misconfigured task, an
unreachable provider, an auth failure — itemeval catches it, records the
condition as `status="error"` with the exception message, and **continues to the
next condition**. No rows are written for that condition. The CLI prints:

```
[2/4] gpt-5-mini_builtin-standard_default  ERROR: terminal: PrerequisiteError: ...
```

The message is prefixed with its **classification** — `terminal:` (the model is
dead/EOL or your key can't reach it — fix the roster) or `transient:` (a timeout,
rate limit, or 5xx — re-running the same command may succeed; see
[Retry and resume](#retry-and-resume--re-run-the-same-command)) — so a glance at
the summary tells you whether to edit the config or just retry. An unclassifiable
failure keeps its raw message, unprefixed.

The command's exit code is **1** if any condition errored. Other conditions in
the same run still complete and persist normally.

## Pre-flight model check (`itemeval preflight`)

A dead model otherwise isn't discovered until it fails mid-paid-run. Run
`itemeval preflight CONFIG` **before** a paid stage to probe each distinct model
in the grid with one ~1-token call and see roster health up front:

```
preflight: probed 40 distinct model(s) over the provider network (~1 token each) — 39 ok · 1 dead · 0 unverified
  dead: openrouter/some/eol-model — BadRequestError: 404 model not found
```

Each model is **ok**, **dead** (a *terminal* failure — fix the roster), or
**unverified** (a *transient* failure the probe can't confirm, e.g. a rate limit —
never reported dead, since deleting a model that was merely throttled is the worse
mistake). The same **terminal-vs-transient** distinction labels in-run condition
errors (above). `preflight` exits **1** when any model is dead, so

```
itemeval preflight cfg.yaml && itemeval generate cfg.yaml
```

stops before spend if the roster is broken. It is a deliberately-invoked command
(invoking it is your consent to its sub-cent spend) — *not* run automatically
inside `generate`/`grade`. `--json` carries `ok`/`dead`/`unverified` counts and a
per-model `{id, status, detail, http_status}` array. `mockllm/*` ids probe ok with
no network. Full reference: [CLI#preflight](CLI.md#preflight--probe-roster-health-before-a-paid-run).

## Setup errors (before any model call)

These raise an exception, print `itemeval: error: <message>`, and stop:

| Exception | Cause | Exit code |
|---|---|---|
| `ConfigError` | YAML shape/validation failure, bad grader/template reference | 2 |
| `TemplateError` | Missing template file or required placeholder | 2 |
| `AdapterError` | Dataset load or field-mapping failure | 2 |
| `StoreError` | Parquet schema/IO problem (e.g. `grade` with no solutions) | 1 |
| `BudgetError` | Pricing refresh / estimator failure | 1 |

The Python API raises these exceptions directly instead of mapping them to exit
codes.

## Budget gate (refusals, not errors)

The cost gate can decline to proceed; this is a deliberate stop, not a failure:

| Situation | Exit code |
|---|---|
| Projection exceeds `confirm_above_usd` and confirmation is needed in a non-TTY shell | 3 |
| Projection exceeds `budget.max_usd` (hard cap; `--yes` does **not** override) | 4 |

Pass `--yes` to auto-confirm in scripts/CI, and set `budget.max_usd` as the
un-overridable backstop. See [Budget and Costs](Budget-and-Costs.md).

## Exit codes (all commands)

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | unexpected error, or at least one condition failed during a run |
| 2 | config / template / adapter error (and argparse usage errors) |
| 3 | cost gate declined, or confirmation required in a non-interactive shell |
| 4 | projected cost exceeds `budget.max_usd` |

## Where failures are visible after a run

- **Run summary (stdout)** — per-condition lines plus totals: `rows written`,
  `errors`, `parse_failures`, and the empty-solution line.
- **`itemeval status`** — the completion matrix: generate `done / err / empty`,
  grade `done / err / parse_fail`, per condition.
- **Stores** — `solutions.parquet` (`error`, `stop_reason`) and
  `gradings.parquet` (`error`, `parse_ok`, `parse_error`, `judge_completion`).
  See [Outputs and Schemas](Outputs-and-Schemas.md).
- **`log_index.parquet`** — per-eval `status` and completed/total sample counts.
- **Raw `.eval` logs** — full inspect evidence: stack traces, retries,
  per-sample events. The store is the source of truth; the logs are the receipts.

## Stalled requests (`attempt_timeout`)

By default neither itemeval nor inspect bounds a single request, so a degraded
endpoint that trickles bytes (or hangs without erroring) can hold a run with no
upper bound — the classic flaky-routing failure. Set a per-attempt timeout to
bound it:

```yaml
solvers:
  attempt_timeout: 300         # seconds; abandon a stalled attempt and retry
  max_retries: 2               # ...at most this many attempts, then the cell errors
graders:
  judge:
    attempt_timeout: 300       # per-judge, same meaning
    max_retries: 2             # per-judge attempt cap
```

The value passes straight through to inspect's `GenerateConfig.attempt_timeout`:
when an attempt exceeds it, inspect **abandons and retries** that attempt — and
through OpenRouter the retry may be routed to a healthier upstream.

**A timeout retries up to `max_retries` attempts, then gives up.** inspect retries
an abandoned attempt *until `max_retries` (or a total timeout) is reached* — and
with neither set it **retries forever**. So itemeval bounds it: when
`attempt_timeout` is set and `max_retries` is not, the attempt cap defaults to a
small value (2) — without it, a genuinely hung backend would loop indefinitely,
timing out and re-issuing without ever stopping. After the cap the cell is left as
an **error** (the `err` column / `errors=N`), which a later [re-run](#retry-and-resume--re-run-the-same-command)
re-attempts — likely on a fresh backend draw. Set `max_retries` explicitly to
raise or lower the cap; it bounds transient-HTTP-error retries too. Both are pure
execution knobs — setting either never changes a condition id or re-keys your study.

Two cautions:

- **Pick a timeout generous enough not to cut a legitimately slow stream.** A
  reasoning model can stream a single completion for a long time; if the timeout
  fires on a healthy-but-slow attempt, each retry hits the same wall until the cap,
  then the cell errors. Size the timeout to your slowest *expected* completion.
- **Leave both unset under a batch plan** (`policy: full-batch`). A batch job's
  submit-and-poll legitimately runs for minutes-to-hours and is the same call the
  timeout wraps, so a timeout would abandon a healthy batch.

A timed-out attempt that ultimately fails surfaces like any other sample error
(see the channels above). Suppressing retries on *terminal* failures (a dead
model, not a slow one) is a separate, upcoming pre-flight check.

## Retry and resume — re-run the same command

The parquet store is keyed, so re-invoking a command is always safe and never
duplicates work:

- **Completed** rows skip; the response cache means already-paid calls aren't
  re-charged.
- **Sample errors** re-run.
- **Empty completions** re-run only under `on_empty: rerun`.
- **Parse failures** stay final (use `--force` to redo).
- `--force` re-runs everything selected; `--condition <id|prefix|slug>` (and
  `--grader`/`--rubric` for grade) narrows a run.
- When a planned run would overwrite existing rows (`--force`, epoch
  extension after raising `replications`, `on_empty: rerun`), the pre-gate
  block states it — `this run replaces N existing rows (…)` — as part of the
  single money confirmation, and the run JSON carries `rows_replaced`.

Interrupting a run (Ctrl-C, a crash, a provider outage mid-run) needs no special
handling — just run the same command again.

## Crash recovery — getting a hard-killed run back into the store

Resume above relies on the **store** holding what completed. But durable parquet
(`solutions`/`gradings`) is written only **after** a stage's `inspect_ai.eval()`
returns cleanly — itemeval projects the run's *in-memory* logs into the store at
that point. A **hard** mid-run death — `SIGKILL`, OOM, or a force-killed stuck
request (the kind of kill that a flaky endpoint provokes) — never reaches that
step, so it can write **zero** rows even though most of the work finished. The
progress isn't lost: inspect writes its `.eval` log **incrementally** (it is the
write-ahead log) under `logs/<stage>/`. It just hasn't been read back into the
store, so `status`/`export` go blind to the killed run.

**`itemeval harvest CONFIG`** reads those `.eval` files back and projects them
into the stores through the same row builders a live run uses — making the crashed
run's completed cells readable and resumable without re-running. It is idempotent
(skips logs already in the store; the upserts dedup), so it is always safe to run.

You rarely call it by hand: **`status`, `export`, `generate`, and `grade`
auto-harvest first**, so the store reflects a crashed run *whenever you look*, and
a re-run resumes the recovered cells instead of re-paying them. When rows are
recovered, those commands print `recovered N solutions + M gradings from K
interrupted run log(s) into the store …` (Law 1: a read/resume command that writes
recovered rows announces it; `harvested` rides the `--json` payload). Pass
`--no-harvest` to skip the automatic step. The distinction from ordinary resume:
*resume* re-runs what the store says is missing; *harvest* first teaches the store
what a crashed run already finished, so the missing set is honest. A normal Ctrl-C
or clean interrupt needs no harvest — only a kill that skipped the store write
does, and the auto-harvest handles it for you. See
[CLI#harvest](CLI.md#harvest--recover-a-crashed-runs-logs-into-the-store).
