# Implementation plan — UX-PATTERNS compliance backlog

**Status: IMPLEMENTED 2026-06-12** (all steps, in growth-ux.md's combined
order; see CHANGELOG `[Unreleased]`). Kept as the design record.
Originally written 2026-06-11, from a
full code scan against `docs/UX-PATTERNS.md` (binding). This file was the
working brief for the implementation sessions. Read first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — the contract being discharged; its side-effect
   ledger and hint catalog are **normative tables** — every step below flips
   or adds rows there in the same commit.
3. `docs/plans/archive/growth-ux.md` — the sibling plan. Two items found by the scan
   were **removed from this file because growth-ux owns them**: the
   `--json` gate-gap fix (growth-ux 1.3) and the replacing-rows statement at
   the money gate (growth-ux 1.5). Do not re-implement them here.

**Sequencing constraint shared with growth-ux:** Step 2 (`--json` on
generate/grade) and Step 3 (hint framework) below are *prerequisites* for
growth-ux 1.2 and 1.3 — see "Coordination with growth-ux" at the end.

---

## Scan verdict (2026-06-11)

Structurally close to the contract: pydantic result models everywhere, exit
codes 0/2/3/4 in place, pricing provenance exemplary (the model to copy),
gate is single-prompt money-only, `estimate`/`export`/`status` `--json` is
pure. The violations are concentrated in three places: dataset side effects
are fully silent (Law 1), the two spending commands have no `--json` (Law
6/7), and the hint framework does not exist anywhere in the code.

| # | Violation | Law | Where |
|---|---|---|---|
| V1 | Dataset revision resolve, download/reuse, lock-pin write all silent; `load_items` computes `locks_changed` and discards it | 1 | `adapters/_base.py:65-98`, `adapters/_hf.py:54-86` |
| V2 | `generate`/`grade` have no `--json` flag at all | 6, 7 | `cli.py` parser block |
| V3 | Hint framework absent: no `ITEMEVAL_HINTS`, no codes, no stderr hint lines, no `hints` JSON field — catalog marks 3 signals ☑ | hint framework | whole package |
| V5 | Local response-cache reuse silent — visible only as `usd=0.0` per row | 1, ledger | `generate/_run.py:91-107` |
| V6 | Python API has no consent parameter; docstring tells users to self-check | 3, 8 | `__init__.py`, `run_generate`/`run_grade` |
| V7 | Grade empty-solutions summary line embeds advice ("raise max_tokens, or set solvers.on_empty…") — advice must be a hint | 2, 8 | `cli.py:210-219` |
| V8 | Export wording doesn't say *rewritten*; estimate/status JSON carry no dataset provenance block | 1, 6 | `cli.py:232-234`, `budget/_estimator.py` |
| V9a | Batch runs produce no `batch:` announcement line (ledger: "partial") | 1 | `generate/_run.py` |

Moved to growth-ux (do not implement here): V4 (`check_gate` ignores
`--json`) → growth-ux 1.3; V9b (`--force`/rerun replaces rows silently) →
growth-ux 1.5.

---

## Step 1 — Dataset provenance: announce resolve / download-or-reuse / pin (V1, V8-json)

Fix at the source, render at the CLI:

1. **Detect fresh-vs-reused** in `HFAdapter.load` (`_hf.py:65`): before
   `datasets.load_dataset`, check whether repo@revision is already
   materialized in the HF cache (snapshot presence check / builder cache
   inspection). Record `cache: "downloaded" | "reused"`, the cache dir, and
   (best-effort, omit when unavailable) download size.
2. **Extend `LoadedDataset`** (`adapters/_base.py:17`), append-only:
   `cache: str`, `cache_dir: str`, `download_bytes: int | None`,
   `revision_source: "config" | "lock" | "resolved"`, `pinned_now: bool`.
   `load_items` already computes the precedence branch — store it instead of
   discarding; set `pinned_now` from the per-spec lock change.
3. **One provenance line per dataset** via a shared `_print_datasets(prep)`
   in `cli.py`, called from `estimate`/`generate`/`grade`/`status` next to
   `_print_pricing` (same pattern):

   ```
   dataset: cais/aime2025 (split test) @ 4a1b2c3 — downloaded 412 MB to HF cache (first use); revision pinned in dataset_locks.json
   dataset: cais/aime2025 (split test) @ 4a1b2c3 — reused from HF cache (pinned)
   ```

   Unconditional in the text rendering (Law 1: no switch hides announcement
   lines). Pin clause only when `pinned_now` (printed on change only).
4. **JSON parity**: new `DatasetProvenance` model; `datasets:
   list[DatasetProvenance]` on `Estimate` and on the status report (extend
   the existing `DatasetStatus` fields — append-only); included in the
   generate/grade JSON from Step 2.
5. **Ledger**: flip the three dataset rows from **silent** to compliant in
   UX-PATTERNS.md in the same change.

