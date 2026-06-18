# Implementation plan — expected-cost (calibrated cost projection alongside the ceiling)

**Status: IN PROGRESS (started 2026-06-17).** Written 2026-06-17 against
inspect_ai 0.3.x (pinned in `uv.lock`) — this feature is **pure pandas over
existing stores; it touches no inspect_ai code and adds no dependency**, so the
engine pin is not load-bearing here. This file is the working brief for a fresh
implementation session: it carries all the context that session needs. Read
these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style,
   "don't over-engineer").
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing law here is
   **Law 2 (nothing blocks but money) → the gate must never be driven by an
   under-estimate**, so the expected figure is *informational only* and the gate
   keeps comparing the ceiling. Also Law 6 (every fact has a text line + a JSON
   field + a doc anchor) and Law 8 (output is written to be quoted).
3. `docs/wiki/Budget-and-Costs.md` (`## Estimation`, line 7) — the user-facing
   home; the expected projection is documented there.
4. This file end-to-end before coding — the two workstreams share the
   per-model-mean substrate and the result-field naming.

Scope: 2 workstreams. **W1** expected pass in the estimator (per-model means +
coverage fallback + new append-only fields) · **W2** rendering, the cold-start
hint, and `--json` parity across estimate/generate/grade. They are tightly
coupled and may land as **one** `feat:` commit; the same-change paperwork
(CHANGELOG `Closes: expected-cost`, BACKLOG removal, UX-PATTERNS row, wiki) goes
in that commit.

---

## Context: the facts that decide the design

### The ceiling today (the three pessimistic assumptions)

`itemeval estimate` is a deliberate **upper bound** so the money gate can never
under-estimate. Three sites in `budget/_estimator.py` (constants at lines
36–37: `DEFAULT_OUTPUT_TOKENS_GENERATE = 4096`, `DEFAULT_OUTPUT_TOKENS_JUDGE =
512`):

| # | Ceiling assumption | Code site |
|---|---|---|
| 1 | generate output = `max_tokens` (or 4096 uncapped) | `:391` `output_tokens = calls * (max_out or DEFAULT_OUTPUT_TOKENS_GENERATE)` |
| 2 | an **un-generated** solution stubs at `4 × max_tokens` **chars** | `:531-534` `placeholder_len = 4 * (...)`, `solution = stored.get(..., "x" * placeholder_len)` |
| 3 | judge output = `grader_max_tokens` (or 512) | `:537` `output_tokens = calls * (cond.grader_max_tokens or DEFAULT_OUTPUT_TOKENS_JUDGE)` |

