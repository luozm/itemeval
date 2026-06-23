# Implementation plan — cell-granular-resume (scope resume re-runs to the missing cells, not whole items)

**Status: IMPLEMENTED 2026-06-23.** Shipped as a `Fixed` CHANGELOG entry
(`Closes: cell-granular-resume`). This file is the design record; the framing
below is the working brief it was built from. Written against inspect_ai 0.3.239
(pinned in `uv.lock`). The reading order it set out:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The relevant laws here:
   Law 1 (no silent side effects — the fill is an announced in-dir write), Law 2
   (advice never acts; money is the only gate — no new gate), Law 8 (output is
   written to be quoted — the fill needs a quotable summary line).
3. `DEVELOPMENT.md` — inspect_ai boundary (wrap don't fork; pass through don't
   rename; inspect imports confined to the task-builder/orchestrator modules).
   This feature stays entirely inside the existing `generate/_task.py` +
   `generate/_run.py` waist — no new inspect surface.
4. The original bug write-up: `local/itemeval_regenerate_overwrite_bug.md` (the
   incident this fixes). This plan implements its **fix (A)**.
5. This file end-to-end before coding.

Scope: **1 workstream.** **W1** — split the resume planning per item and add a
cell-granular hole-fill phase.

---

## Context: the facts that decide the design

### The bug (fix A from the write-up)

