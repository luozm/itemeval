# Implementation plan — output-validity-reroute (provider-aware reroute for soft failures)

**Status: IMPLEMENTED 2026-06-20.** Shipped on `feat/output-validity-reroute`
(CHANGELOG `Closes: output-validity-reroute`); this file is now the design record.
Written 2026-06-20 against inspect_ai 0.3.x (pinned in `uv.lock`) — re-verify the
`[verify]` facts below if that moved. It carried all context the implementing
session needed. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Relevant here: Law 1 (every
   side effect / spend announced — a reroute re-issues paid calls), Law 2 (the
   money gate is the only gate — reroute adds no new gate, but its extra spend
   must be announced and bounded by the cap), the knob buckets (`max_reroutes` is
   an **operational** knob, not a design declaration), and the hint framework.
3. `DEVELOPMENT.md` — inspect_ai boundary (wrap don't fork; inspect imports stay
   in the orchestrator — here `generate/_run.py`; the reroute reuses the existing
   task-build → `eval` → harvest path, no new inspect API) and "Study-facing
   schema evolution" (no new store column — detection reads the
   `provider-finish-capture` columns; `GenerateResult` gains append-only fields).
4. This file end-to-end before coding.

Scope: 1 workstream. **W1** an opt-in, capped, provider-excluding reroute of
soft-failed `generate` cells, with an honest residue record.

---

## Context: the facts that decide the design

### The failure (why this exists)

OpenRouter routes each call to one of several backends. A flaky one returns a
**soft failure**: HTTP 200 + `finish_reason=error` (or an unmapped reason) +
empty/truncated content. It slips through every layer:

- OpenRouter `allow_fallbacks` only retries *hard* failures (5xx/unavailable).
- inspect's `as_stop_reason` flattens the non-enum reason to
  `stop_reason="unknown"` and treats the 200 as a final result — **no retry**.
- itemeval stores it with `error=None` and (often) blank/short text, so it counts
  as completed and is graded as a low/empty score — a **false floor at full cost**.

Evidence (a real dev run): `glm-5.1`→GMICloud, `kimi-k2.6`→DigitalOcean,
`qwen3.5`→Phala each soft-failed and were clean via a different backend. The
manual stopgap (`provider_routing: {ignore: [GMICloud, Phala, DigitalOcean]}`) is
whack-a-mole — a new item/model surfaced a backend the list didn't have.

### The substrate is already in place (provider-finish-capture, shipped)

`solutions.parquet` now carries **`served_provider`** (the backend that answered)
and **`native_finish_reason`** (the raw reason before the flatten). So detection
is a pure store predicate — no live-response access needed. This is why a
**post-eval, store-driven** design (not an in-solver one) is correct: inspect's
`ModelOutput` does *not* expose `served_provider`/`native_finish_reason` to a
solver (verified — they live on `ModelEvent.call.response`, read in
`generate/_run.served_provider_finish`), so an in-solver reroute couldn't build
the exclude list. The store can.

### The generate orchestration (where W1 hooks in)

`run_generate` (`generate/_run.py:642`) runs in phases:
- **Phase 1** plans each condition → builds a `Task` (`build_generate_task`,
  `generate/_task.py:37`) with the *missing* items, sets `task.model = factory(exec_model, "generate", model_args_for(provider_routing=..., ...))` (`:792`).
- **Phase 2** `run_condition_evals` (`:197`) runs all tasks in one
  `inspect_ai.eval`, mapping logs back by `itemeval.condition_id` metadata.
- **Phase 3** `persist_generate_condition` (`:585`) → `rows_from_generate_log`
  (`:473`) → upserts solutions/log-index/ledger.

W1 adds **Phase 4** after Phase 3. It must be **self-contained off the store**
(not off Phase-1 `planned`): on a resume where every cell is "done", Phase 1
skips all conditions (`to_run` empty), yet prior-run soft failures should still be
rerouted. So Phase 4 reads the store, finds in-scope soft-failed cells, and
rebuilds tasks independently.

### Epoch granularity (the one tricky bit)

The store key is `(condition_id, item_id, epoch)`. A reroute must overwrite the
*same* key, so the re-issued draw must be written to the **original epoch**.
inspect epochs are positional (`epochs=N` → samples numbered 1..N), and re-running
a whole item would re-touch its *good* epochs under changed routing (different
cache key → re-pay + different backend → corrupts good cells). So the reroute task
runs **one sample per bad cell** with `epochs=1`, carrying the target epoch in
sample metadata; a reroute-aware extractor writes that epoch instead of the
positional one. `rows_from_generate_log` already reads `item_id` from
`sample.metadata` (set by the task builder) with `sample.id` fallback; add an
optional `reroute_epoch` metadata read for the epoch (back-compatible — absent on
normal samples → positional path unchanged).