Assumption 2 only fires when no real solution is stored; when one is
(`stored` map built at `:456-459` from `solutions_df`), the estimator already
sizes the judge input from the **real** solution text. The expected pass swaps
each worst-case for an observed mean (and only the *stub* path for #2).

### The data the expected pass reads (no new calls)

Both stores already carry what's needed — `estimate_study` already reads them
(`read_solutions`/`read_gradings` at `:235-243`):

- **solutions** (`store/_solutions.py`): `output_tokens` (int64), `model`,
  `solution` (text), `error`. → per-`model` mean `output_tokens` (assumption 1);
  per-`model` mean `len(solution)` chars over non-empty, no-error rows
  (assumption 2 stub).
- **gradings** (`store/_gradings.py`): `output_tokens` (int64), `grader_model`,
  `error`. → per-`grader_model` mean `output_tokens` (assumption 3).

Non-null, no-error rows only (`error.isna()`); for the solution-char mean also
exclude empties (`empty_solution_mask`, already imported transitively).

### Coverage-aware fallback (per model)

A model's expected mean is chosen by sample count, never silently borrowed
without saying so:

```
own observed mean   (>= K samples for that model)
   -> reasoning-group mean   (models sharing ModelPrice.reasoning True/False)
      -> global pooled mean  (all of the stage's observed rows)
         -> ceiling          (only when the stage store is empty — cold start)
```

- The reasoning flag comes from `lookup_price(prep.pricing, model).reasoning`
  (`_pricing.py:34`; bool or None). `None`/unpriced models skip the group tier
  and fall to pooled.
- **K** (min samples to trust a per-model mean) is an internal constant, not a
  config knob (optimization-bucket; no speculative knob — CLAUDE.md). Start
  **K = 5** (a `--policy dev` run yields ~`dev_items × prompts` samples per
  model). Define it next to the other estimator constants with a comment.
- Consequence worth stating: once a stage has **≥1** observation anywhere, every
  model gets at least the pooled mean — so `uncalibrated` (pure ceiling) means
  exactly **cold start** (empty stage store). That is the precise trigger for
  the W2 hint. `dev`'s default runs the whole model grid on the first
  `dev_items` items (`budget/_policies.py` limits items/replications, not
  models), so one pilot calibrates every model; the per-model fallback only
  fires on selective (`--condition`) or grow-in-place runs.

Empirical-Bayes shrinkage of sparse means toward the group is **out of scope**
(BACKLOG open question — explicitly deferred).

### The gate stays on the ceiling (the binding constraint)

`usd` / `remaining_usd` keep meaning the ceiling; `confirm_above_usd`,
`max_usd`, and `check_gate` are **untouched**. The expected figure is a second,
informational projection. This is UX-PATTERNS Law 2 ("the gate must never be
driven by an under-estimate") and is non-negotiable.

### Where the numbers render today (the surfaces W2 touches)

- `cli.py:85-120` `_print_estimate` — the `estimate` stage line + per-condition
  table + the trailing "(projected figures cover the full grid …)" note.
- `cli.py:316-323` `_run_stage` projection line for `generate`/`grade`
  (`projected {stage} cost: …`), built from a `StageEstimate` (`st`).
- `cli.py:219-237` `_gate_stop_doc` — the `--json` document emitted on a gate
  stop (carries `estimate_usd`/`estimate_full_usd`).
- `cli.py:350-353` `_run_stage` sets `result.estimate_usd`/`rows_replaced`
  **after** the runner returns — the natural place to also set a new
  `result.expected_estimate_usd` (no runner-signature change needed).
- Run results: `GenerateResult` (`generate/_run.py:101`, has `estimate_usd`
  `:126`), `GradeResult` (`grade/_run.py:60`, has `estimate_usd` `:90`).

### What does NOT break

- `tests/test_public_api_snapshot.py` guards only `itemeval.__all__` and the CLI
  subcommand set — this feature adds **neither** a public export nor a
  subcommand, so the snapshot stays green (no golden-set bump).
- All new model fields are append-only with defaults; the pydantic models use
  `extra="forbid"`, which only rejects *unknown input* keys — adding defined
  fields is safe and is the established pattern (`StageEstimate` already grew
  this way: cache + delta fields).

---

## W1 — Expected pass in the estimator

**Goal.** Alongside the ceiling, project an **expected** (calibrated) cost per
stage from data already in the stores, so a planner sees a realistic number
without it ever weakening the gate. After a cheap `--policy dev` pilot the
expected figure becomes a good predictor of the full run's bill.

**Config / public surface.** **No new knob** (the K threshold is an internal
constant; no `expected_output_ratio` — BACKLOG forbids inventing a ratio).
New append-only fields:

- `StageEstimate.expected_usd: float = 0.0` (full grid, calibrated) and
  `StageEstimate.expected_remaining_usd: float = 0.0` (the delta, mirroring
  `usd`/`remaining_usd`). When a stage is uncalibrated these **equal** the
  ceiling figures (honest: expected == ceiling at cold start).
- A new `Calibration` pydantic model (in `_estimator.py`), attached as
  `StageEstimate.calibration: Calibration` (default-constructed). Fields:
  `calibrated_models: int` (own mean), `group_models: int` (reasoning-group),
  `pooled_models: int` (global pooled), `uncalibrated_models: int` (ceiling),
  `observed_rows: int` (rows the stage's means were computed from),
  `mean_output_tokens: float | None`, and `mean_solution_chars: float | None`
  (grade-only; `None` for generate). The split makes "a borrowed estimate is
  never shown as measured" a machine-readable fact (BACKLOG requirement).

**Mechanism.** All in `budget/_estimator.py`, no inspect import (stays
engine-free, DEVELOPMENT.md boundary satisfied trivially).

1. Up front (after the stores are read, near `:244`), build three per-model mean
   lookups with the fallback: a small pure helper
   `_calibrated_means(samples_by_model, model_reasoning) -> (mean_fn, Calibration_counts)`.
   `mean_fn(model)` returns `(value, tier)` where tier ∈
   {own, group, pooled, none}. Inputs:
   - generate: `solutions_df` non-error rows → `output_tokens` by `model`, and
     `len(solution)` by `model` (two means share one pass).
   - grade: `gradings_df` non-error rows → `output_tokens` by `grader_model`.
2. In the **generate** loop, compute `exp_output_tokens` from the calibrated
   generate-output mean (per `cond.model`) instead of `max_out`; cost it the
   same way (`_priced_usd` / the cache-split `_discounted_usd`) into
   `expected_usd` + `expected_remaining_usd`. Input tokens are unchanged
   (calibration is output-side for generate).
3. In the **grade** loop, two swaps: (a) the **stub** length for un-generated
   solutions uses the calibrated per-`gen_cond.model` solution-char mean instead
   of `4 × max_tokens` (this changes judge *input* sizing); (b) judge output
   uses the calibrated per-`grader_model` mean. Stored real solutions still win
   over the stub. Cost into the expected accumulators.
4. The expected pass reuses the **exact same** cache-split / batch / warm-group
   machinery (`_cache_split`, `_discounted_usd`, the `force`/wave delta logic) —
   only the token *assumptions* differ, so expected and ceiling stay
   structurally identical (and, e.g., a batch-discounted condition stays
   batch-discounted in both).
5. Keep it lean: accumulate expected token sums + USD inside the existing
   per-condition loops (parallel to the ceiling accumulators), then set the new
   `StageEstimate` fields in `stage_total`.

Rejected generality: no per-condition expected breakdown on `ConditionEstimate`
yet (BACKLOG open question — "lean stage totals first"); no shrinkage; no
configurable K.

**UX contract.** No side effect (pure read of existing stores → no ledger row).
Knob bucket: n/a (no knob). Consent: none (no spend, no row replacement). The
fields are append-only (Law 7). Carries no interaction strength itself — W2 owns
the rendering and the hint.

**Tests.** `tests/test_expected_estimate.py` (new), hermetic, no API:
- Cold start (empty stores): `expected_usd == usd` for both stages;
  `calibration.uncalibrated_models == len(models)`, `observed_rows == 0`.
- With seeded `solutions_df`/`gradings_df` frames carrying small
  `output_tokens` / short `solution` text: `expected_usd < usd` (means below the
  max-token ceiling), and the expected judge input shrinks when the solution-char
  mean is below `4 × max_tokens`.
- Fallback tiers: a model with `< K` samples borrows the reasoning-group mean
  (`group_models` increments); a model absent from the stage's data but with the
  stage non-empty borrows pooled (`pooled_models`).
- Gate invariance: `remaining_usd` (ceiling) is byte-for-byte unchanged by the
  expected pass (guard against accidental coupling). Build the frames the way
  `tests/test_delta_estimate.py` does (`_prepared` from `conftest.write_study_files`).

**Docs/CHANGELOG.** Covered with W2 (one behavior commit).

---

## W2 — Rendering, the cold-start hint, and `--json` parity

**Goal.** Show the expected figure next to the ceiling on every projection
surface, make the ceiling assumption explicit (always-on parenthetical), and —
at cold start — point the user at `--policy dev` to calibrate.

**Config / public surface.**
- `estimate` text (`_print_estimate`): the stage line gains an always-on
  `(ceiling: output at max_tokens; solutions stubbed at max)` clause, and a
  second indented line when calibrated:
  `  expected ~$X (calibrated from N observed gradings: mean judge output 380 tok, mean solution 2,140 tok)`.
  When uncalibrated, no expected line (the hint covers it).
- `generate`/`grade` projection line (`_run_stage`): append the expected figure
  + ceiling clause to the existing `projected {stage} cost:` line, reading
  `st.expected_remaining_usd` and `st.calibration`.
- New append-only run-result field `expected_estimate_usd: float | None` on
  `GenerateResult` and `GradeResult`, set in `_run_stage` next to
  `result.estimate_usd` (`cli.py:351-353`); add it to `_gate_stop_doc`.
- New coded hint **`estimate-is-ceiling`** in `_hints.py`
  (`detect_estimate_is_ceiling`), appended to `CATALOG_ORDER`. Fires when a
  money-spending stage is **uncalibrated** (cold start — no observations to
  calibrate from). Message:
  `this is an upper bound (output assumed at max_tokens) — run --policy dev to calibrate an expected cost`,
  `learn_more: Budget-and-Costs#expected-cost`. Surfaced on `estimate` (via
  `est.hints`) and on `generate`/`grade` as an estimate-time hint (merged into
  the run hints / gate-stop doc, exactly like `split-head-below-min` does via
  `StageEstimate.hints`).

**Mechanism.** `cli.py` render helpers + `_hints.py` detector + wiring the hint
into `estimate_study`'s hint assembly (`:614-625`) for the estimate surface and
into `StageEstimate.hints` for the run surfaces. The detector is a pure function
over `Calibration` (testable without inspect).

**UX contract.**
- Interaction strength: the ceiling clause and expected line are **summary/
  announcement** text (never block, never act — Law 2); the cold-start pointer is
  a **hint** (stable code, Cost/Budget wiki anchor). No new gate.
- Law 6: every text fact has a JSON field — `expected_usd` /
  `expected_remaining_usd` / `calibration` on `StageEstimate` (in `estimate
  --json` via `Estimate`), `expected_estimate_usd` on the run results and the
  gate-stop doc. The text line and the JSON field carry the **same** number.
- Law 8 (quotable): the expected figure is a self-contained line with a number
  and its provenance clause, so an agent can relay "expected ~$X (calibrated
  from N…)".
- Ledger: no new row (no side effect).
- **UX-PATTERNS rows to flip in the same commit:** add the `estimate-is-ceiling`
  row to the hint catalog table (status ✅), owning doc `Budget-and-Costs`.

**Relationship to `pilot-available`.** Both point at `--policy dev`, but on
different triggers: `pilot-available` needs the **gate to engage** with zero
completed rows *for the selected conditions*; `estimate-is-ceiling` fires on the
no-calibration-data condition (incl. plain `estimate`, no gate). On a cold-start
gate stop both are candidates; the 2-hint budget + catalog priority handle it
(order `estimate-is-ceiling` after `pilot-available`, so the never-pay-twice
message wins when both fire). No dedup logic — they say complementary things.

**Tests.** Extend `tests/test_expected_estimate.py` + a `cli`-level assertion
(follow existing `tests/test_estimator.py` / cli test style):
- `detect_estimate_is_ceiling` returns a Hint at cold start, `None` once
  calibrated; appears in `est.hints` and in a `generate`/`grade` gate-stop doc.
- `--json` parity: `expected_usd`/`expected_remaining_usd`/`calibration` present
  on `estimate --json`; `expected_estimate_usd` present on `generate`/`grade`
  `--json` and the gate-stop document.
- Text: the ceiling parenthetical is always present; the expected line appears
  only when calibrated.

**Docs/CHANGELOG.**
- `CHANGELOG.md` `[Unreleased]` → `### Added` entry with a `Closes: expected-cost`
  trailer (same commit as the behavior).
- **Remove** the `expected-cost` section from `docs/BACKLOG.md` (Tier 3); the
  design record stays in this plan once archived.
- `ROADMAP.md`: `expected-cost` is named under **Later (vision-level)** as a
  candidate — move it to the 0.x **Already landed** line that ships it (a shipped
  key may only appear on the `**Already landed**` line; `tests/test_docs_
  consistency.py` fails otherwise). Confirm with the consistency test.
- Wiki `docs/wiki/Budget-and-Costs.md`: extend `## Estimation` with an
  `### Expected (calibrated) cost` subsection (the `#expected-cost` anchor the
  hint points at) — the ceiling-vs-expected distinction, the calibration tiers,
  the `--policy dev` calibration flow, and "gate stays on the ceiling".

---

## Sequencing (canonical)

1. **W1** — estimator expected pass + `Calibration` model + fields + tests.
   (Self-contained; no rendering yet.)
2. **W2** — rendering, hint, `--json` parity, docs + same-change paperwork.
   Consumes W1's `expected_*`/`calibration` fields.

W1 and W2 may be committed together as one `feat:` commit if convenient; if
split, the same-change paperwork (CHANGELOG/BACKLOG/ROADMAP/wiki/UX-PATTERNS)
rides the W2 commit that makes the surface user-visible.

After each step: `make check` (lint + fast tests), CHANGELOG and normative doc
tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **Driving the gate off the expected figure** — never; the gate is the ceiling
  (UX-PATTERNS Law 2).
- **A config knob** for the expected ratio or the K threshold — BACKLOG forbids
  an invented ratio; K stays an internal constant.
- **Empirical-Bayes / shrinkage** of sparse per-model means toward the group —
  BACKLOG open question, deferred.
- **Per-condition expected breakdown** in `--json` — stage totals first
  (BACKLOG open question); revisit on demand.
- **`native-batch-routing`'s dual projection** — when that ships it should
  compare *expected* figures; the hook is "expected and ceiling stay structurally
  identical" (W1.4), but no work here. Tracked under BACKLOG `native-batch-routing`.
