# Implementation plan — provider-finish-capture (serving provider + native finish_reason in the stores/export)

**Status: NOT STARTED.** Written 2026-06-20 against inspect_ai 0.3.x (pinned in
`uv.lock`) — re-verify the `[verify]` facts below if that moved. This file is the
working brief for a fresh implementation session: it carries all context that
session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Relevant here: Law 6 (a
   *reported* fact lives in three renderings) and the side-effect ledger — this
   feature reports **nothing** at run time and writes only inside the study dir,
   so it adds **no** status line, hint, gate, or ledger row. It captures raw
   provenance columns, like `stop_reason` / `served_model` / `sample_uuid` today.
3. `DEVELOPMENT.md` — "Study-facing schema evolution": this **adds columns** to
   two persisted stores (`solutions`, `gradings`), so it must be *additive with a
   default* **and** carry an older-schema fixture guard test. Read-time backfill
   (mirroring `store/_solutions._backfill_wave`) is the mechanism. Inspect
   boundary: the `ModelEvent.call.response` read stays in the orchestrator
   modules.
4. This file end-to-end before coding.

Scope: 1 workstream. **W1** capture `served_provider` + `native_finish_reason`
on the solver call and the judge call, and carry both into the export.

---

## Context: the facts that decide the design

### The gap

OpenRouter load-balances each request across provider backends. A flaky backend
can return **HTTP 200 + `finish_reason=error`** (or any non-enum reason) **plus
empty/truncated content** — a "soft failure" that scores as a silent empty / a
false-floor partial. Two facts about *why* a cell looks the way it does live
**only in the `.eval` log** today:

- the **serving backend** — `ModelEvent.call.response["provider"]` (e.g.
  `"GMICloud"`, `"Fireworks"`); and
- the **raw finish_reason** — `choices[0].native_finish_reason`. inspect's
  `as_stop_reason` collapses any reason it doesn't recognize (including `error`)
  to `stop_reason="unknown"`, so the stored `stop_reason` cannot distinguish a
  soft failure from a genuinely unmapped stop.

A real dev run had to hand-extract these from every `.eval` to diagnose silent
empties. This is also the deferred KNOWN-ISSUES defect *"Unmapped provider
finish-reasons collapse to `unknown`"* — whose fix sketch is **exactly** "read
OpenRouter's `native_finish_reason` off the model event". Capturing both into the
stores/export discharges that issue and is the enabler for the planned
output-validity-gate / reroute work.

### Where the data is `[verify on inspect bump]`

`endpoint_info` (`generate/_run.py:429`) already walks `sample.events`, reads
`ev.call.response` (a dict), and pulls `resp.get("provider")` for the
condition-level `upstream` rollup — proof the access pattern is correct on the
pinned inspect. `native_finish_reason` lives alongside it at
`resp["choices"][0]["native_finish_reason"]`. Both are present only when the
provider returned them (OpenRouter does; mock models and cache replays do not).

### Where rows are built

- Solver: `rows_from_generate_log` (`generate/_run.py:473`) builds one solution
  row per `sample`; `stop_reason` is read at `:526`.
- Judge: `_judge_rows` (`grade/_run.py:214`) builds one grading row per judge
  `sample`. Non-judge grade rows (`_verifiable_rows`, `_oversized_rows`,
  empty-skip) make no model call — they omit the new fields and
  `_coerce_to_schema` defaults them to null on upsert.

### The store/export schema facts

- `SOLUTIONS_SCHEMA` (`store/_solutions.py:11`) and `GRADINGS_SCHEMA`
  (`store/_gradings.py:12`) are pyarrow schemas; `read_parquet_or_empty` reads the
  **raw** parquet (no read-time coercion), so a column absent from an old file is
  absent after read — hence `_backfill_wave` (`_solutions.py:62`) adds the wave
  columns on read. New columns need the same treatment.
- Export (`store/_export.py`): `_SOLUTION_COLS` renames solution columns to a
  `gen_` prefix, `_GRADING_COLS` to a `grade_` prefix; `EXPORT_SCHEMA` (`:33`)
  lists the output columns and `long = long[list(EXPORT_SCHEMA.names)]` (`:333`)
  makes the written columns equal the schema. `tests/test_export.py:38` asserts
  `list(parquet.columns) == list(EXPORT_SCHEMA.names)` — self-consistent once the
  schema and the `sol_cols` / rename maps agree.

### Naming decision

`served_provider`, **not** `provider`. `provider` is already overloaded: the
`model` namespace (`openrouter/…`) and the ledger's **billing** provider
(`ledger.provider`, the native account). `served_provider` = "the backend that
actually answered this call", unambiguous beside both. `native_finish_reason`
keeps the raw OpenRouter field name verbatim (the term used in the KNOWN-ISSUES
entry, already unambiguous).

---

## W1 — capture served_provider + native_finish_reason

**Goal.** Make the serving backend and the raw finish_reason of every solver and
judge call legible directly in `solutions.parquet`, `gradings.parquet`, and the
export — so a soft failure (and who served it) is diagnosable without opening the
`.eval`, and the downstream validity-gate/reroute feature has a detection
substrate.

**Config / public surface.** **No new knob, hint, gate, status line, or result
field** (pure provenance capture; UX Law 6 governs *reported* facts, and nothing
is reported at run time). Append-only schema additions only:
- `solutions.parquet` / `gradings.parquet`: two nullable string columns
  `served_provider`, `native_finish_reason`.
- export `gradings_long.parquet`: `gen_served_provider`, `gen_native_finish_reason`
  (solver side), `grade_served_provider`, `grade_native_finish_reason` (judge
  side) — additive to the rebuilt disposable view (no migration of the view).

