# Implementation plan — truncation-signal (truncation as a first-class signal)

**Status: IN PROGRESS (started 2026-06-20).** Written 2026-06-20 against
inspect_ai 0.3.x (pinned in `uv.lock`) — re-verify the `[verify]` facts below if
that moved. Working brief for a fresh session; read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Relevant here: Law 6 (every
   fact in three renderings — a status line, a JSON field, a doc anchor), the
   hint framework (W2), and Law 5 (no new knob — this is pure visibility).
3. `DEVELOPMENT.md` — "Study-facing schema evolution": the export column is
   additive to a **rebuilt disposable view** (not a pinned store), so it needs no
   migration; `stop_reason` is *already* in the solutions store, so there is **no
   store-schema change** at all.
4. This file end-to-end before coding.

Cluster context: item **A** of the run-UX cluster (`local/run-ux-reorder-plan.md`),
the last Tier-1 fix. Covers report §4.B (silent truncation) + part §4.E.
The `unknown` stop-reason handling is a **separate** item (A2 / KNOWN-ISSUES,
upstream-rooted) — explicitly out of scope here.

Scope: **W1** truncation channel in `status` + export column (the spec) ·
**W2** a `truncated-completions` coded hint (the "make it not silent" surface).

---

## Context: the facts that decide the design

### The bug

A solver that stops on `max_tokens` returns a **truncated-but-non-empty** string.
itemeval records it with `error=None` and a non-blank `solution`, so `status`
counts it `completed` and `grade` scores it as a finished answer — **a budget cut
is silently scored as a content failure**, corrupting the measurement. Nothing in
any rendering distinguishes it from a genuine complete answer.

### What is already stored (no store change needed)

`stop_reason` is persisted per solution row:
`store/_solutions.py:40` — `pa.field("stop_reason", pa.string())`, written from
`sample.output.stop_reason` at `generate/_run.py:505`. So the signal is in the
store today; this feature only **reads it back** into `status` and the export.

### The truncation set `[verify on inspect bump]`

inspect's `StopReason` literal (`.venv/.../inspect_ai/model/_model_output.py:72`):
`"stop" | "max_tokens" | "model_length" | "tool_calls" | "content_filter" |
"unknown"`. `as_stop_reason` (:400) maps provider `"length"` → `"max_tokens"`.