**Checklist.** Side effects: discharges 3 existing ledger rows. Quotable:
the dataset line. JSON: `datasets[]` everywhere the line prints. Doc anchor:
Outputs-and-Schemas (locks) + Configuration (revision). Hint: none — the
provenance line *is* the visibility. Knob: none (visibility is not
optional). Consent: none (announced, not gated). Parity: fields on
`LoadedDataset`/`PreparedStudy`. Stability: new JSON keys append-only.

**Tests.** Fake adapter asserting `revision_source`/`pinned_now` across the
three precedence branches; CLI test (mock provider) asserting one line per
dataset, re-runs say `reused … (pinned)` with no pin clause; cache detection
unit-tested against a tmp dir; no paid APIs.

## Step 2 — `--json` on generate and grade (V2)

Prerequisite for growth-ux 1.2 (hints in `--json` at the gate) and 1.3 (the
`--json` gate-gap fix is unreachable until the flag exists).

1. **Add `--json`** to the generate/grade parsers. Extend `GenerateResult`/
   `GradeResult` (append-only) with what text shows but the models lack:
   `pricing: PricingProvenance`, `estimate_usd`, a `gate` outcome object,
   `datasets` (Step 1), `hints` (Step 3). Same numbers in both renderings
   (Law 6).
2. **stdout purity**: under `--json`, stdout carries only the final JSON
   document — skip `_print_pricing`/projected-cost/`_print_reports`, and
   force inspect `display="none"` unless explicitly overridden, so nothing
   else can leak. Exit codes unchanged.
3. **Gate stop under `--json`** still emits a JSON document (projected cost,
   gate reason, rerun command, hints) before exit 3 — an agent gets
   structure even on a stop. The `check_gate` *behavior* change (never
   prompt under `--json`) belongs to growth-ux 1.3; this step provides the
   `machine`/json signal it needs and the document-on-stop shape.
4. **Stability docs** (Law 7): document exit codes 0/1/2/3/4 and all new
   JSON keys in the wiki + CHANGELOG in the same change.

**Checklist.** Side effects: none. Quotable: existing summary lines
unchanged. JSON: this step *is* JSON parity. Doc anchor: CLI page +
Agent-Guide. Hint: none. Knob: `--json` is the declared machine rendering
(no detection). Consent: gate semantics untouched here. Parity: results
already returned by the Python functions. Stability: append-only keys,
documented.

**Tests.** stdout parses as JSON with nothing else, including the exit-3
gate-stop path; field values equal the text rendering's numbers; display
forced to none.

## Step 3 — Hint framework + the three ☑ hints (V3, V7)

Prerequisite for growth-ux 1.2 (`pilot-available` hint assumes codes,
stderr rendering, `ITEMEVAL_HINTS`, and `hints[]` in JSON all exist).

New module `src/itemeval/_hints.py`:

1. **Model + renderer**: `Hint(code, message, learn_more)`. `emit_hints()`
   prints at most **2** (priority = catalog order), dim when stderr is a
   TTY, to **stderr**, after the summary block, format
   `hint: <fact> — learn more: <wiki-page#anchor>`; suppressed entirely by
   `ITEMEVAL_HINTS=off`; no memory (re-fires whenever the trigger
   re-occurs). In `--json`, `hints` rides in the document — never
   suppressed, never budget-capped.
2. **Detectors** (pure functions over run data) for the three ☑ signals:
   - `cache-zero-reads` — scheduling resolved on, >1 same-prefix call, and
     total `cache_read_tokens == 0` (data already in `cache_columns`).
   - `empty-solutions` — from `GradeResult.empty_total`/`empty_stop_reasons`.
     **Replaces the advice clause** in `cli.py:213-218` (V7): the summary
     line keeps the self-contained fact
     (`empty solutions: 21 excluded [model_length×21] — on_empty=skip`);
     the "raise max_tokens / set solvers.on_empty" advice moves to the doc
     anchor (`Error-Handling#empty`).
   - `unpriced-models` — from `StageEstimate.unpriced_models` /
     `CostReport.unpriced_models`; fires on estimate/generate/grade/export.
     Drop the current inline mention in favor of the hint (one fact, one
     rendering per channel).
3. Aggregate across conditions (one line per run); codes documented in the
   wiki (Law 7); catalog table updated ☑→done in the same commit.
4. Verify the owning anchors exist before shipping:
   `Error-Handling#empty`, `Budget-and-Costs#pricing-table`,
   `Cost-Savings#two-gotchas` — create stubs if missing.

**Checklist.** Side effects: none. Quotable: each hint is one line.
JSON: `hints[]` (Step 2 field). Doc anchor: one owner per hint, per
catalog. Hint: n/a (this builds the mechanism). Knob: `ITEMEVAL_HINTS` env
var only, append-only switch. Consent: hints never block (Law 2). Parity:
hints data returned on result models, rendered by the CLI. Stability: codes
append-only.

**Tests.** Detector units on synthetic results; budget-of-2 and priority
order; `ITEMEVAL_HINTS=off` silences text but never JSON; V7 line reshaped.

## Step 4 — Local response-cache announcement (V5)

