# Implementation plan — recoverable-harvest (make a crashed run's partial progress durable)

**Status: NOT STARTED.** Written 2026-06-20 against the current `main`
(post-#18/#19) and inspect_ai as pinned in `uv.lock` — re-verify the pinned
file:line facts if the tree moved. This file is the working brief for a fresh
implementation session and carries all the context that session needs (assume no
conversation history — only this file and the repo). Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing rule here:
   **no silent side effects** — auto-harvest writes to the store on a read
   command, so it must be announced (Law 1), with JSON parity and an opt-out.
3. `DEVELOPMENT.md` — inspect_ai boundary: `read_eval_log`/`list_eval_logs`/the
   hooks API are inspect imports → they live only in the orchestrator modules
   (`generate/_run`, `grade/_run`) and a new `_harvest.py`, never in config/
   store/CLI. Wrap don't fork; flatten at the boundary.
4. This file end-to-end before coding.

Scope: 2 workstreams. **W1** disk-harvest projection (harvest a log read from
disk) · **W2** unharvested classifier + auto-harvest-on-read + `harvest` command.
The live-during-run hook is **out of scope** here (ships with C — see below).

**Why this exists.** Durable parquet is written **all-or-nothing after a clean
`eval()` return**. A hard mid-run death leaves progress only in inspect's
on-disk `.eval`, which our code never reads back — so every store surface is
blind to a killed run, and a persistently flaky study that never completes one
clean `eval()` produces no reportable store at all (though ~all the data exists
in cache + `.eval`). This feature reads the `.eval` back into the stores.

**It gates `recovery-run-identity` (R).** R's "recovery fills holes" assumes
harvested rows exist; after a crash they don't until this harvests them. R's
per-experiment index also **consumes** this feature's `.eval` lifecycle
classifier (below) instead of owning supersession. Build this before R.

---

## Context: the all-or-nothing harvest, and why disk-harvest is feasible

**The gap (file:line).** `generate/_run.py` runs ONE `inspect_ai.eval()` over all
conditions (Phase 2, [generate/_run.py:666](../../src/itemeval/generate/_run.py#L666)),
then harvests per-condition into parquet (Phase 3,
[:678-720](../../src/itemeval/generate/_run.py#L678-L720)) from the **in-memory**
logs the call returns — never the on-disk `.eval`, and with no `try/finally`
salvage. So Phase 3 only runs if `eval()` returns; a SIGKILL/OOM (or the §2.J
force-kill of a stuck SSL read) writes zero rows. grade is symmetric
([grade/_run.py:162-216](../../src/itemeval/grade/_run.py#L162-L216)).

**Everything needed to harvest from disk is in the `.eval` or rebuildable:**

- **The harvest functions already consume a single `EvalLog`.**
  `rows_from_generate_log(log, cond, prep, run_id, …)`
  ([generate/_run.py:423-492](../../src/itemeval/generate/_run.py#L423-L492))
  reads only `log.samples` + `prep` (paths/origins/pricing). A log read from disk
  has the same `.samples`. The grade builder `_judge_rows(prep, cond, pending,
  log, run_id)` ([grade/_run.py:162-216](../../src/itemeval/grade/_run.py#L162-L216))
  additionally needs `pending` — the solutions rows for that grade condition —
  which we rebuild by re-querying `solutions.parquet` (the sample metadata
  carries `gen_condition_id`/`item_id`/`epoch`).
- **condition_id + stage are in the log itself:**
  `log.eval.metadata["itemeval"]["condition_id"]` and `["stage"]`, set in the task
  ([generate/_task.py:104-111](../../src/itemeval/generate/_task.py#L104-L111),
  [grade/_judge.py:169](../../src/itemeval/grade/_judge.py#L169)). So a stray
  `.eval` is self-describing — no extra context to map it back to its condition.
- **inspect exposes the readers:**
  `read_eval_log(path, header_only=False)` and
  `list_eval_logs(log_dir)` →`EvalLogInfo[]`
  ([.venv inspect_ai/log/_file.py:264-301](../../.venv/lib/python3.12/site-packages/inspect_ai/log/_file.py#L264-L301)).
- **Re-harvest is idempotent.** `upsert_solutions`/`upsert_gradings` dedup on the
  content key (`drop_duplicates(keep="last")`,
  [store/_base.py:44](../../src/itemeval/store/_base.py#L44)), so harvesting the
  same `.eval` twice can't duplicate rows — harvest is safe to run repeatedly.
- **`log_file` paths are stored relative to study_dir**
  (`rel_to_study`, [store/_base.py:58](../../src/itemeval/store/_base.py#L58)),
  so "which `.eval` are already harvested" is a set-compare against
  `solutions.log_file` / `gradings.log_file`.

**One subtlety (drives W2's detection):** `log_index.parquet` records which logs
were *indexed*, not which were *harvested into rows*. Detect unharvested by
comparing on-disk `.eval` (via `list_eval_logs`) against the `log_file` values
present in `solutions`/`gradings` — not against `log_index`.

---

## W1 — Disk-harvest projection

**Goal.** One function that harvests a stage's `.eval` files from disk into the
stores, reusing the existing row builders — so a crashed run's progress can be
made durable without re-running.

**Config / public surface.** No new knob. New internal module `_harvest.py`.

**Mechanism (file:line).** New `_harvest.harvest_stage(prep, stage) -> HarvestReport`:
1. `list_eval_logs(prep.paths.logs_stage_dir(stage))`; filter to logs **not**
   already in `solutions.log_file`/`gradings.log_file` (W2's classifier).
2. For each: `log = read_eval_log(path)`;
   `cid = log.eval.metadata["itemeval"]["condition_id"]`; look up the
   `GenCondition`/`GradeCondition` in `prep.grid` by id.
3. **Generate:** `rows = rows_from_generate_log(log, cond, prep, run_id=…)` →
   `upsert_solutions`/`upsert_log_index`/`upsert_ledger` — the *same* Phase-3
   calls, factored so both the live path and this path call one helper.
4. **Grade:** rebuild `pending` by reading `solutions` for the
   `(gen_condition_id, item_id, epoch)` keys present in the log's samples, then
   `_judge_rows(prep, cond, pending, log, run_id)` → `upsert_gradings` etc.
5. `run_id`: harvested rows carry the `run_id`/identity recorded in the log's
   manifest, not a fresh one — this is *recovering* an existing attempt, not a
   new invocation. (Under R this is the log's `experiment_id`/`attempt`.)

**Refactor.** Extract Phase 3's per-condition upsert block
([generate/_run.py:678-720](../../src/itemeval/generate/_run.py#L678-L720)) into a
helper that takes `(log, cond, prep)` so the live run and disk harvest share it —
"one fact, one home" for the harvest write.

**Boundary.** `read_eval_log`/`list_eval_logs` are inspect imports → confined to
`_harvest.py` (an orchestrator-tier module). Rows out are itemeval dicts/parquet.

**Tests.** Hermetic: write a tiny `.eval` fixture (via inspect's writer or a
committed sample), harvest it, assert the expected solutions/gradings rows;
harvest twice → no duplicates (idempotency). Reuse `_mockmodels` for any
generation needed to produce the fixture. No paid APIs.

---

## W2 — Unharvested classifier + auto-harvest-on-read + `harvest` command

**Goal.** Make the store reflect reality *whenever you look*, with no silent
writes — so `status`/`export`/`report` answer "what's done" after any crash.

**Config / public surface.**
- New command `itemeval harvest CONFIG [--json]` — explicit, idempotent.
- `status`/`export`/the run `prepare` phase **auto-harvest first**, announced.
- `--no-harvest` flag to suppress the auto step (escape hatch).

**Mechanism (file:line).**
- **Classifier** `_harvest.classify_logs(prep) -> {harvested, unharvested}`:
  `list_eval_logs` per stage dir vs the `log_file` set in `solutions`/`gradings`.
  This is the function **R's per-experiment index consumes** (R adds the
  `superseded` dimension on top, keyed by attempt) — so the `.eval` lifecycle has
  one home here.
- **Injection point:** after `prepare_study`
  ([_prepare.py:73-151](../../src/itemeval/_prepare.py#L73-L151), which stays
  side-effect-free) and before `build_status`
  ([_status.py:87-92](../../src/itemeval/_status.py#L87-L92)) / before the run
  loop, call `harvest_stage` for any unharvested logs. **Do not** put harvest
  inside `prepare_study` — keep that contract pure; harvest is an explicit
  orchestration step.

**UX contract.** Auto-harvest is a **side effect on a read command** → announce
(Law 1): one stderr line, e.g. `recovered 1,240 rows from an interrupted run
(logs/generate/…) into the store`. JSON parity: a `harvested` object
(`{rows, logs, stage}`) in the `status`/`export` payload. `--no-harvest`
suppresses. Idempotent + recovers the user's own data, so default-on is safe;
hint (stable code + wiki anchor) explains it and the flag. No gate (money only).

**Tests.** A crashed-run fixture (orphan `.eval`, empty/partial store): `status`
auto-harvests and reports the rows; `--no-harvest` leaves the store untouched;
re-running `status` is idempotent (no growth). Hermetic.

---

## Design decisions to confirm at the brief-review gate

1. **First-ship scope = W1 + W2 (read-triggered), live hook deferred.** The
   inspect hooks API (`on_sample_end`,
   [.venv inspect_ai/hooks/_hooks.py:315-537](../../.venv/lib/python3.12/site-packages/inspect_ai/hooks/_hooks.py#L315-L537))
   would flush rows *during* the run (a truly live store), but it's a bigger
   integration and the **same hook C wants for its heartbeat** — so it ships with
   C, not here. W1+W2 (read-triggered projection) already removes the blindness.
   *Recommend: confirm this split.*
2. **Auto-harvest-on-read defaults ON (announced), `--no-harvest` to opt out.**
   The alternative — explicit `harvest` only, never auto — keeps reads pure but
   re-introduces "the store is stale until you remember to harvest." Recommend
   default-on (idempotent, announced, recovers your own data); the UX call is
   yours since it's a read command that writes.

---

## Sequencing (canonical)

W1 (the projection + the Phase-3 refactor) → W2 (classifier + integration +
command). One conventional commit per workstream; the final commit carries
`Closes: recoverable-harvest` and removes the BACKLOG section.

After each step: `make check`; CHANGELOG + UX-PATTERNS rows in the same commit.

**Gates R.** `recovery-run-identity` consumes `classify_logs` and assumes
harvested rows. Land this first.

## Out of scope (explicitly)

- **Live-during-run harvest (the inspect hook)** — designed for, ships with C
  (shared heartbeat hook). Until then, read-triggered projection (W2) is the
  store's freshness guarantee.
- **`.eval` supersession / pruning** — that's R's per-experiment index (it adds
  the attempt dimension on top of W2's classifier). This feature only does
  harvested-vs-unharvested.
- **Changing the content keys or the row schema** — harvest reuses the existing
  builders and keys unchanged.
- **Cross-machine / S3 logs** — `list_eval_logs` supports `fs_options`, but
  remote log dirs are out of scope; local `logs/<stage>/` only.