**Truncation = `{"max_tokens", "model_length"}`** — both are length caps (the
requested budget, or the model's own context limit). **`content_filter` is NOT
truncation** (a refusal, not a budget cut) and **`unknown` is NOT** (its conflation
is the separate A2 defect). Define the set once as a module constant so the bump
checklist re-verifies it.

### Disjoint from the existing `incomplete`/empty channel

`status` already has an `incomplete` channel (`_status.py:39`) counting
**empty** no-error completions via `empty_solution_mask` (`store/_solutions.py:82`)
— e.g. a reasoning model whose whole budget went to hidden reasoning (an empty
`max_tokens`/`model_length` row). **`truncated` is the complement: a length-cap
stop with a NON-empty solution.** The two masks must be disjoint by construction
(`truncated_mask` excludes `empty_solution_mask`), so a row is *either* empty
*or* truncated, never double-counted, and `truncated ⊆ completed` (it does not
change `completed` — it is an informational sub-count, never a reclassification:
no behavior change, no new gate).

### Where the renderings live

- `ConditionStatus` model + `build_status` (`_status.py:29`, `:129`) — add the
  channel here (generate conditions only; grade output is the judge's, not the
  solver's).
- CLI status table (`cli.py:741-753`) — the GENERATE table currently shows
  `done / err / empty`; add a `trunc` column.
- Export: `export_study` merges solutions→gradings (`store/_export.py:317`),
  selecting `sol_cols` (`:291`); `EXPORT_SCHEMA` (`:33`). `stop_reason` is **not**
  currently carried into the export — add a derived `truncated` boolean column.

---

## W1 — truncation channel in `status` + export column

**Goal.** Make a truncated solution visible everywhere a completed one is, so an
analyst can filter it out of a content-validity analysis and an operator sees it
on `status`.

**Config / public surface.** **No new knob** (pure visibility; UX Law 5). Append-
only additions:
- `ConditionStatus.truncated: int = 0` (status `--json` parity; additive default).
- `EXPORT_SCHEMA` gains `truncated: bool` (additive column to the **rebuilt
  disposable** `gradings_long` view — no store migration; a config that produces
  no truncation still gets the column, value all-`False`).

**Mechanism.**
- New `store/_solutions.py` constant `TRUNCATION_STOP_REASONS = frozenset({"max_tokens",
  "model_length"})` + `truncated_mask(df) -> pd.Series` mirroring
  `empty_solution_mask`: `error.isna() & stop_reason.isin(TRUNCATION_STOP_REASONS)
  & ~empty_solution_mask(df)`. Pure pandas, hermetic.
- `_status.build_status`: per generate condition, `truncated = int(truncated_mask(
  in_scope).sum())`; set on `ConditionStatus`. `completed` is unchanged.
- `store/_export.export_study`: compute `sol["truncated"] = truncated_mask(solutions)`
  before the merge, add `"truncated"` to `sol_cols`, and add the field to
  `EXPORT_SCHEMA` (boolean). The left-merge gives `NaN` for a grading with no
  matching solution row — coerce to `False` (`.fillna(False).astype(bool)`)
  `[verify]` the existing merge already left-joins, so handle the null exactly
  like other solution-side columns do.
- CLI `_cmd_status`: add a `trunc` column to the GENERATE table header + rows
  (`str(c.truncated)`), beside `empty`.

Rejected (kept simple): exporting the raw `stop_reason` string. The boolean is
the spec and the measurement question ("was this cut for length?"); the
finer-grained `content_filter`/`unknown` distinctions belong to A2.

**UX contract.** Interaction strength: **none** — it is a count in the summary
(Law 8) and a column. No side effect (read-only), so **no ledger row**. JSON
parity: `truncated` on each generate `ConditionStatus`; `truncated` column in the
export. Doc anchor: `Error-Handling.md#truncation` (new) owns the explanation; the
Outputs-and-Schemas export-column table gains the row. Consent: none (no spend,
no replacement). Surface parity: `build_status`/`export_study` already serve both
CLI and Python; no prompt.

**Tests.** `tests/test_status.py` — a solutions fixture with one `max_tokens`
non-empty row, one `model_length` non-empty row, one `max_tokens` **empty** row
(must count as `incomplete`, NOT `truncated`), one clean `stop` row → assert
`truncated == 2`, `incomplete == 1`, `completed` unchanged. `tests/test_export.py`
(or `test_store.py`) — assert the `truncated` column exists, is `True` exactly for
the length-cap non-empty rows, and the schema is otherwise unchanged. A
`truncated_mask` unit test beside `empty_solution_mask`'s.

**Docs/CHANGELOG.** `[Unreleased]` `Added` with `Closes: truncation-signal`;
**remove** the `Truncation as a first-class signal` section from `docs/BACKLOG.md`;
ROADMAP move (see Sequencing). Wiki: new `Error-Handling.md#truncation` section,
the export-column row in `Outputs-and-Schemas.md`, and the `status` column in
`CLI.md`. Update the export-schema snapshot/golden if one exists (`[verify]`
`tests/` for an `EXPORT_SCHEMA` column-set assertion).

---

## W2 — `truncated-completions` coded hint

**Goal.** Surface the signal at the moment of action, not only on a later
`status`/`export`. A coded hint is exactly the framework's job: an observed fact
from this run + a pointer, never blocking (Law 2).

**Config / public surface.** A new stable hint code `truncated-completions`
(append-only, Law 7) in `_hints.py`; rides `hints[]` in `--json` like the others.

**Mechanism.** A pure detector `detect_truncated_completions(truncated_count,
total_completed, model_or_max_tokens_hint)` in `_hints.py` (one per code, the
module's pattern). Fires on `generate` (and `grade` relays generate-stage hints
already) when `truncated_count > 0`. Line, e.g.:
`hint: 21 completions stopped at max_tokens (truncated, non-empty) and are scored
as finished answers — raise solvers.max_tokens or filter truncated rows —
learn more: Error-Handling#truncation`. Wire it where `generate`'s run result
assembles hints (mirror `empty-solutions`, `generate/_run.py` /
`_hints.emit_hints`), reading the per-run truncated count (sum of the new
per-condition channel, computed over this run's rows).

**UX contract.** Hint strength (never acts, never blocks); stderr, dim, after the
summary; budget of 2 enforced by the framework. JSON: always present in `hints[]`.
Doc anchor: same `Error-Handling.md#truncation`. UX-PATTERNS hint **catalog row**
flipped to ✅ in the same commit.

**Tests.** `tests/test_hints.py` — detector returns a hint when `truncated_count >
0`, `None` at zero; message contains the count and `max_tokens`.

**Docs/CHANGELOG.** Folded into the same `Closes: truncation-signal` entry (one
clause); the UX-PATTERNS catalog row.

**Decision flag for checkpoint.** W2 is *recommended* (it is the piece that makes
the truncation "not silent" at run time, the feature's whole motivation) but is
**beyond the BACKLOG's literal "status channel + export column" scope**. If the
maintainer prefers the minimal spec, ship W1 only and drop the hint (the status
column + export column still fix the measurement-validity bug; the hint is the
proactive nicety). The catalog already lists analogous `empty-*` hints, so W2 is
on-pattern.

---

## Sequencing (canonical)

1. **W1** — the channel + column + `truncated_mask` (the spec; everything else is
   visibility on top).
2. **W2** — the hint (consumes W1's count).

One `feat:` commit (W1+W2 + same-change paperwork). After it: `make check`. The
public-API snapshot is **untouched** (no `__all__` / CLI-subcommand change — only
an additive model field + export column + hint code), so
`test_public_api_snapshot.py` stays green; if an export-schema golden exists,
update it deliberately in this commit. Then archive this plan (`IMPLEMENTED
<date>`, `git mv` to `docs/plans/archive/`, fix inbound links + the
`local/run-ux-reorder-plan.md` A row).

After each step: `make check`; CHANGELOG + normative doc tables in the same commit.

## Out of scope (explicitly, to prevent creep)

- **`unknown` / `content_filter` stop-reason handling** — A2 (tracked in
  KNOWN-ISSUES; upstream-rooted: inspect's `as_stop_reason` collapses unmapped
  reasons to `"unknown"`, and `content_filter` is a refusal, not a length cut).
- **Any behavior change** — `truncated` never alters `completed`, the money gate,
  grading eligibility, or `on_empty`. It is a signal only. Re-running a truncated
  cell with a larger `max_tokens` is the user's call (grow-in-place already
  supports it); no auto-rerun, no new `on_truncated` knob.
- **Raw `stop_reason` export column** — the boolean answers the measurement
  question; a raw column is A2's territory if it lands.
- **Grade-side truncation** (a judge truncating) — out of scope; the signal is
  about solver output validity.
