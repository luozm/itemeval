# Implementation plan — straggler-heartbeat (timer-driven slow-cell liveness)

**Status: IN PROGRESS (started 2026-06-22).** Written against inspect_ai 0.3.239
(pinned in `uv.lock`) — re-verify the pinned hook facts below if that moved. This
file is the working brief for a fresh implementation session: it carries all the
context that session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing law here is
   **Law 8**: progress is ephemeral decoration; *no fact may exist only in
   ephemeral output*. The straggler line carries **zero facts of record** — it is
   the existing stderr heartbeat under a stall. The **relay rule** (a plain factual
   line survives an agent's summary; a progress bar that never rendered off-TTY
   does not) is why it is plain text on stderr.
3. `DEVELOPMENT.md` — inspect boundary rules (mandatory, this wraps inspect): wrap
   don't fork; pass through don't rename; flatten at the public API; inspect imports
   confined to task-builder / orchestrator / **extension** modules. `_tracker.py` is
   the extension module, so the new hook methods + the asyncio timer live there.
4. `docs/plans/archive/live-tracker.md` — the **predecessor**. This feature extends
   the heartbeat it shipped; that plan documents the hook-registration mechanics,
   the banner pre-latch hazard, and the display gate this plan reuses unchanged.
5. This file end-to-end before coding — the workstreams share the `_RunContext`
   shape and the timer's emit-gating decision.

Scope: 2 workstreams. **W1** the timer + straggler render (the core) · **W2** the
per-sample retry annotation (`try N`).

---

## Context: the facts that decide the design

**What's being solved.** The shipped `live-tracker` heartbeat
(`src/itemeval/_tracker.py`) is **completion-event-driven**: `LiveTracker.on_sample_end`
(`_tracker.py:237`) is the only per-sample emit. So a hung cell — no `SampleEnd` —
produces no updates: the line freezes for as long as the stall lasts (observed: a
~13-min freeze during a provider hang), and slow-vs-hung is invisible. The tracker
already computes an aggregate `in-flight` count (`render_heartbeat`, `_tracker.py:129`
= `started − ended`) but cannot say **which** cells are slow or for how long.

**The fix in one sentence.** Add a wall-clock keepalive that ticks every ~30s and,
*only when no per-sample line has emitted recently* (a genuine stall), prints the
slowest in-flight cells — each `model · item · elapsed` — over a threshold
(~120s), capped at ~10 with a `+N more` footer.

**Everything lives in `_tracker.py`.** Both `generate` and `grade` run their stage
through the single `run_condition_evals` (defined `generate/_run.py:207`, imported by
`grade/_run.py:51`), which already wraps `inspect_ai.eval(...)` in
`_tracker.tracking(...)` (`generate/_run.py:239`) and already does
`import itemeval._tracker` (`generate/_run.py:26`, registering the hook before the
first eval). **No run-module change is needed** — the timer is driven by inspect
hooks the existing `LiveTracker` already owns. (This corrects the BACKLOG note
"`generate/_run.py` / `grade/_run.py` (launch/cancel the timer inside the
`tracking()` scope)": there is one shared seam, not two, and the launch point is an
in-loop hook, not the orchestrator's `tracking()` — see next.)

**Why the timer cannot launch in the orchestrator thread.** `inspect_ai.eval()` is
**synchronous** — it starts its own event loop (`anyio`/`asyncio`) and blocks the
orchestrator thread for the whole run. A task created in `tracking()` (which runs
*before* `eval()` on the orchestrator thread) has no running loop and never ticks.
The timer must be scheduled **onto inspect's loop**, from a coroutine that runs
inside it. The hooks are exactly that. **Verified in `.venv` inspect 0.3.239:**

- **Run-level hooks fire once per `eval()`, in-loop, and always.**
  `on_run_start(self, data: RunStart)` (`hooks/_hooks.py:367`) is emitted at
  `_eval/eval.py:850` (`await emit_run_start(...)`), once, after tasks resolve and
  before they run. `on_run_end(self, data: RunEnd)` (`:379`) is emitted on all three
  exit paths — success `:943`, empty `:945`, exception `:949` — so cancellation in
  `on_run_end` always runs. `RunStart` carries `run_id` + `task_names`; `RunEnd`
  carries `exception` + `logs`. Neither is needed beyond being a launch/teardown point.