Resume re-runs are scoped at **item** granularity. The Phase-1 planning loop
([generate/_run.py:886-911](../../src/itemeval/generate/_run.py#L886-L911))
computes the per-epoch missing set and then collapses it to whole items:

```python
missing = _solutions.epochs_to_run(store, cond.id, item_ids, epoch_block,
                                   require_solution=...)          # dict[item_id, set[epoch]]
to_run = [iid for iid in item_ids if missing[iid]]               # ← collapse to items
...
items = [it for it in prep.items_effective if it.id in to_run_set]
task = build_generate_task(items, cond, ..., epochs via Epochs(replications))
```

So an item with **one** errored/missing epoch re-executes **every completed
sibling epoch** of that item (Phase 2 runs `epochs=Epochs(replications)` over the
whole item). When the response cache replays those siblings it is "only" the
zero-usage-overwrite known issue; but on a **cache miss** (the incident:
`attempt_timeout` bumped → inspect's per-epoch cache key changed for every cell)
the siblings are real paid re-draws, and a fresh valid draw **overwrites** the
prior valid solution via the recency tie-break in `upsert_parquet`
([store/_base.py:114](../../src/itemeval/store/_base.py#L114)). The stale grade is
then silently kept (separate feature `grade-solution-fingerprint`).

### Why a holed item is the *only* corruptible case

An item with **zero** stored rows ("whole-missing") has nothing to overwrite —
re-running all its epochs is always safe and is the correct fresh draw. The
corruption is **exclusively** a *partial-hole* phenomenon: an item with some
completed epochs **and** some missing ones. So the fix only needs to change the
holed-item path; whole-missing items stay exactly as today.

This split also preserves a shipped **cost** optimization: whole-missing items
keep the `epochs=Epochs(N)` path with warm-then-fan-out cache scheduling and
`cache_prompt` provider-cache sharing across epochs
([generate/_task.py:54-84](../../src/itemeval/generate/_task.py#L54-L84)). This is
the path the grow-in-place / `dev`→`full` primary lifecycle rides (dev's first-N
items leave the rest **whole**-missing, never holed), so it must not regress.

### Inspect's per-epoch cache key forces a hole-fill to run cache-OFF

`inspect_ai/model/_cache.py` (0.3.239): `CachePolicy.per_epoch` defaults `True`
(line 68) and `_cache_key` appends the **runtime epoch ContextVar** to the key
components when `per_epoch` (lines 91-98, 164-165). itemeval already relies on
this: a wave run (`epoch_offset > 0`) **forces cache off**
([generate/_task.py:85-89](../../src/itemeval/generate/_task.py#L85-L89)) because
an offset eval also runs internal epochs `1..N` and would otherwise replay the
wave-0 draws under the same per-epoch key.

Consequence for this feature: a cell-granular task built reroute-style (one
`Sample` per cell, `epochs=Epochs(1)`) runs **every** cell at internal epoch 1,
so with cache **on** (a) a cell for store-epoch 5 would not match the original
epoch-5 cache entry, and worse (b) two missing epochs of the same item would
collide on one cache key and the second would replay the first. Therefore the
hole-fill task **must run cache-off** — exactly what the shipped reroute task and
the `epoch_offset>0` path already do. A holed item's missing epochs have no good
completion to replay anyway (they errored/blanked), and a valid-but-unharvested
cell is already projected into the store by `recoverable-harvest` before resume,
so cache-off is not a cost regression for these cells.

### The mechanism already exists: the reroute phase

`output-validity-reroute` already does cell-granular, write-back-by-metadata,
cache-off re-issue of specific `(item, epoch)` cells. The pieces to reuse:

- **`build_reroute_task(cells, cond, template, study, origins, ...)`**
  ([generate/_task.py:128-189](../../src/itemeval/generate/_task.py#L128-L189)):
  one `Sample` per `(item, epoch)` with `id=f"{item.id}#e{epoch}"`, carrying
  `reroute_epoch=epoch` in metadata; `epochs=Epochs(1)`; `solver=generate(cache=False)`.
- **Harvest write-back**
  ([generate/_run.py:544-546](../../src/itemeval/generate/_run.py#L544-L546)):
  `rows_from_generate_log` writes the row at `reroute_epoch` when present, else
  `sample.epoch + epoch_offset`. So a reroute-style cell writes its **absolute**
  target epoch in place — no `epoch_offset` needed.
- **The reroute *phase* shape**
  ([generate/_run.py:665-797](../../src/itemeval/generate/_run.py#L665-L797)):
  build per-condition tasks → `run_condition_evals(tasks, ...)` → per-cond
  `persist_generate_condition(...)`. This is the exact template for the new
  hole-fill phase, and it sidesteps the one-task-per-condition assumption in the
  main eval (`run_condition_evals` maps logs→conditions by the task's
  `itemeval.condition_id` metadata,
  [generate/_run.py:266](../../src/itemeval/generate/_run.py#L266) — two tasks
  with the same id in one eval would collide, so hole-fill is its own eval).

Difference from reroute: reroute adds the failed `served_provider` to
`provider:{ignore:[…]}` via `merge_provider_ignore`
([generate/_run.py:749-751](../../src/itemeval/generate/_run.py#L749-L751)); the
hole-fill is a **primary** draw, so it uses the base `provider_routing` (no
ignore). Everything else is identical — so `build_reroute_task` is reused
as-is (its cache-off, one-shot, write-back contract is exactly what hole-fill
needs).

### The estimator and status are already cell-granular (no change needed)

The pre-flight estimate counts **cells**, not items:
[budget/_estimator.py:406,424](../../src/itemeval/budget/_estimator.py#L406)
derives `remaining_calls` / `completed_cells` from `epochs_to_run` over effective
items × the epoch block. `status` likewise counts error-free **rows/cells**
(`expected = items × reps`, `completed = error-free rows`,
[_status.py:121,189](../../src/itemeval/_status.py#L121)). So the estimate
already promises *cell*-level remaining work — today's executor silently
**over-runs** it (re-executing completed siblings of holed items). This fix makes
the executor honor the estimate; the estimator/status need **no behavioral
change**, only a consistency test. (This corrects the BACKLOG implementation
note, which said these modules must change to "count cells" — they already do.)

### Seed

`solvers.seed` defaults `None` ([_config.py:213](../../src/itemeval/_config.py#L213));
fresh draws are naturally independent. A user-set fixed seed has the **same**
per-epoch semantics the shipped reroute path already has (reroute re-draws
arbitrary epochs at internal epoch 1 too) — an accepted precedent, not a new
concern. Out of scope to revisit here.

---

## W1 — split resume planning per item; add a cell-granular hole-fill phase

**Goal.** A resume re-runs only the truly-missing `(item, epoch)` cells.
Completed sibling epochs of a holed item are never re-executed, so they can be
neither re-paid nor overwritten — closing the data-corruption path independent of
the inspect cache-key issue, and aligning the executor with the already
cell-accurate pre-flight estimate.

**Config / public surface.** **No new knob.** This is a correctness fix to
existing resume behavior; adding a toggle would violate "don't over-engineer" and
UX-PATTERNS Law 5 (this is not an optimization users tune). One append-only
result/JSON field for the announced fill (below).

**Mechanism** (all in `generate/_run.py`, reusing `generate/_task.py`):

1. **Phase 1 split** ([generate/_run.py:886-911](../../src/itemeval/generate/_run.py#L886-L911)).
   In the non-`force` branch, after computing `missing = epochs_to_run(...)`,
   classify each item with missing epochs:
   - **whole-missing** — `missing[iid]` covers the entire `epoch_block` (count ==
     `reps`): keep on the main path (`to_run`, built into the `epochs=Epochs(reps)`
     task — unchanged, cache/warming preserved).
   - **holed** — `0 < len(missing[iid]) < reps`: collect its missing cells into a
     per-condition `holes: dict[cond_id, list[(Item, epoch)]]`; **exclude the item
     from `to_run`** so Phase 2 does not re-run it whole.

   `force` is untouched (it re-draws everything whole by design). A condition with
   only holed items gets an empty main task → today's "skipped" branch
   ([generate/_run.py:898-909](../../src/itemeval/generate/_run.py#L898-L909));
   the hole-fill phase below produces its rows and its report must reflect that
   (don't leave it reported as a bare skip).

2. **New Phase 3.5 "fill holes"** — a function modeled on `_reroute_soft_failures`
   ([generate/_run.py:665-797](../../src/itemeval/generate/_run.py#L665-L797)),
   running **after** Phase 3 harvest and **before** Phase 4 reroute. For each
   condition in `holes`: clamp `max_tokens` to context like the main loop
   ([generate/_run.py:918-930](../../src/itemeval/generate/_run.py#L918-L930)),
   build `build_reroute_task(cells, cond, template, study, origins,
   max_tokens_override=…, attempt_timeout=…, max_retries=…)`, set
   `task.model = factory(exec_model, "generate", model_args_for(..., provider_routing=base_routing,
   cache_scheduling=False, ...))` (base routing — **no** provider ignore), then
   one `run_condition_evals(tasks, ...)` + per-cond `persist_generate_condition(...)`.
   Honor `prep.plan.batch` like the main path. Runs for both wave and non-wave
   resumes (the absolute `reroute_epoch` write-back is offset-agnostic, so a holed
   **wave** is covered too — unlike Phase 4 reroute, the fill is the primary draw
   and is **not** gated on `epoch_offset == 0`).

3. Fold the filled rows into the run totals (`rows_written`, `total_usd`,
   `truncated_total`) and the per-condition `ConditionRunReport` (a condition
   filled-only must report `status="run"` with its filled `rows_written`, not
   `skipped`).

**Why a holed item drops warming.** A holed item's few missing epochs are filled
cache-off with no cross-epoch warming. This is marginal (holes are the minority;
warming matters across a full fresh item, which stays on the main path) and is the
price of never touching the completed siblings. Stated as accepted, not hidden.

**UX contract.**
- **Side effects / Law 1 + 8.** The fill writes result rows in-dir (normal
  operation) but it spends and replaces nothing it shouldn't — announce it with
  one quotable summary line, mirroring the reroute line, e.g.
  `filled: N missing cell(s) across K item(s) — completed siblings untouched`.
  Emit only when `N > 0`. This is **not** a new side-effect ledger row (it writes
  result rows in the study dir like every other generate write; the existing
  "Replacing existing result rows" row already covers in-dir result writes — and
  the fill specifically does **not** replace completed siblings).
- **Gate / Law 2.** No new gate. The fill is part of the same single money gate
  (its cells are already in `remaining_calls`). It reduces spend vs. today; it
  never adds a prompt.
- **JSON parity / Law 6.** Append-only fields on the generate result, e.g.
  `cells_filled` (int) and `items_holed` (int), alongside the existing reroute
  fields. Same numbers in the text line and JSON.
- **Hint / Law 5.** None needed — the fill removes a silent failure mode rather
  than adding one; the announcement line is the visibility.

**Tests** (`tests/`, no paid APIs — mock the model like the existing generate
tests; grep `mockllm` / the reroute tests for the pattern):
- **Holed item fills only the hole.** Seed a store with an item that has valid
  epochs {1,2,4,5} and a missing/errored epoch 3 (reps=5); run resume with a mock
  model whose output differs from the stored siblings; assert epoch 3 is written
  and epochs {1,2,4,5} are **byte-identical** to before (not re-drawn, not
  overwritten) and their `attempt`/usage rows are unchanged. This is the
  regression test for the incident.
- **Whole-missing item keeps the epochs=N path.** A condition with only
  whole-missing items produces one main task (assert it is not routed through the
  hole-fill phase) and writes all `reps` epochs.
- **No-op resume.** A fully-complete store runs zero tasks (no main, no fill).
- **Wave hole.** A holed cell inside a wave block fills at the correct absolute
  epoch.
- **Estimator/executor consistency.** `remaining_calls` for a holed-item scope
  equals the number of cells the executor actually runs (was: executor ran more).
- Mock-level assertion that the hole-fill task is built `cache=False` and
  `epochs=Epochs(1)` (reuse of `build_reroute_task` guarantees this — assert the
  call rather than the provider behavior).

**Docs / CHANGELOG.** Same commit as the behavior:
- `CHANGELOG.md` `[Unreleased]` → `Fixed` (it's a correctness fix shipped as a
  feature) with a `Closes: cell-granular-resume` trailer; one line describing the
  no-longer-overwritten-siblings + no-longer-re-paid behavior.
- **Remove** the `cell-granular-resume` section from `docs/BACKLOG.md` (design
  record lives on in this plan once archived).
- Wiki: the resume/recovery behavior is user-facing — touch the relevant
  Error-Handling / Cost-Savings anchor (the "completed work is never re-paid"
  promise) to note holed-item resume now fills only the hole. Confirm with the
  maintainer which anchor.
- UX-PATTERNS: no ledger/hint row flip required (no new side effect class, no new
  hint); add the `cells_filled` field to the relevant schema doc if one enumerates
  generate-result fields.

---

## Sequencing (canonical)

One conventional commit (the fix + its same-change paperwork). After it:
`make check` (lint + fast tests). If the generate-result model gains
`cells_filled`/`items_holed`, expect `tests/test_public_api_snapshot.py` to go
red — update the golden set deliberately in the same change.

## Out of scope (explicitly, to prevent creep)

- **The inspect cache-key root** (`attempt_timeout` sits in inspect's response
  cache key though itemeval treats it non-identity). Upstream-inspect filing,
  like `token-progress-timeout` / `batch-resume`. This fix removes the hazard
  in-package without it.
- **Stale-grade detection** when a solution legitimately changes — that is
  `grade-solution-fingerprint` (the complementary half). Not built here.
- **A provenance lock in `upsert_parquet`** (refuse to overwrite a graded cell).
  Defensive belt-and-suspenders; redundant once holed siblings are never re-run
  + `grade-solution-fingerprint` detects any other overwrite. Tracked in the
  BACKLOG open questions; not built here.
- **Cross-epoch warming for holed cells.** Accepted loss (marginal; holes are the
  minority). Whole-missing items keep warming.
- **The zero-usage-overwrite known issue** (cache-served re-run zeroes a cell's
  usage). Distinct bug in KNOWN-ISSUES; this fix incidentally removes its
  resume-path trigger for holed items (siblings aren't re-harvested) but does not
  address `--force`/replication re-touch — leave the KNOWN-ISSUES entry.