`usd_for_usage` already encodes the signal (`usage is None` with a known
price ⇒ served from inspect's local response cache). Surface it:

1. Add `local_cache_rows: int` to `ConditionRunReport` (count error-free
   rows where `sum_usage(sample) is None`) and a run-level total plus
   `local_cache_dir: str` (from inspect's cache-dir API) on
   `GenerateResult`/`GradeResult`.
2. When total > 0, one summary-block line:
   `12 calls answered from local cache ($0) — cache dir: ~/Library/Caches/inspect_ai`.
   JSON parity via the new fields. Flip the ledger row.

Note: growth-ux's epoch-extension path deliberately *relies* on local-cache
replay (old epochs replay byte-identically at $0). This line is what makes
that replay visible — it is load-bearing for growth-ux Workstream 3's
story, so land it before or with 3.x.

**Checklist.** Side effects: discharges the ledger row. Quotable: the line.
JSON: new fields. Doc anchor: Cost-Savings (local cache section). Hint:
none (the line is the visibility). Knob: none. Consent: none. Parity:
result-model fields. Stability: append-only.

**Tests.** Mock run with pre-warmed cache → correct count + line; fresh run
→ line absent; fields present in JSON.

## Step 5 — Python-surface consent: `max_usd=` (V6)

1. Add `max_usd: float | None = None` to `run_generate`/`run_grade`. When
   set, check the stage estimate *before any API call* and raise a new
   `BudgetExceededError(ItemevalError)` if exceeded. **Never prompts**
   (Law 3). The CLI does not pass it (the gate covers the CLI; one
   mechanism per surface).
2. Wire `config.budget.max_usd` (the hard cap — a safety interlock, Law 5)
   into the Python run path too, same exception, so the cap holds on every
   surface.
3. **Coordination with growth-ux 1.3**: once estimate gains
   `remaining_usd`, both checks here must compare against *remaining* (what
   this run can spend), matching the CLI gate's new semantics. If this step
   lands first, compare against the full stage estimate and switch to
   remaining in the 1.3 PR.
4. Update the `__init__.py` docstring (it currently documents the
   violation) and the wiki Python page.

**Checklist.** Side effects: none. Quotable: the exception message carries
the numbers. JSON: n/a (Python raises). Doc anchor: Python-API page +
Budget-and-Costs. Hint: none. Knob: safety interlock — explicit forever.
Consent: the parameter *is* the consent (Law 3 row 2). Parity: this step
closes the parity gap. Stability: new exception documented.

**Tests.** Raises before any model call (mock factory asserts zero calls);
under-threshold proceeds; config `max_usd` enforced in Python path.

## Step 6 — Wording and batch announcement (V8-text, V9a)

1. **Export wording** (`cli.py:232-234`): say *rewrote* — e.g.
   `export: rewrote export/ — gradings_long.parquet + .csv, ledger.csv (disposable view)`.
   Ledger row wording requirement.
2. **Batch announcement** (V9a): inspect manages provider batch jobs
   internally, so a per-job-id line depends on what inspect exposes on the
   eval log. Ship the honest best-effort line now —
   `batch: enabled (anthropic) — provider-side jobs created; resume with the same command`
   — update the ledger row, and leave job-id plumbing as a follow-up row if
   inspect's API doesn't surface ids. Never fake an id.

**Checklist.** Side effects: ledger rows updated. Quotable: both lines.
JSON: `batch` already on ledger rows; add a `batch` field to run results if
absent. Doc anchor: Outputs-and-Schemas (export), Budget-and-Costs (batch).
Hint: none. Knob: none. Consent: none. Parity: fields exist. Stability:
append-only.

---

## Coordination with growth-ux (read before sequencing either plan)

- **growth-ux 1.2 depends on Step 3 here.** The `pilot-available` hint
  needs the framework (codes, stderr, `ITEMEVAL_HINTS`, `hints[]` in JSON).
  Either land Step 3 first, or growth-ux 1.2 builds the framework to Step
  3's spec (then Step 3 reduces to the three detectors).
- **growth-ux 1.3's `--json` gate fix depends on Step 2 here.** `generate`/
  `grade` have no `--json` flag today; "the gate never prompts under
  `--json`" is unreachable until the flag exists. Either land Step 2 first,
  or growth-ux 1.3 absorbs adding the flag (then do it to Step 2's spec:
  stdout purity, display=none, document-on-stop).
- **Estimate JSON shape**: Step 1 adds `datasets[]`; growth-ux 1.3 adds
  `remaining_usd`/`full` fields and 1.1 adds `policy_source`. All
  append-only — no conflict, just don't rename anything.
- **Step 5 follows growth-ux 1.3's semantics** (gate on remaining) — see
  the note in Step 5.
- **Step 4 before or with growth-ux Workstream 3** — wave/epoch-extension
  replay is invisible without the local-cache line.

The canonical combined order lives in **growth-ux.md § Sequencing** (this
plan's Step 2 → Step 3 run first; Steps 1/4/5/6 slot in after growth-ux
1.3+1.5; Step 4 must precede growth-ux Workstream 3). Do not re-derive the
order here — update it there.

After each step: CHANGELOG `[Unreleased]`, wiki anchors, and the normative
UX-PATTERNS tables (ledger + hint catalog) updated in the same commit.