### Cost & cache facts

- Reroute calls must be **fresh** — the local response cache key does not vary on
  the `provider` routing object, so a cached bad response could replay. Build
  reroute tasks with `cache=False` (the same lever the wave/offset path uses,
  `generate/_task.py:83`).
- Reroute is **extra spend** beyond the pre-flight estimate. The pre-flight money
  gate (`enforce_budget_cap`) runs once before the eval; runtime variance (longer
  outputs, and now reroutes) is already outside it. Bound and **announce** it:
  spend is ≤ `max_reroutes` × (soft-failure cells), a minority; the summary line
  and `GenerateResult` carry the counts and the extra `$`.

### Identity

`max_reroutes` is an **operational retry policy**, not a scientific design choice
— like `provider_routing` and `attempt_timeout`. Add it to
`_identity._NON_IDENTITY_SOLVERS` so it is popped from the `experiment_id` digest:
a recovery re-run with reroute toggled on **converges** into the same experiment
and cleans up its soft failures, instead of forking a new one.

---

## W1 — opt-in, capped, provider-excluding reroute

**Goal.** When `solvers.max_reroutes` is set, `generate` automatically re-issues a
soft-failed cell on a different backend (excluding the one that failed), up to the
cap, replacing the bad row when a good draw lands and leaving an honest record
when it doesn't — turning the manual `ignore:` blocklist into an automatic,
model-agnostic recovery.

**Config / public surface.** New knob on `SolverSpec` (`_config.py`):
`max_reroutes: int | None = Field(default=None, ge=1)` — UX bucket: **operational**
(retry policy; does not change what is measured). `None` = off (no behavior
change). Append-only `GenerateResult` fields: `rerouted: int` (cells re-issued),
`reroute_recovered: int` (now valid), `reroute_unresolved: int` (still soft after
the cap). **No new store column** (the residue is already marked by
`native_finish_reason`/`stop_reason`). **No new gate.**

**Mechanism.**
- `store/_solutions.py`: `soft_invalid_mask(df) -> pd.Series` beside
  `truncated_mask` — `df["error"].isna() & ((native_finish_reason == "error") |
  (stop_reason == "unknown"))`. Pure pandas; `[verify]` the reason set against
  inspect's `StopReason` on a bump. Document that `unknown` is broad (it also
  catches benign unmapped reasons) — acceptable because reroute is opt-in and
  capped.
- `_endpoints.py`: `merge_provider_ignore(provider_routing, ignore_providers) ->
  dict | None` — returns the verbatim routing object with `ignore_providers`
  unioned into its `ignore` list (other keys untouched); unchanged when there is
  nothing to ignore. Pure, unit-tested.
- `generate/_run.py`:
  - `rows_from_generate_log`: read `epoch` from `sample.metadata["reroute_epoch"]`
    when present, else the positional path (one-line, back-compatible).
  - A `_reroute_soft_failures(...)` Phase-4 helper: loop up to `max_reroutes`
    rounds. Each round: `read_solutions`, restrict to selected conditions × in-scope
    items × `epoch_block`, take `soft_invalid_mask`; stop if empty. Per condition
    with bad cells, accumulate `ignored_by_cond[cid] |= {served_provider of each
    bad cell}`, build a reroute `Task` (samples = bad cells, each
    `metadata={item_id, reroute_epoch, condition_id, dataset_*}`, `epochs=1`,
    `cache=False`, same template + `fit_max_tokens` clamp as Phase 1), set
    `task.model = factory(exec_model, "generate",
    model_args_for(provider_routing=merge_provider_ignore(base, ignored_by_cond[cid]), ...))`.
    Run all reroute tasks through `run_condition_evals`; persist with the
    reroute-aware extractor (per-cell epoch). Tally recovered (a previously-bad
    cell that is no longer soft-invalid) and accumulate spend.
  - Skip Phase 4 entirely when `max_reroutes is None`, under native batch
    (`prep.plan.batch is not None`), or for wave/offset runs (`epoch_offset > 0`).
  - After the loop, `reroute_unresolved` = bad cells still soft-invalid; fold the
    reroute spend into `total_usd`; emit the summary line + hint.

**UX contract.** Interaction strength: **announcement + hint**.
- Summary line (Law 1, spend announced): e.g. `reroute: 7 cell(s) re-issued
  excluding [GMICloud, Phala], 5 recovered, 2 still invalid (+$0.43)`.
