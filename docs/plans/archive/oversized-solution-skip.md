# Implementation plan — oversized-solution-skip (grade-time skip for over-long solutions)

**Status: IMPLEMENTED 2026-06-20.** Written 2026-06-20 against inspect_ai
0.3.x (pinned in `uv.lock`). This file is the design record for the feature.
Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (no silent side effects,
   the money gate is the only gate, JSON parity, append-only machine surface).
3. `DEVELOPMENT.md` — inspect_ai boundary + study-facing schema evolution
   (the new grading rows are additive on an existing schema).

Scope: 1 workstream. **W1** grade-time oversized-solution skip.

---

## Context: the facts that decide the design

A consuming study found weak models emitting 100k–376k-char repetition-loop
outputs that are not valid proofs. Sending them to the LLM judge (large input,
expensive model) wastes money; they should simply score 0.

The empty-solution skip is the exact pattern to mirror:

- `store/_solutions.py:empty_solution_mask` — the predicate (no-error rows with
  blank text). The grade flow excludes those rows from grading under the
  `solvers.on_empty: skip` policy and reports the count.
- `grade/_run.py:run_grade` computes `empty_total` / `empty_skipped`, threads
  `include_empty` through `store/_gradings.pending_solutions`, and returns them
  on `GradeResult`. `cli.py` (~line 645) prints the
  `empty solutions: N excluded from grading` summary line.
- Identity: `solvers.on_empty` is **not** in `_identity._NON_IDENTITY_SOLVERS`,
  so it enters the `experiment_id` digest; it is **not** in any grade-condition
  payload (`design/_grid.py`), so it does **not** enter the grade condition id.
  We match the *behavioural* pattern (skip + count), but NOT this identity
  treatment: `on_empty` is a `solvers` field, so re-keying generate when it
  changes is correct; `max_solution_chars` is a *grader* knob, and the shared
  `config_digest` means making it identity-bearing would wrongly re-key generate
  too. So it is non-identity (`_NON_IDENTITY_GRADER`) — see the design section.

Unlike `on_empty` (a study-wide `solvers` policy), the oversized threshold is a
**per-grader** knob (`GraderSpec.max_solution_chars`) — a study may give a
cheaper judge a higher tolerance. So the skip is applied **per judge condition**
inside the Phase-1 loop, not study-wide via `pending_solutions`.

---

## W1 — grade-time oversized-solution skip

**Goal.** Before sending a stored solution to the judge, if its visible text
length exceeds `graders.<name>.max_solution_chars`, do not call the judge:
record a grading row with `score=0`, `parse_ok=False`,
`parse_error="oversized_skip"`, `judge_completion=None`, and surface the count
in the run summary + `--json`. `None` (default) = off, no behavior change.

**Config / public surface.** New field on `GraderSpec`
(`src/itemeval/_config.py`): `max_solution_chars: int | None = Field(default=None, ge=1)`.
UX-PATTERNS bucket: **design declaration** (it changes what gets graded, hence
results — like `on_empty`). New `GradeResult` field: `oversized_skipped: int`
(append-only). No new export.

**Mechanism.**
- `store/_solutions.py:oversized_solution_mask(df, max_chars)` — no-error,
  non-empty rows whose `solution` length > `max_chars`; excludes
  `empty_solution_mask` rows so the two are disjoint (empty handled first).
- `grade/_run.py`: in the Phase-1 judge branch, after `pending_solutions`, split
  off the oversized rows for this condition's threshold, write them via
  `_oversized_rows` (a score-0 builder mirroring `_verifiable_rows` — no model
  call) + a `(oversized-skip)` ledger row, narrow `pending`, and carry the skip
  rows to Phase 3 so the condition report's row count covers them. If every
  pending row was oversized, finalize without a judge eval.
- Identity: the field is added to `GraderSpec`, **not** to the grade payload in
  `design/_grid.py` (so it never enters the condition id), **and** added to
  `_identity._NON_IDENTITY_GRADER` (so it does NOT enter the experiment_id
  digest) — matching `provider_routing` / `attempt_timeout`, not `on_empty`.
  Rationale: `experiment_id = sha256(config_digest : study : stage)` shares one
  `config_digest` across stages, so an identity-bearing *grader* knob would also
  re-key the **generate** experiment_id and orphan already-paid solutions —
  violating "grading never re-triggers generation". A grade-operational policy
  added between runs must be non-identity, like the other grade pass-throughs.

**UX contract.** No new gate, exit code, or hint — pure design declaration with a
summary line. `cli.py`: `oversized solutions: N scored 0 without grading (over
max_solution_chars)` after the empty-solutions line; JSON parity via the
`oversized_skipped` field (automatic on the pydantic dump). No side effect
outside the study dir (only in-dir grading rows, normal operation) → no ledger
row in UX-PATTERNS. Hint candidate: none — the skip is announced unconditionally
in the summary, so there is no silent failure mode to detect.

**Tests.** `tests/test_grade_run.py`: (a) a solution over the threshold is scored
0 and never reaches the judge (a factory that raises if called proves it);
(b) one under the threshold is judged normally; (c) `None` threshold =
unchanged behavior. Mocked judge / direct store seeding, no paid APIs.

**Docs/CHANGELOG.** `[Unreleased]` `### Added` entry with `Closes:
oversized-solution-skip`. Wiki: a `max_solution_chars` row in
Configuration#graders + a note under Error-Handling. New feature — no BACKLOG
section to remove.

---

## Out of scope (explicitly)

- Error-stop / API-error handling — errors are already re-attempted; untouched.
- Empty-solution handling — already exists (`on_empty`); oversized is disjoint.
- Truncation handling — `truncated-completions` hint already covers length caps.
- A cost-estimate adjustment for skipped rows — the estimate is an upper bound;
  the post-run summary reports the actual skips. Keeping the estimator simple
  (it would otherwise need per-grader length lookups) over a small over-estimate.
