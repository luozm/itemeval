# Implementation plan — recoverable-harvest (make a crashed run's partial progress durable)

**Status: IN PROGRESS (started 2026-06-20).** Written 2026-06-20 against the
current `main` (post-#18/#19) and inspect_ai as pinned in `uv.lock`. **Re-verified
2026-06-20** against the post-`parallel-conditions` tree — the anchors below are
current; deltas vs the first draft are captured in *Re-verification* at the end of
Context. This file is the working brief for a fresh implementation session and
carries all the context that session needs (assume no conversation history — only
this file and the repo). Read these first, in order:

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
conditions inside `run_condition_evals`
([generate/_run.py:195](../../src/itemeval/generate/_run.py#L195), called from
Phase 2 at [:668](../../src/itemeval/generate/_run.py#L668)), then harvests
per-condition into parquet (Phase 3,
[:678-747](../../src/itemeval/generate/_run.py#L678-L747)) from the **in-memory**
logs the call returns — never the on-disk `.eval`, and with no `try/finally`
salvage. So Phase 3 only runs if `eval()` returns; a SIGKILL/OOM (or the §2.J
force-kill of a stuck SSL read) writes zero rows. grade is symmetric: judge rows
are built by `_judge_rows`
([grade/_run.py:162-216](../../src/itemeval/grade/_run.py#L162-L216)) and harvested
in its Phase 3 ([:552-612](../../src/itemeval/grade/_run.py#L552-L612)).

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
  ([generate/_task.py:104-111](../../src/itemeval/generate/_task.py#L104-L111) —
  the generate task **also** carries `["epoch_offset"]`;
  [grade/_judge.py:169](../../src/itemeval/grade/_judge.py#L169)). The run-level
  `itemeval_run_id` rides the top-level eval metadata
  ([generate/_run.py:205](../../src/itemeval/generate/_run.py#L205)). So a stray
  `.eval` is self-describing — no extra context to map it back to its condition or
  its originating run.
- **inspect exposes the readers** (importable from `inspect_ai.log`, verified
  2026-06-20): `read_eval_log(path, header_only=False)`
  ([.venv inspect_ai/log/_file.py:264](../../.venv/lib/python3.12/site-packages/inspect_ai/log/_file.py#L264))
  and `list_eval_logs(log_dir)` → `EvalLogInfo[]`
  ([:88](../../.venv/lib/python3.12/site-packages/inspect_ai/log/_file.py#L88)).
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

### Re-verification (2026-06-20, post-`parallel-conditions`)

The first draft's anchors predated the `parallel-conditions` ship; re-checked
against the current tree. **The design holds unchanged**; three implementation
facts shifted and one new decision surfaced:

1. **The single `eval()` moved into a helper.** It's now `run_condition_evals`
   ([:195](../../src/itemeval/generate/_run.py#L195)); both stages call it. The
   per-condition harvest is Phase 3 (generate [:678-747], grade [:552-612]). The
   **Phase-3 refactor (W1) is a slightly wider surface** than the first draft's
   `:678-720` snapshot: the block also builds `endpoints_effective` /
   `sampling_effective` and finalizes the manifest. Scope the shared helper to the
   **durable-store writes only** — `upsert_solutions` + `upsert_log_index` +
   `upsert_ledger` (generate) / `upsert_gradings` + … (grade). Harvest does **not**
   re-finalize the crashed run's manifest provenance (`endpoints_effective` etc.)
   — that's best-effort and the live run already wrote the requested values; one
   fact, one home for the *row* write, not for manifest finalization.
2. **`rows_from_generate_log` grew** to `(log, cond, prep, run_id, epoch_offset=0,
   wave=0, wave_label=None)` ([:423-492]); harvest supplies those from the manifest
   (W1 step 5 above). `_judge_rows(prep, cond, pending, log, run_id)` is unchanged
   ([:162-216]) and still needs `pending` rebuilt from `solutions` filtered to the
   log's sample keys (`gen_condition_id`/`item_id`/`epoch` on each sample's
   metadata, [grade/_run.py:171](../../src/itemeval/grade/_run.py#L171)).
3. **`build_status` builds its own `prep` when none is passed**
   ([_status.py:87-91](../../src/itemeval/_status.py#L87-L91)); `export_study` is
   the analogous public function. The CLI passes a `prep` in, so injecting
   auto-harvest in `_cmd_status`/`_cmd_export` (after `_load`, before the read) is
   clean. **New decision (D3 below):** whether the *Python* `build_status(cfg)` /
   `export_study(cfg)` calls also auto-harvest, or stay pure reads with a separate
   explicit `harvest_study(prep)` — a library writing silently on a read is a
   sharper surprise in a notebook than at a CLI (UX "a library never prompts/…"
   spirit). Recommendation in D3.

**Public-surface tripwire:** adding the `harvest` command flips
`CLI_COMMANDS` in [tests/test_public_api_snapshot.py](../../tests/test_public_api_snapshot.py);
a public `harvest_study` (if D3 adds one) flips `PUBLIC_API` there too. Deliberate
golden-set bump in the same commit (step 9 of the feature flow).

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
5. `run_id` + **epoch/wave identity**: harvested rows carry the `run_id` recorded
   in the log (`itemeval_run_id` in the top-level eval metadata, [:205]), not a
   fresh one — this is *recovering* an existing attempt. `rows_from_generate_log`
   now also requires `epoch_offset`/`wave`/`wave_label`; recover them from the
   crashed run's manifest `manifests/<run_id>.json`
   ([_manifest.py:200](../../src/itemeval/_manifest.py#L200)), which is written
   **before** the eval ([generate/_run.py:546](../../src/itemeval/generate/_run.py#L546),
   [grade/_run.py:427](../../src/itemeval/grade/_run.py#L427)) so it survives a
   hard kill. (`epoch_offset` is also on the generate task metadata, but
   `wave_label` lives only in the manifest + the rows, so the manifest is the one
   source that covers both.) (Under R this becomes the log's
   `experiment_id`/`attempt`.)

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

## Design decisions (confirmed 2026-06-20 at the brief-review gate)

All three confirmed at the recommended values below.

**D1. First-ship scope = W1 + W2 (read-triggered), live hook deferred.** The
inspect hooks API (`on_sample_end`,
[.venv inspect_ai/hooks/_hooks.py:315-537](../../.venv/lib/python3.12/site-packages/inspect_ai/hooks/_hooks.py#L315-L537))
would flush rows *during* the run (a truly live store), but it's a bigger
integration and the **same hook C wants for its heartbeat** — so it ships with
C, not here. W1+W2 (read-triggered projection) already removes the blindness.
*Recommend: confirm this split.*

**D2. Auto-harvest-on-read defaults ON (announced), `--no-harvest` to opt out.**
The alternative — explicit `harvest` only, never auto — keeps reads pure but
re-introduces "the store is stale until you remember to harvest." Recommend
default-on (idempotent, announced, recovers your own data); the UX call is
yours since it's a read command that writes.

**D3. Auto-harvest scope = CLI commands only; Python `build_status`/`export_study`
stay pure reads.** Add an explicit public `harvest_study(prep) -> HarvestReport`
for the Python surface; the CLI's `status`/`export`/generate-prepare call it
before reading (announced, D2). Rationale: a CLI command that announces a write is
fine (Law 1), but a *library* function silently writing to disk when a notebook
asked it to *read* is the surprise UX-PATTERNS works hardest to prevent. This also
keeps `prepare_study`/`build_status`/`export_study` contracts pure (no hidden
side effect), matching today's "prepare is side-effect-free-ish" shape.
*Recommend: confirm CLI-only auto + explicit `harvest_study` for Python.* If you'd
rather the Python reads also self-heal, say so — then auto-harvest moves inside
`build_status`/`export_study` (gated by a `harvest: bool = True` param).

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
