# Implementation plan — live-tracker (live stderr heartbeat + `--json`-dark fix)

**Status: IMPLEMENTED 2026-06-20.** Shipped on `feat/live-tracker` (CHANGELOG
`Closes: live-tracker`). Both workstreams landed as one commit: the `SampleEnd`/
`SampleStart` heartbeat hook (`_tracker.py` + `run_condition_evals` wiring) and the
`--json` pre-flight ETA on stderr (closing the `--json`-dark KNOWN-ISSUE). The
banner pre-latch + a fresh-process guard test keep `--json` stdout one clean JSON
doc; the gate is `resolve_display == "none"`. The deferred "live store during the
run" (S's L3) was **dropped**, not built (see Out of scope). Written against
inspect_ai 0.3.239 (pinned in `uv.lock`). This file is the design record; below was
the working brief. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing law here is
   **Law 8**: progress is ephemeral decoration; *no fact may exist only in
   ephemeral output*. The heartbeat therefore carries **zero facts of record** —
   everything in it (final counts, spend) also lives in the existing summary block
   and result JSON. The **relay rule** (a plain factual line survives an agent's
   summary; a progress bar that never rendered off-TTY does not) is why the
   heartbeat is plain text on stderr, not a bar.
3. `DEVELOPMENT.md` — inspect boundary rules (mandatory, this wraps inspect): wrap
   don't fork; pass through don't rename; flatten at the public API; inspect
   imports confined to task-builder / orchestrator / **extension** modules.
4. `docs/KNOWN-ISSUES.md` → "A paid run under `--json` goes dark" — this feature
   **closes it** (remove that section in the same change).
5. This file end-to-end before coding — W1 and W2 share the display-gating decision.

Scope: 2 workstreams. **W1** the heartbeat hook · **W2** the `--json`-dark ETA fix.

---

## Context: the facts that decide the design

**What's being solved.** A running `generate`/`grade` stage goes dark when
inspect's rich progress display isn't rendering — under `--json` (which forces
`display="none"`), `--display none`, or off-TTY. The operator then has no liveness,
no live ETA, and resorts to guessing from a log file's mtime. The pre-flight ETA is
a coarse static prior that never updates with real throughput.

**The single eval seam.** Both stages run **one** `inspect_ai.eval()` over all
conditions via `run_condition_evals(tasks, *, stage, experiment_id, attempt, study,
display, log_dir, max_tasks)` in `src/itemeval/generate/_run.py:186` (called from
`run_generate` at `:767` and from `run_grade` at `src/itemeval/grade/_run.py:634`).
This is the one place to set/clear the hook's run context and to compute the
heartbeat's `total`.

**inspect hooks API (verified in `.venv` inspect 0.3.239).**
- Registration: `@hooks(name, description)` on a subclass of `Hooks`
  (`inspect_ai/hooks/_hooks.py:542`) — `registry_add`s an **instance** at import
  time. Per-sample lifecycle methods: `async on_sample_start(self, data:
  SampleStart)` (`:419`) and `async on_sample_end(self, data: SampleEnd)` (`:444`,
  "called once per epoch … after the sample completed or errored with no retries
  remaining"). `SampleEnd.sample` is a full `EvalSample` (carries `.error`, scores,
  metadata); `SampleEnd` also has `run_id`/`eval_id`/`sample_id`.
- Emission is **in-process and awaited inside `eval()`**: `_eval/task/run.py:1478`
  `await emit_sample_end(...)`, which `_emit_to_all` (`_hooks.py:977`) awaits on
  every enabled hook. **Consequence:** a slow hook delays the eval — the heartbeat
  must be a cheap counter + a throttled write, never any I/O of size.
- `enabled()` (`_hooks.py:334`, default `True`) is checked **per-emit** by
  `_emit_to_all` → a dynamic per-run gate works. `get_all_hooks()` (`:943`) caches
  against `(registry_version, len(registry))` and re-scans on any registry change,
  so a hook registered at import is found on the next emit regardless of init order.
- **`eval()` has no scoped `hooks=` parameter** (`_eval/eval.py:92`) → global
  registration is the only path.

**The banner hazard (the one tricky boundary detail).** `init_hooks()`
(`inspect_ai/hooks/_startup.py:15`) is called from `platform_init` and `get_model`
(`model/_model.py:1760`), **latched once per process** (`_registry_hooks_loaded`).
When it first runs, if *any* hook is registered it appends a `hooks enabled: N …`
message and `print(...)`s it — and `_startup.py:4` is `from rich import print`,
which writes to **stdout**. Under `--json` that corrupts the one-JSON-doc contract.
Mitigation: **call `init_hooks()` once to latch it *before* we register our hook**
(at the top of `_tracker.py`, before the `@hooks` class is defined). With nothing
of `type=="hooks"` registered at that moment, the latched banner is empty; our
later registration is still discovered by `get_all_hooks()` for emission. A guard
test pins this (W1 tests) — assert `--json` stdout parses as exactly one JSON doc.