**Mechanism.**
- New pure helper `served_provider_finish(sample) -> tuple[str | None, str | None]`
  in `generate/_run.py` (beside `endpoint_info`): walk `sample.events`, and for
  each event with a dict `call.response`, take the last non-empty
  `response["provider"]` and `response["choices"][0]["native_finish_reason"]`.
  Duck-typed reads (`getattr`/`.get`) so it is unit-testable with fabricated
  events and returns `(None, None)` for mock/cache samples.
- `rows_from_generate_log`: `served_provider, native_finish_reason =
  served_provider_finish(sample)`; add the two fields to the solution row dict.
- `grade/_run.py:_judge_rows`: import the helper (like `sum_usage`), add the two
  fields to the judge row dict. The non-judge row builders are left unchanged
  (null by coercion).
- `store/_solutions.py`: add the two `pa.field(..., pa.string())` to
  `SOLUTIONS_SCHEMA`; add a `_backfill_provenance(df)` (adds missing columns as
  None) and call it in `read_solutions` after `_backfill_wave`. Export the
  constant tuple so `_gradings.read_gradings` reuses it.
- `store/_gradings.py`: add the two fields to `GRADINGS_SCHEMA`; call the
  provenance backfill in `read_gradings`.
- `store/_export.py`: add `served_provider`→`gen_served_provider` and
  `native_finish_reason`→`gen_native_finish_reason` to `_SOLUTION_COLS` (+ to
  `sol_cols`); add the `grade_*` pair to `_GRADING_COLS`; add all four to
  `EXPORT_SCHEMA`. String columns need no `fillna` (a left-join miss is null, like
  `solution` today).

Rejected (kept simple): a run-time hint or status count for soft failures (that
belongs to the *output-validity* feature that consumes these columns, not to the
capture); folding `served_provider` into the manifest `upstream` rollup
(different granularity — per-row vs per-condition).

**UX contract.** Interaction strength: **none**. No side effect outside the study
dir → **no ledger row**. No reported fact → **no status line, no hint, no JSON
result field** (Law 6 is satisfied vacuously — there is nothing to render). JSON
parity: the columns are in the export parquet/CSV like any other. Doc anchor:
`Outputs-and-Schemas.md` (store + export schema tables) and a short
`Error-Handling.md` section explaining the diagnostic columns and the
soft-failure context. Consent: none (no spend, no replacement).

**Tests.** All hermetic (no paid APIs):
- `tests/test_store.py` (or `test_generate_run.py`) — unit-test
  `served_provider_finish` over a fabricated `sample` with events carrying
  `call.response = {"provider": "X", "choices": [{"native_finish_reason": "error"}]}`
  → `("X", "error")`; an events-less sample → `(None, None)`; last-non-empty wins
  across two events.
- **Older-schema fixture guard** (`tests/test_store.py`) — write a solutions (and
  gradings) parquet *without* the two columns, `read_solutions`/`read_gradings`,
  assert the columns exist and are null and the rest is unchanged (the
  additive-by-construction invariant; DEVELOPMENT.md gate item 1).
- `tests/test_export.py` — assert the four new column names are in
  `EXPORT_SCHEMA.names` and the written parquet (the existing
  `columns == EXPORT_SCHEMA.names` assertion covers ordering); a seeded grading
  with a `served_provider` value round-trips to `grade_served_provider`.
- A generate→grade integration row carries the columns (null under mock models —
  proves the plumbing exists without a real provider).

**Docs/CHANGELOG.** `[Unreleased]` `### Added` (the columns) **and** `### Fixed`
(closes the unmapped-finish-reason KNOWN-ISSUES defect — `native_finish_reason`
now recovers the reason `stop_reason` flattens to `unknown`), with
`Closes: provider-finish-capture`. **Remove** the `provider-finish-capture`
section from `docs/BACKLOG.md` and the *"Unmapped provider finish-reasons collapse
to `unknown`"* entry from `docs/KNOWN-ISSUES.md` in the same commit. ROADMAP:
add the key to the `0.3` "Already landed / in flight" line. Wiki:
`Outputs-and-Schemas.md` (the two store-schema rows + the four export rows; bump
the export column count) and an `Error-Handling.md` section. No UX-PATTERNS change
(no hint/gate/ledger surface).

---

## Sequencing (canonical)

One workstream, one `feat:` commit (W1 + same-change paperwork). Order within it:
schema columns + backfill → extractor + row wiring → export → tests → docs.
After: `make check`. The public-API snapshot is **untouched** (no `__all__` /
CLI-subcommand / result-model change — only store columns + export columns), so
`tests/test_public_api_snapshot.py` stays green. Then archive this plan
(`IMPLEMENTED <date>`, `git mv` to `docs/plans/archive/`, fix inbound links).

After each step: `make check` (lint + fast tests), CHANGELOG and normative doc
tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **Acting on a bad output** — detection/retry/reroute/flagging is the separate
  `output-validity-reroute` (§1) feature; this only *captures* the signal.
- **A run-time hint or status count for soft failures** — same: belongs to the
  consuming feature.
- **Re-keying or re-classifying anything** — the new columns are inert
  provenance; they never touch a content key, the money gate, grading
  eligibility, or `stop_reason`/`truncated`.
- **The upstream inspect flatten itself** — `as_stop_reason` collapsing reasons to
  `unknown` is upstream-rooted; we recover the raw value as a wrapper column
  rather than patching inspect (could file an inspect issue separately).
- **Non-OpenRouter providers that don't return `provider`/`native_finish_reason`**
  — the columns are simply null there; no synthesis.
