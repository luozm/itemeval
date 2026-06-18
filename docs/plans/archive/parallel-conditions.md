# Implementation plan — parallel-conditions (concurrent condition execution + pre-flight run plan/ETA)

**Status: IMPLEMENTED 2026-06-18.** Written + shipped 2026-06-18 against
`inspect_ai 0.3.239` (pinned in `uv.lock`). This file is now the design record.

**As-built notes (where the implementation settled):**
- W1 shipped for both stages via a shared `run_condition_evals` helper +
  `max_tasks_for` (distinct-execution-model count) in `generate/_run.py`, reused
  by `grade/_run.py`. Grade keeps verifiable conditions in-process (Phase 1) and
  batches only judge conditions. Logs moved to one `logs/<stage>/` dir
  (`StudyPaths.logs_stage_dir`). Per-sample retry/error semantics unchanged;
  model-construction failures are isolated per condition (a `try/except` around
  the factory call in Phase 1), and a whole-eval exception errors every planned
  condition (`eval_error_message`).
- W2 shipped as `concurrency` / `eta_seconds` / `eta_latency_basis` on
  `StageEstimate` (computed in `estimate_study`'s `stage_total`), rendered by
  `cli._eta_line` on estimate/generate/grade. Latency prior =
  `median_latency_s` over the stage store; default `DEFAULT_CALL_LATENCY_S=8.0`.
- Issue #3 (cost-lever line) shipped as `cli._cost_levers_line` — a CHANGELOG
  `Added` entry, no key (legibility of existing behavior).
- **Accepted tradeoff:** parallel single-eval defers `upsert_solutions` /
  `upsert_gradings` to the end of the run, so a mid-run `itemeval status` from
  another shell no longer sees rows accumulate per condition (inspect's own
  `.eval` logs still update live). Left as-is; revisit only if long-run live
  `status` becomes important (would need an inspect hook to stream rows).
- **Cache mechanisms verified unaffected** (an explicit follow-up concern). The
  warm-then-fan-out scheduler (`gated_generate`) keeps an `events` dict per task
  build (one per condition), so two conditions reusing the same group key (both
  keyed by item id) stay isolated under one eval — the gate is per-condition, not
  per-eval (comment in `_cachegate.py` corrected; `tests/test_cache_parallel.py`
  guards it). Local response cache, OpenAI `prompt_cache_key` (per
  study+condition), provider `cache_prompt` markers, and `split_prompt`/
  `split_rubric` are all per-task config keyed by content, so distinct conditions
  never collide. The estimator models cache savings per condition (never
  cross-condition), so projections are unchanged. **One honest nuance, not a
  regression:** a serial run could *incidentally* let condition B read a provider
  cache that condition A warmed when both shared an identical cacheable prefix on
  the same model (differing only by sampling config); concurrent runs may race on
  that one leader write. This was never a designed or estimated mechanism (the
  per-condition gate never coordinated across conditions), so no projection is
  violated — worst case is one extra cache write for such a condition pair.

Original brief follows (read these first, in order):

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Load-bearing here: Law 5
   (knob buckets — "call ordering" is an *optimization* knob → invisible
   default, never a config option), Law 8 (output written to be quoted; a
   progress bar is not a fact of record), Law 6/7 (every fact has a text line +
   a stable JSON field).
3. `DEVELOPMENT.md` — inspect boundary rules: **inspect imports stay confined to
   the task-builder / orchestrator / extension modules.** The orchestrators
   (`generate/_run.py`, `grade/_run.py`) own the `inspect_ai.eval()` call; any
   shared helper that touches inspect lives beside them, not in a pure module.
4. This file end-to-end before coding any part — the two workstreams share the
   build-all-tasks / one-eval mechanism.

Scope: 2 workstreams. **W1** concurrent condition execution (generate + grade) ·
**W2** pre-flight run plan + coarse ETA line. A third, *separate* fix-class
change ships alongside but carries no key — see "Related (separate commit)".

---

## Context: the facts that decide the design

### Current behavior (this repo)

- **Serial per-condition loop.** [generate/_run.py:482-552](../../src/itemeval/generate/_run.py#L482)
  iterates `selected` conditions and calls `inspect_ai.eval(task, model=…)`
  **once per condition**. The comment at
  [:527](../../src/itemeval/generate/_run.py#L527) ("inspect's eval is
  one-at-a-time per process") is the load-bearing misconception this plan
  corrects — **delete it**. Within a condition inspect already parallelizes
  samples; the serial limit is purely cross-condition.
- **Per-condition error isolation** is a `try/except` around the eval call
  ([:554](../../src/itemeval/generate/_run.py#L554)) that emits a `status="error"`
  `ConditionRunReport` and continues.
- **Rows persist per condition, inside the loop:** `upsert_solutions`
  ([:575](../../src/itemeval/generate/_run.py#L575)) + ledger/log-index writes
  run after each condition. (See W1 tradeoff: single-eval defers these.)
- **`rows_from_generate_log(log, cond, …)`** ([:343](../../src/itemeval/generate/_run.py#L343))
  already takes the condition explicitly — it does not depend on loop position,
  so it works unchanged once we can map a log → its condition.
- **Per-condition log dir:** `logs_dir(stage, condition_id)` →
  `logs/<stage>/<condition_id>/` ([store/_layout.py:46](../../src/itemeval/store/_layout.py#L46)).
- **Grade mirrors generate** exactly: the same serial `try/except` eval loop in
  [grade/_run.py](../../src/itemeval/grade/_run.py) (~line 340-375). Both stages
  are in scope.
- **The model factory** `resolve_model(model, stage, model_args)`
  ([_mockmodels.py:63](../../src/itemeval/_mockmodels.py#L63)) returns a `Model`
  when `model_args` is non-empty or the id is mock, else a bare string — **both
  are valid for `Task.model`** (a string is resolved by inspect at eval time
  with no extra args, which is exactly right when there are no per-condition
  args). Per-condition `model_args` come from `model_args_for(...)`
  ([_endpoints.py:81](../../src/itemeval/_endpoints.py#L81)).
- **Task metadata for mapping** is set in `build_generate_task`
  ([generate/_task.py:99](../../src/itemeval/generate/_task.py#L99)):
  `metadata={"itemeval": {"stage", "study", "condition_id", "epoch_offset"}}`,
  task `name=f"gen_{cond.slug}"`.
- **Ledger stores `latency_s`** per row ([generate/_run.py:406](../../src/itemeval/generate/_run.py#L406))
  and log stats carry `started_at`/`completed_at` — the raw material for W2's
  latency prior. There is **no time/ETA estimate anywhere today** (the estimator
  computes cost only).
- **Pre-flight rendering** for both stages lives in `_run_stage`
  ([cli.py:368-393](../../src/itemeval/cli.py#L368)) — the projected-cost block.
  W2's line and the related cost-lever fix land here.

### inspect_ai facts (installed source, 0.3.239 — **[verify]** if pinned ver moved)

- `inspect_ai.eval(tasks, model=…, max_tasks=…, …)` accepts a **list of tasks**
  and runs up to `max_tasks` concurrently. `max_tasks` defaults to the number of
  distinct model names when >1 (`_eval/eval.py`). Returns **one `EvalLog` per
  task** (order not contractually tied to input order → map explicitly).
- **A `Task` may carry its own model.** `resolve_tasks` (in
  `inspect_ai/_eval/loader.py`) builds each `ResolvedTask` with
  `model=task.model or model`. `Task.__init__` accepts `model=` (verified).
  ⇒ Bind each condition's `factory(exec_model, …)` to `Task.model`, pass **one**
  fallback model at the top level (e.g. the first task's model), and every task
  runs on its own model + baked `model_args`. The top-level `model=` only
  cross-products over the *top-level* list (length 1 here) — no unwanted product.
- **Log → condition mapping:** `EvalSpec` (i.e. `log.eval`) exposes both `task`
  (the `gen_<slug>`/`grade_<slug>` name) and `metadata` (verified field list).
  Map by `log.eval.metadata["itemeval"]["condition_id"]`.
- Per-task `GenerateConfig` (temperature, `batch`, `cache_prompt`) already rides
  on `Task.config` (set in `build_generate_task`). We pass **no** generate-config
  kwargs to `eval()`, so each task's config stands independently. Batch/native
  routing are orthogonal to this change (they flow through `Task.config` /
  `Task.model` per condition exactly as today).

---

## W1 — Concurrent condition execution (generate + grade)

**Goal.** Run all selected conditions of a stage in **one** `inspect_ai.eval()`
call with bounded concurrency, so a multi-model sweep's wall-clock approaches
the slowest single model rather than the sum. Generate and grade both.

**Config / public surface.** **No new knob** (UX Law 5: concurrency is an
optimization → invisible default). `max_tasks` is an internal default —
**[decide]** distinct-model count (inspect's own default) vs a fixed small cap;
recommend a modest cap to bound same-provider 429 pressure (inspect retries
429s). `ConditionRunReport` / `GenerateResult` / grade result fields are
unchanged (append-only if anything is added).

**Mechanism.**
1. Keep the existing per-condition planning (compute `to_run` / skip set per
   condition as today — that logic is independent and stays).
2. For each condition *with work*, build its task and set
   `task.model = factory(exec_model, stage, model_args_for(...))`. (Either add a
   `model=` arg to `build_generate_task` or set `.model` after building — pick
   the smaller diff.)
3. One `inspect_ai.eval(tasks, model=tasks[0].model, max_tasks=K, display=…,
   log_dir=<single stage dir>, log_format="eval", fail_on_error=False,
   retry_on_error=1, tags=…, metadata=<run-level>)`.
4. Map each returned log → condition via
   `log.eval.metadata["itemeval"]["condition_id"]`; then the existing
   `rows_from_generate_log` / upsert / ledger / log-index / report code runs
   per log, unchanged.
5. **Error mapping** replaces the per-condition `try/except`. Two error levels,
   verified against inspect 0.3.239 — preserve both:
   - *Sample level* (one item's request fails): unchanged and immediate.
     `retry_on_error=1` retries the sample once right away; `fail_on_error=False`
     ("never fail on sample errors") records it and the task's other samples
     finish. Under parallel exec these retries are per-task and independent of
     sibling models' progress — no waiting, no cross-model coupling.
   - *Task level* (a model's whole task fails — auth/connection): inspect's
     multi-task runner **isolates** it — `inspect_ai/_eval/run.py:464` "errors
     generally don't escape from tasks (the exception being … finalisation)".
     A failed model yields a failed `EvalLog`; **sibling models keep running**,
     are not cancelled, and do not wait for it. Map such a log
     (`status != "success"`) → `ConditionRunReport(status="error", …)`. This is
     the same record-and-continue outcome as today's serial `try/except`, just
     concurrent and with no auto-restart (re-run to resume, as now).
   - *Whole-call escape* (rare — the finalisation case inspect flags): wrap the
     single `eval()` in `try/except` and mark **every** not-yet-reported
     condition `status="error"`, so a catastrophic failure can't silently drop
     conditions.
6. **Log layout:** single `logs/<stage>/` dir (drop the per-condition subdir).
   Readback is parquet-keyed by `condition_id`, so this is low-risk — **confirm
   nothing globs `logs/<stage>/<cond_id>/`** (grep `logs_dir(` callers; only
   generate/grade write today).
7. **Shared helper:** generate and grade share the build-all-tasks / one-eval /
   map-by-condition shape. Factor it into a small helper *beside the
   orchestrators* (inspect import allowed there per DEVELOPMENT.md) — keep the
   stage-specific row/ledger handling in each `_run.py`.

**UX contract.** No new gate, no new blocking interaction (Law 2). No new
announcement is *required* by W1 itself (concurrency is invisible), but the
post-run summary already lists per-condition results — unchanged. The deleted
`:527` comment is the only narrative change in-code.

**Tests.** Mock-model smoke test (no paid APIs): a study with ≥2 conditions on
≥2 distinct `mockllm/*` ids → one `eval()` call → assert each condition's rows
land with the right `condition_id`, correct counts, and that an induced failure
in one condition isolates to that condition's `status="error"` report while the
others succeed. Assert log→condition mapping is by metadata, not index (shuffle
to prove it). Existing generate/grade tests must pass unchanged.

**Docs/CHANGELOG.** `[Unreleased]` `Changed` entry with `Closes: parallel-conditions`
(co-shipped with W2). No wiki page is strictly required; if any tutorial states
"conditions run serially", fix it. Remove the `parallel-conditions` section from
`docs/BACKLOG.md` in the shipping commit (design record stays in this archived
plan). If `ROADMAP.md` names the key, move it to the `**Already landed**` line.

---

## W2 — Pre-flight run plan + coarse ETA line

**Goal.** One durable, quotable line before the run telling the operator how big
it is and roughly how long — the fact an agent needs to decide whether to
background the command. A progress bar can't be relayed (Law 8); a line can.

**Config / public surface.** No new knob. New **append-only** JSON fields on the
estimate/run surface for parity (Law 6/7): e.g. `expected_calls`,
`eta_seconds` (+ the concurrency `K` and the latency basis used). Names final at
implementation; keep append-only (Law 7).

**Mechanism.** In `_run_stage` ([cli.py:368](../../src/itemeval/cli.py#L368)),
next to the projected-cost block, print:
`generate: N conditions × E epochs × I items = C calls; ~T min at concurrency K`.
`C` reuses the estimator's expected-call count. `T ≈ (C / K) × median_latency`;
seed `median_latency` from this study's ledger `latency_s` when present, else a
clearly-labeled default constant (mark the ETA **rough** — it is advice-grade,
never a gate). `K` is W1's effective `max_tasks`. Keep the math in a pure helper
(testable without inspect).

**UX contract.** This is an **announcement/summary line** (Law 8) — prints
unconditionally in the text rendering, no switch hides it; mirrored in `--json`
(Law 6). It never changes behavior and never blocks (Law 2). Add the line to the
UX-PATTERNS channel-spec examples / ledger if the surface table tracks it.

**Tests.** Pure-helper unit tests: call count from a known grid; ETA from a
synthetic ledger (latency present) and from the default (latency absent); assert
text line and JSON field carry the *same* numbers (Law 6). No paid APIs.

**Docs/CHANGELOG.** Same `Closes: parallel-conditions` entry (`Added` for the
ETA line). Wiki: a short note wherever run output is documented that the ETA is
rough and how it's seeded.

---

## Related (separate commit, no key) — cost-lever status line (Issue #3)

Not part of the `parallel-conditions` feature, but it touches the **same**
pre-flight region and answers the same operator question ("is any cost-saving on
for dev?"), so do it in the same session as a `fix:`-class change. For
`--policy dev` today, batch is force-OFF, native routing is OFF (needs a batch
plan), `cache_prompt` resolves to provider-default at reps=1, cache-scheduling
is on-but-inert at reps=1 — and the pre-flight prints **none** of this (it
announces levers only when *active*: [cli.py:202](../../src/itemeval/cli.py#L202),
[:330](../../src/itemeval/cli.py#L330)). Add a single durable line stating each
lever's state with a one-clause reason when off, e.g.
`cost levers: batch off (dev policy) · native-routing off (needs batch) ·
prompt-cache provider-default (reps=1) · response-cache on ($0 replays)`, with a
JSON mirror and a `docs/wiki/Cost-Savings.md` anchor (Law 1/6). Per CLAUDE.md
this is a `fix:` (legibility of existing behavior, no new behavior) → no key, no
BACKLOG entry; just a `[Unreleased]` `Fixed`/`Changed` entry + wiki touch.

---

## Sequencing (canonical)

1. **W1** generate first (smaller, has the mock-model test harness), then grade
   reusing the shared helper. One commit (or two: generate, then grade).
2. **W2** on top of W1 — the ETA needs `K` from W1 to be meaningful.
3. **Cost-lever line** (Issue #3) — independent; can land before or after, its
   own `fix:` commit.

After each step: `make check` (lint + fast tests), CHANGELOG and any normative
doc tables updated in the **same** commit. Run the `same-change` skill before
committing the W1/W2 user-visible change.

## Out of scope (explicitly, to prevent creep)

- **A calibrated latency prior / time-calibration plumbing** (like the cost
  calibrator). W2 is deliberately *coarse*. If demand appears, that is a new key
  (sibling to `reuse-savings`'s calibration path), not this plan.
- **Streaming per-condition rows mid-run via an inspect hook** to preserve live
  `itemeval status` visibility. W1 accepts that single-eval defers parquet
  writes to the run's end (inspect's own `.eval` logs still update live). If
  mid-run `status` for long runs becomes important, track it separately.
- **A pause/break command** — out of scope project-wide (Ctrl-C + re-run is
  safe and complete).
- **A user-facing concurrency knob** — rejected by Law 5 (optimization knobs
  trend to invisible defaults).