**The display gate.** `resolve_display(display)` (`src/itemeval/cli.py:163`) returns
`display or INSPECT_DISPLAY or "rich"`; `_run_stage` sets `display="none"` under
`--json` (`cli.py:567`). The heartbeat fires **iff `resolve_display(display) ==
"none"`** — exactly the dark cases (`--json`, `--display none`), and never when the
rich bars are carrying liveness, and never in a notebook (display is not "none"
there). This is the simplest correct gate; an `isatty`/background refinement is out
of scope (agents are steered to `--json`, which sets `none`).

**The heartbeat `total`.** `run_condition_evals` receives the built `tasks`; the
expected sample count is `Σ len(task.dataset) × epochs(task)` (inspect emits
`on_sample_end` once per sample per epoch). Compute it there; if a task's size is
not cheaply countable, fall back to `total=None` (heartbeat shows
`done · rate · errors`, ETA omitted). **[verify]** the Task attribute used for the
per-task dataset length + epochs against inspect 0.3.239 when wiring.

**Reusable formatting.** `_fmt_duration` (`cli.py:38`) is the coarse duration
format the pre-flight `_eta_line` already uses; the heartbeat reuses the same idea.
Keep a tiny local formatter in `_tracker.py` (≤5 lines) rather than importing cli
into an extension module (cli imports the world).

**UX checklist (the binding nine — all answered).** 1 side effects: **none**
(stderr text, no network/cache/lock/provider state) → no ledger row. 2 quotable
summary: the existing end-of-run summary block is unchanged; heartbeat lines are
themselves quotable. 3 JSON parity: heartbeat is ephemeral liveness (Law 8) with
**no fact of record** → no JSON field; `--json` stdout stays pure JSON (banner
pre-latch + heartbeat on stderr). 4 doc anchor: wiki **Agent-Guide** (liveness
under `--json`) + a line in the CLI/Outputs page. 5 hint candidate: none — no new
silent failure mode. 6 knob bucket: **no new knob**. 7 consent: no spend, no row
replacement → not gate-related. 8 surface parity: fires in `run_condition_evals`,
so the Python API (`run_generate`/`run_grade`) gets it too; it never prompts. 9
stability: **no new exit code / JSON key / hint code**.

---

## W1 — the heartbeat hook (`_tracker.py` + `run_condition_evals` wiring)

**Goal.** During a `generate`/`grade` run whose display is silenced, emit a
throttled plain-text liveness line to stderr:
`[itemeval] generate · exp a7b3c9d2/a1 · 142/400 (35%) · 11/min · ~3m left · 2 errors · 8 in-flight`.
So a `--json`/backgrounded paid run shows it is alive and gives a *live* ETA.

**Config / public surface.** None. No new knob, no new result field (Law 8: the
heartbeat is ephemeral; facts of record stay in the existing summary/JSON). Not
exported from `itemeval/__init__.py` — `_tracker` is internal/extension-tier.

**Mechanism (file:line level).**
- New `src/itemeval/_tracker.py` (extension module; inspect import allowed here):
  - At import, **pre-latch**: `from inspect_ai.hooks._startup import init_hooks;
    init_hooks()` with a comment explaining the banner-avoidance (boundary bend,
    documented per DEVELOPMENT.md "bypass … and say why in code").
  - A module-level dataclass `_RunContext` (active: bool, stage, experiment_id,
    attempt, total: int|None, started: int, ended: int, errors: int, start_monotonic,
    last_emit_monotonic) held in a module global, plus a `tracking(...)`
    context manager that populates it on enter and resets `active=False` on exit
    (try/finally). `time.monotonic()` is fine in normal Python (the Date/random
    restriction is Workflow-script-only).
  - `@hooks(name="itemeval/live-tracker", description="…")` class `LiveTracker(Hooks)`
    overriding `enabled(self)->bool` = `_CTX.active`, `on_sample_start` (++started),
    `on_sample_end` (++ended; ++errors when `data.sample.error` is not None; then
    `_maybe_emit()`). `_maybe_emit` throttles (emit if ≥ ~10s since last or on the
    final sample) and writes one line to `sys.stderr`. All wrapped so a formatting
    error never propagates into the eval (inspect already try/excepts hooks, but be
    defensive).