- **`enabled()` is checked per-emit** by `_emit_to_all` (`hooks/_hooks.py`), so the
  existing dynamic gate (`enabled()` returns `_CTX.active`, `_tracker.py:231`) covers
  the new hook methods too: they no-op outside an itemeval run with the heartbeat on.
- **Async backend is asyncio by default** (`configured_async_backend()`,
  `_util/_async.py:184`: `INSPECT_ASYNC_BACKEND` env, default `"asyncio"`); itemeval
  never sets trio. So `asyncio.create_task(...)` / `asyncio.sleep(...)` inside
  `on_run_start` are valid. **Defensive degrade:** wrap the `create_task` in
  `try/except RuntimeError` (and only launch when `_CTX.active`) — if a future run is
  ever under trio, the straggler timer is simply skipped and the run falls back to the
  shipped completion-driven heartbeat. No private `current_async_backend` import.

**Mapping a sample → `model · item`.** The straggler line names the model, but the
model is **not** on the per-sample event — it lives at the task level:

- `EvalSpec.model: str` (`log/_log.py:919`) is the full sampled id (e.g.
  `openrouter/anthropic/claude-haiku-4.5`). `on_task_start(self, data: TaskStart)`
  (`hooks/_hooks.py`) carries `data.eval_id` + `data.spec` → build a
  `{eval_id: model}` map. Each itemeval condition is one task with one `.model`
  (`run_condition_evals` docstring, `generate/_run.py:219`), so `eval_id` keys the model.
- `on_sample_start(self, data: SampleStart)` carries `data.eval_id` (join key →
  model), `data.sample_id` (unique per sample *execution*, i.e. per (item, epoch) —
  the start-map key), and `data.summary.id` (`EvalSampleSummary.id`, `log/_log.py:255`
  = the item id, since the task builder sets `Sample(id=item.id, …)`,
  `generate/_task.py:71`). Record `time.monotonic()` here as the cell's start.
- `on_sample_end(self, data: SampleEnd)` carries the same `sample_id` → remove the
  map entry (the existing method already increments `ended`/`errors`).

**The start map + new ctx fields.** Extend `_RunContext` (`_tracker.py:58`), reset in
`tracking()` (`_tracker.py:191`) like the other counters:

- `inflight: dict[str, _Inflight]` — `_Inflight(model: str, item: str,
  start_monotonic: float, attempt: int)`, keyed by `sample_id`.
- `task_models: dict[str, str]` — `eval_id → model`, filled by `on_task_start`.
- `last_end_monotonic: float` — `time.monotonic()` of the last `on_sample_end`
  (set in that method); seeds "no completion for Ns". Starts at `start_monotonic`.
- `straggler_task: "asyncio.Task | None"` — the timer handle (launched in
  `on_run_start`, cancelled+awaited in `on_run_end`). Runtime object; never serialized.

**Emit-gating (no competition with the per-sample line).** The shipped `_emit`
(`_tracker.py:144`) throttles per-sample lines to one per `_MIN_INTERVAL_S` (10s) and
records `last_emit` (monotonic). The straggler tick reuses `last_emit`: it emits only
when `now − ctx.last_emit ≥ _STRAGGLER_INTERVAL_S` (~30s) — i.e. no per-sample line
recently, which during a healthy run (completions ≤10s apart) never trips. When it
emits it sets `last_emit = now`, so during a long stall it self-throttles to one
straggler line per ~30s (the keepalive cadence). This is precisely the BACKLOG's
"ticks only when no sample has ended since the last emitted line".

