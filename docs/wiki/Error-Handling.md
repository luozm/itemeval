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
graders:
  judge:
    attempt_timeout: 300       # per-judge, same meaning
```

The value passes straight through to inspect's `GenerateConfig.attempt_timeout`:
when an attempt exceeds it, inspect **abandons and retries** that attempt — and
through OpenRouter the retry may be routed to a healthier upstream. It is opt-in
(unset = today's unbounded behavior) and is a pure execution knob, so setting it
never changes a condition id or re-keys your study.

Two cautions:

- **Pick a value generous enough not to cut a legitimately slow stream.** A
  reasoning model can stream a single completion for a long time; if the timeout
  fires on a healthy-but-slow attempt, the retry hits the same wall and the row
  can fail repeatedly. Size the timeout to your slowest *expected* completion.
- **Leave it unset under a batch plan** (`policy: full-batch`). A batch job's
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