- `src/itemeval/generate/_run.py`: add `import itemeval._tracker` at top (registers
  the hook before the first `eval()` — the module already imports `inspect_ai`, so
  no new lazy-import cost on no-API paths, which don't import `generate._run`). In
  `run_condition_evals`, before `inspect_ai.eval(...)`: compute
  `heartbeat = resolve_display(display) == "none"` and `total` from `tasks`, and
  wrap the eval call in `with _tracker.tracking(stage, experiment_id, attempt,
  total, enabled=heartbeat): …`. `resolve_display` lives in `cli.py` — to avoid an
  orchestrator→cli import, move `resolve_display` to a neutral module (it only reads
  env) or inline the one-liner in `_run.py`; **lean: inline** `(display or
  os.environ.get("INSPECT_DISPLAY") or "rich") == "none"` in `_run.py` (it already
  imports `os`).

**UX contract.** The heartbeat is **live progress** in the channel spec
(ephemeral, stderr) — interaction strength *below* hint (it is decoration, not
commentary), never blocks, never changes behavior. Nothing to announce (Law 1: no
side effect). No ledger row, no hint-catalog row.

**Tests** (`tests/test_tracker.py`, hermetic with `mockllm/*`):
- Drive a tiny `run_generate` with `display="none"` capturing stderr; assert ≥1
  `[itemeval] generate · …` line appears and the final one shows `N/N`.
- With `display="rich"` (or default), assert **no** `[itemeval]` heartbeat on stderr.
- **Banner guard:** run `generate --json` (or the Python path with `display="none"`)
  and assert captured **stdout** parses as exactly one JSON document (no
  `hooks enabled` banner leaked). This is the regression net for the pre-latch.
- Unit-test the throttle + line formatting as a pure function (feed counters/clock,
  assert the rendered string) so logic is covered without inspect.

**Docs/CHANGELOG.** CHANGELOG `[Unreleased]` Added entry with `Closes: live-tracker`
(written in W2's commit alongside the KNOWN-ISSUE removal, or here — keep one
`Closes:` for the feature). Wiki Agent-Guide gains a short "liveness under `--json`"
note. Remove the `live-tracker` section from `docs/BACKLOG.md` in the shipping
commit.

---

## W2 — the `--json`-dark pre-flight ETA fix (`cli.py`)

**Goal.** Close the KNOWN-ISSUE: under `--json`, the pre-flight ETA line is
currently inside `if not args.json:` (`cli.py:510`) and the live display is off, so
the run is dark until the first heartbeat. Echo the pre-flight ETA (and a one-line
"starting <stage>" with the call count) to **stderr** under `--json`, so the dark
window before the first sample completes still shows intent + a rough ETA.

**Mechanism.** In `_run_stage` (`cli.py:498`), when `args.json`, after the gate
passes and before `runner(...)`, print the `_eta_line(st)` (and a terse
`starting {stage}: {st.remaining_calls} calls` line) to **stderr**. The non-`--json`
human path is unchanged (it already prints the ETA to stdout among the pre-flight
block, and gets the rich display). No stdout change under `--json` (stays pure JSON).

**UX contract.** stderr commentary about the run (same channel as hints) — no
behavior change, no block. No new JSON field (the ETA fields already ride
`StageEstimate` in `estimate --json`).

**Tests** (extend `tests/test_cli*.py`): `generate --json` on a mock study →
captured stdout is one JSON doc; captured stderr contains the ETA/starting line.

**Docs/CHANGELOG.** Remove the "A paid run under `--json` goes dark" section from
`docs/KNOWN-ISSUES.md` in this same change. The feature's CHANGELOG entry notes it
closes that liveness gap. Relax the Agent-Guide carve-out ("prefer `--json` only on
no-cost commands") back toward "`--json` everywhere is safe — liveness rides
stderr" (W1's heartbeat + W2's ETA).

---

## Sequencing (canonical)

1. **W1** — `_tracker.py` + `run_condition_evals` wiring + tests (incl. the banner
   guard). The core; everything else is additive.
2. **W2** — the `--json` pre-flight ETA on stderr + KNOWN-ISSUE removal + Agent-Guide
   relax + CHANGELOG `Closes: live-tracker` + BACKLOG removal (the same-change
   paperwork lands with W2 so the branch diff is one clean feature).

After each step: `make check` (lint + fast tests). The live smoke
(`make test-live`) runs automatically as the pre-push gate — confirm the heartbeat
shows under a real two-model `--json` run there.

## Out of scope (explicitly, to prevent creep)

- **Per-sample parquet flush / "live store during the run"** (S's deferred L3) —
  killed by the design pass: redundant (recoverable-harvest already projects the
  incremental `.eval` on read) and harmful (hook awaited in-eval, parquet O(N²)).
  The "truly live store" is inspect's WAL + the read-triggered projection.
- **`status --watch`** — `watch itemeval status` already works (status auto-harvests
  the in-flight `.eval`). Revisit only on demand.
- **isatty/background display detection** — v1 gates on `display == "none"`; agents
  are steered to `--json`, which sets it. A backgrounded run with no flags can pass
  `--display none`.
- **In-flight liveness via mid-run `.eval` metadata** — depends on the open question
  (does a partial `.eval` carry experiment_id/attempt early?); the in-process
  heartbeat is robust regardless, so this is a free bonus only if metadata is early.
- **A new JSON field / knob / hint / exit code** — none; this is pure liveness.