**Constants (fixed internal defaults, not knobs — see UX checklist #6).**
`_STRAGGLER_INTERVAL_S = 30.0` (tick + emit cadence), `_STRAGGLER_THRESHOLD_S = 120.0`
(min elapsed to list a cell), `_STRAGGLER_CAP = 10` (max cells listed). The BACKLOG's
open question ("threshold a knob or a fixed default — lean fixed default first") is
resolved **fixed**; matches `live-tracker`'s "no new knob".

**Reusable formatting.** `_fmt_duration` (`_tracker.py:88`) already renders coarse
durations (`13m`, `2m`); reuse it for elapsed + stall age.

**UX checklist (the binding nine — all answered, mirrors live-tracker).**
1. **Side effects:** none — stderr text only, no network/cache/lock/provider state →
   no ledger row. 2. **Quotable summary:** the straggler block is itself quotable
   self-contained lines with numbers; the end-of-run summary block is unchanged.
3. **JSON parity:** ephemeral liveness with **no fact of record** (Law 8) → no JSON
   field; `--json` stdout stays exactly one JSON document (lines go to stderr, same as
   the shipped heartbeat). 4. **Doc anchor:** wiki **Agent-Guide** (liveness under
   `--json`) — one added note on the stall/straggler line. 5. **Hint candidate:**
   none — no new silent failure mode (the line *is* the visibility). 6. **Knob
   bucket:** **no new knob** (threshold/interval/cap are fixed internal defaults).
7. **Consent:** no spend, no row replacement → not gate-related. 8. **Surface
   parity:** fires in the shared eval path, so the Python API (`run_generate` /
   `run_grade`) gets it too; never prompts. 9. **Stability:** **no new exit code /
   JSON key / hint code.**

---

## W1 — the timer + straggler render (`_tracker.py`)

**Goal.** During a `generate`/`grade` run whose display is silenced, when no sample
has completed for ~30s, emit a plain-text stderr block naming the slowest in-flight
cells, so a hung provider call is visible (which model, which item, how long) instead
of a frozen line. Example:

```
[itemeval] generate · no completion for 3m · 3 cell(s) in-flight >2m:
[itemeval]   openrouter/anthropic/claude-opus-4.8 · item aime-17 · 13m
[itemeval]   openrouter/openai/gpt-5.1 · item aime-03 · 4m
[itemeval]   openrouter/google/gemini-2.5-pro · item aime-22 · 2m
```

**Config / public surface.** None. No new knob, no new result field (Law 8). Not
exported from `itemeval/__init__.py` — `_tracker` is internal/extension-tier.

**Mechanism (file:line level).** All in `src/itemeval/_tracker.py`:

- `import asyncio` at the top (alongside `sys`, `time`).
- Add the four fields above to `_RunContext` (`:58`) and reset them in `tracking()`
  (`:191`) — `inflight={}`, `task_models={}`, `last_end_monotonic=start`,
  `straggler_task=None`. (Use `field(default_factory=dict)` for the maps.)
- New pure function `render_stragglers(inflight, now, *, stage, last_end_monotonic,
  threshold_s=_STRAGGLER_THRESHOLD_S, cap=_STRAGGLER_CAP) -> "str | None"`:
  filter cells with `now − start ≥ threshold_s`, sort slowest-first, build the header
  (`no completion for {_fmt_duration(now − last_end_monotonic)} · {K} cell(s)
  in-flight >{_fmt_duration(threshold_s)}:`) + up to `cap` `  {model} · item {item} ·
  {_fmt_duration(elapsed)}` lines (+ the `try N` suffix from W2) + a `  +{N} more`
  line when over cap. Returns `None` when no cell is over threshold. Pure → unit
  tested without inspect.
- New pure helper `_straggler_due(ctx, now) -> bool`: `ctx.active and (now −
  ctx.last_emit) ≥ _STRAGGLER_INTERVAL_S`. (Tested purely.)
- New `async def _straggler_loop()`: `while _CTX.active: await
  asyncio.sleep(_STRAGGLER_INTERVAL_S); now = time.monotonic(); if not
  _straggler_due(_CTX, now): continue; line = render_stragglers(dict(_CTX.inflight),
  now, stage=_CTX.stage, last_end_monotonic=_CTX.last_end_monotonic); if line:
  _CTX.last_emit = now; _safe_stderr(line)`. Wrap the body so a formatting slip never
  kills the loop; swallow `asyncio.CancelledError` on teardown. Snapshot the map
  (`dict(...)`) before rendering — `on_sample_end` may mutate it concurrently in the
  same loop (cooperative, but cheap insurance). Skip the timer entirely in **batch
  mode** (`_CTX.batch`): batch liveness is already provider-paced by the ~15s status
  poll (`_on_batch_status`, `:154`), and there is no per-sample "stall" to detect.
- On `LiveTracker` (`:230`), add/extend hook methods (all guarded by the existing
  `enabled()`):
  - `on_run_start`: `if _CTX.active and not _CTX.batch: try: _CTX.straggler_task =
    asyncio.create_task(_straggler_loop()) except RuntimeError: pass` (defensive
    degrade if not on asyncio).
  - `on_run_end`: `t = _CTX.straggler_task; _CTX.straggler_task = None; if t: t.cancel()`
    then `await` it suppressing `CancelledError` (in-loop teardown before `eval()`
    returns).
  - `on_task_start`: `_CTX.task_models[data.eval_id] = data.spec.model`.
  - extend `on_sample_start` (currently `++started`, `:234`): also
    `_CTX.inflight[data.sample_id] = _Inflight(model=_CTX.task_models.get(data.eval_id,
    "?"), item=str(data.summary.id), start_monotonic=time.monotonic(), attempt=1)`.
  - extend `on_sample_end` (`:237`): also `_CTX.inflight.pop(data.sample_id, None)`
    and `_CTX.last_end_monotonic = time.monotonic()` (before the existing `_emit`).

**Inspect boundary.** Extension module; new hook methods + the in-loop asyncio task
use only published hook events (`RunStart`/`RunEnd`/`TaskStart`/`SampleStart`/
`SampleEnd`) and `EvalSpec.model` (a flat str). No new private-symbol reach beyond the
two the predecessor already documents (`init_hooks` pre-latch, batch callbacks).

**UX contract.** The straggler block is **live progress** (ephemeral, stderr) —
interaction strength *below* hint (decoration, not commentary); never blocks, never
changes behavior, carries no fact of record. Nothing to announce (Law 1: no side
effect). No ledger row, no hint-catalog row, no UX-PATTERNS table change (it is the
existing stderr heartbeat under a stall).

**Tests** (`tests/test_tracker.py`, hermetic):
- `render_stragglers`: feed a constructed `inflight` dict at a fixed `now` — assert
  the header counts cells over threshold, lists them slowest-first, formats elapsed
  via `_fmt_duration`, honors the cap with `+N more`, and returns `None` when all
  cells are under threshold.
- `_straggler_due`: true only when active and `now − last_emit ≥ interval`.
- Map maintenance via direct hook drive (the existing `asyncio.run(hook.on_*(...))`
  pattern, cf. `test_no_duplicate_final_heartbeat_line`): drive `on_task_start` →
  `on_sample_start` (assert `inflight` gains the entry with the right model/item) →
  `on_sample_end` (assert it is removed and `last_end_monotonic` advanced).
- Timer skipped in batch mode: with `tracking(..., batch=True)`, `on_run_start` leaves
  `straggler_task is None`.
- No e2e "force a 120s stall" test — mockllm completes instantly; the stall path is
  covered by the pure `render_stragglers` + `_straggler_due` + the map-drive tests.
  (Document this gap inline so it is a deliberate scope choice, not an oversight.)

**Docs/CHANGELOG.** CHANGELOG `[Unreleased]` Added entry with `Closes:
straggler-heartbeat` (one entry covering W1+W2). Wiki Agent-Guide: one sentence that
the `--json` heartbeat now surfaces slow/hung cells during a stall. Remove the
`straggler-heartbeat` section from `docs/BACKLOG.md` in the shipping commit. ROADMAP
is **not** touched — `straggler-heartbeat` is an unscheduled Tier-2 BACKLOG item, not
named in ROADMAP, so there is no `**Already landed**` move (the consistency check only
fires for keys ROADMAP names).

---

## W2 — per-sample retry annotation (`try N`)

**Goal.** Append `· try N` to a straggler line when that cell is on its 2nd+ inspect
attempt, so "hung" (stuck on attempt 1) reads differently from "retrying" (transient
hiccup, likely to clear). This is the single highest-value straggler annotation and
directly answers the BACKLOG's open question.

**Correction to the BACKLOG (committed in step 3).** The open question states the
retry count "waits on an inspect retry hook". That hook **exists** in 0.3.239:
`on_sample_attempt_start(self, data: SampleAttemptStart)` (`hooks/_hooks.py:472`),
"Fired at the beginning of every attempt (including the first) … this fires on
retries too", with `SampleAttemptStart.attempt: int` (1-based) + `sample_id`
(`hooks/_hooks.py:179`). itemeval sets `retry_on_error=1` (`generate/_run.py:250`), so
attempts reach 2. The cross-eval reroute / `on_empty` reruns named in the open
question remain genuinely between-eval (not per-sample-observable) — those stay out of
scope, unchanged.

**Mechanism.** On `LiveTracker`, add `on_sample_attempt_start`: `e =
_CTX.inflight.get(data.sample_id); if e is not None: e.attempt = data.attempt`. In
`render_stragglers`, append `f" · try {attempt}"` only when `attempt >= 2` (no `/max`
— inspect's event carries no ceiling, and `retry_on_error=1` is the implicit cap;
adding a fabricated max would violate "pass through, don't rename"). `_Inflight`
already carries `attempt` (default 1 from W1), so W2 is purely the new hook method +
the render suffix.

**UX contract.** Same as W1 — ephemeral stderr liveness, no fact of record. (Inspect's
retry count is also written to the `.eval` and `EvalSampleSummary.retries`, so this is
not the sole channel — it is convenience liveness for a number already on record.)

**Tests.** Extend the `render_stragglers` test with an `attempt=2` cell → asserts
`try 2` appears and `attempt=1` cells show no `try` suffix. Drive
`on_sample_attempt_start` and assert the map entry's `attempt` updates.

**Docs/CHANGELOG.** Folded into W1's single CHANGELOG entry (mention "with retry
state"). The BACKLOG correction (above) is committed in step 3 *before* branching,
as part of the BACKLOG-fix `docs:` commit, so the shipping branch's BACKLOG change is
a clean section deletion.

---

## Sequencing (canonical)

1. **W1** — ctx fields + `render_stragglers` + `_straggler_due` + `_straggler_loop` +
   the run/task hooks + the `on_sample_start`/`on_sample_end` extensions + tests.
   The core; everything else hangs off the start map.
2. **W2** — `on_sample_attempt_start` + the `try N` render suffix + tests. Additive;
   depends only on W1's `_Inflight.attempt` field.

Then the same-change paperwork in the shipping commit: CHANGELOG `[Unreleased]` Added
with `Closes: straggler-heartbeat`, `docs/BACKLOG.md` section removal, the wiki
Agent-Guide note. (The BACKLOG open-question correction lands earlier, in the step-3
`docs:` commit on `main`, so the branch diff is a clean deletion.)

After each step: `make check` (lint + fast tests). The live smoke (`make test-live`)
runs automatically as the CC pre-push gate — confirm a real two-model `--json` run
still emits the heartbeat and stdout stays one JSON doc (the straggler block won't
fire on the fast smoke, but the regression net is the banner/JSON-purity guard).

## Out of scope (explicitly, to prevent creep)

- **A threshold/interval/cap knob** — fixed internal defaults (BACKLOG open question
  resolved); revisit only on demonstrated demand (UX-PATTERNS bucket would be
  optimization → default).
- **`reroute N/max` and `on_empty rerun aN` annotation** — these happen *between*
  evals (itemeval orchestration), not inside a sample, so they are not observable from
  the per-sample hooks. The run-level `attempt`/`experiment_id` already on the
  heartbeat line covers the cross-eval dimension.
- **A new JSON field / knob / hint / exit code** — none; pure liveness (Law 8).
- **isatty/background display detection** — inherited from `live-tracker`: the gate is
  `display == "none"`; agents are steered to `--json`, which sets it.
- **An e2e test that forces a real stall** — mockllm is instant; the stall logic is
  pure-function tested instead (documented in W1 tests).