- Hint `reroute-residue` (stable code, append-only; `_hints.py`): fires when
  `reroute_unresolved > 0` — `2 cell(s) still soft-failed after N reroutes
  (native_finish_reason=error) — they carry an honest score; exclude them or pin
  provider_routing — learn more: Error-Handling#serving-provider-and-native-finish-reason`.
- JSON parity: the three `GenerateResult` fields. No new gate, exit code, or
  ledger row (reroute writes only inside the study dir; the ledger already records
  the extra generate spend per its existing rows). Flip the hint **catalog row**
  in `docs/UX-PATTERNS.md` in the same commit (the one UX-PATTERNS change).

**Tests.** All hermetic (`tests/test_generate_run.py` + `tests/test_store.py` +
`tests/test_endpoints.py`):
- `soft_invalid_mask` unit (error/unknown true; clean/empty/api-error false;
  disjoint from the api-error channel) — beside `test_truncated_mask`.
- `merge_provider_ignore` unit (None base → `{ignore:[...]}`; existing `ignore`
  unioned + sorted; other keys preserved; empty ignore → unchanged).
- **Recovery integration**: seed a soft-failed row (`error=None`,
  `native_finish_reason="error"`, `served_provider="BadCo"`, blank solution) for an
  in-scope cell; `run_generate` with `max_reroutes=2` and the normal mock factory
  (main eval skips the "done" cell; Phase 4 re-issues it → clean) → assert
  `reroute_recovered == 1`, the row now has a real solution and null
  `native_finish_reason`, and `reroute_unresolved == 0`.
- **Unresolved-after-cap integration**: a factory returning a model that emits
  `ModelOutput(..., stop_reason="unknown")` (the test pattern at
  `test_generate_run.py:111`); `run_generate` with `max_reroutes=2` → every cell
  stays soft-invalid → assert `reroute_unresolved > 0`, `rerouted` reflects the
  capped attempts, and it ran ≤ `max_reroutes` rounds (no infinite loop).
- **Off by default**: `max_reroutes=None` with a seeded soft-failed cell →
  untouched, `rerouted == 0` (proves opt-in).
- A `reroute-residue` hint detector test (`tests/test_hints.py`).

**Docs/CHANGELOG.** `[Unreleased]` `### Added` with `Closes:
output-validity-reroute`; **remove** the `output-validity-reroute` section from
`docs/BACKLOG.md`. ROADMAP: add the key to the `0.3` "Already landed / in flight"
line. Wiki: a `max_reroutes` row in `Configuration.md` (graders/solvers field
notes) and a reroute subsection in `Error-Handling.md` (extending the
soft-failure section that `provider-finish-capture` added). UX-PATTERNS: the
`reroute-residue` hint catalog row.

---

## Sequencing (canonical)

One `feat:` commit (W1 + same-change paperwork). Order within it: `max_reroutes`
config + identity → `soft_invalid_mask` + `merge_provider_ignore` (+ their unit
tests) → `rows_from_generate_log` reroute-epoch read → the Phase-4 loop →
`GenerateResult` fields + summary + hint → integration tests → docs. After:
`make check`. The public-API snapshot is **untouched** (no `__all__`/CLI change;
only an additive config field + result fields + hint code), so
`test_public_api_snapshot.py` stays green. Then archive this plan (`IMPLEMENTED
<date>`, `git mv` to `docs/plans/archive/`, fix inbound links).

After each step: `make check` (lint + fast tests), CHANGELOG and normative doc
tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **Grade-side reroute** (judge soft failures) — same mechanism on `grade/_run`;
  a follow-up once the generate side proves out.
- **Grade-time exclusion of the residue** — the separate "extend the validity
  gate to error/empty at grade time" item; W1 only re-issues and records honestly,
  it does not change grading eligibility.
- **Single-provider models** — cannot be rerouted (no alternate backend); needs
  preflight detection (`preflight-endpoints`) + study-level substitution, not a
  reroute. The summary/hint name them via the unresolved count; no synthesis.
- **Native batch + wave/offset runs** — reroute skipped (batch can't re-issue
  mid-flight; waves are fresh observations). Documented, not worked around.
- **Truncation / empty completions** — legit budget cut and the empty channel keep
  their own mechanisms (`truncation-signal`, `solvers.on_empty`); reroute targets
  only the provider soft failure, never reclassifies those.
- **A runtime hard cost cap on reroute spend** — out of scope; the spend is
  bounded by the cap × soft-failure rate and announced (the pre-flight gate stays
  the only gate, per UX-PATTERNS).
