# Implementation plan — grade-solution-fingerprint (self-invalidating grades)

**Status: IN PROGRESS (started 2026-06-23).** Written 2026-06-23 against the
current store/grade code on `main` (no inspect_ai surface is touched — this is a
pure pandas/parquet change). This file is the working brief for a fresh
implementation session: it carries all context that session needs. Read these
first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Relevant laws: Law 1 (no
   silent side effects — a stale grade *is* one), Law 2 (replacing result rows
   joins the single money gate, never a new prompt), Law 6 (three renderings),
   Law 8 (quotable summary). The development checklist (9 questions) is answered
   per workstream below.
3. `DEVELOPMENT.md` → **"Study-facing schema evolution"** — the gradings parquet
   is a machine surface; this plan adds one optional column and discharges the
   gate the additive-by-construction way (read-time backfill + a guard test).
   No inspect_ai boundary rules apply (no engine code changes).
4. This file end-to-end before coding — the two workstreams share the hash
   helper and the match rule.

Scope: 2 workstreams. **W1** solution-fingerprinted skip (the core
self-invalidation) · **W2** honest `status` stale surface.

---

## Context: the facts that decide the design

**The hazard (today).** A grading row is keyed on
`GRADING_KEY = ["grade_condition_id", "gen_condition_id", "item_id", "epoch"]`
([store/_gradings.py:10](../../src/itemeval/store/_gradings.py#L10)) with **no
solution-content component**. `pending_solutions`
([store/_gradings.py:73-104](../../src/itemeval/store/_gradings.py#L73-L104))
marks a solution cell "done" purely on the presence of an error-free grading row
for `(gen_condition_id, item_id, epoch)`:

```python
done_keys = set(zip(done["gen_condition_id"], done["item_id"], done["epoch"].astype(int)))
mask = [(row.condition_id, row.item_id, int(row.epoch)) not in done_keys for row in gradable.itertuples()]
```

So if a solution at a fixed `(condition_id, item_id, epoch)` is ever
**overwritten** (the recency/quality tie-break in `upsert_parquet`,
[store/_base.py:59](../../src/itemeval/store/_base.py#L59)) the grade keyed to it
silently goes stale — the report joins grade↔solution on that same key and shows
a grade computed against text that no longer exists.

**Why now (and why it stays narrow).** The two *triggers* that overwrite a
solution were just fixed on the sibling branch (both in this cycle's
`[Unreleased]`): `cell-granular-resume` (resume no longer re-draws completed
epochs) and the `cache_prompt` dev→full fix. This feature is the **complementary
detection layer** the cell-granular-resume CHANGELOG entry explicitly carves out
("The complementary stale-grade detection … remains the separate
`grade-solution-fingerprint` candidate"). It defends against *any* future
resume/recovery path that re-draws a completed epoch — it does not re-fix the
known triggers. Defense-in-depth, not a duplicate.

**The fix in one line.** Stamp each grading row with the sha256 of the solution
text it graded; make a cell "done" only when a grading row exists **and** its
`solution_hash` matches the current solution's — else it's pending again and
auto-re-grades.

**Pinned facts the design hangs on:**

- **Single write locus.** Every gradings row — verifiable
  ([grade/_run.py:151-179](../../src/itemeval/grade/_run.py#L151)), oversized-skip
  ([:182-212](../../src/itemeval/grade/_run.py#L182)), and judge
  ([:215-277](../../src/itemeval/grade/_run.py#L215)) — is built through one
  shared helper `_base_row(prep, cond, experiment_id, attempt, sol_row, now)`
  ([grade/_run.py:125-148](../../src/itemeval/grade/_run.py#L125)), and `sol_row`
  carries the graded solution text as `sol_row.solution`. Adding
  `solution_hash` in `_base_row` covers all three paths at once.
- **The probe inherits for free.** `_cacheprobe.probe_grade`
  ([_cacheprobe.py:191-262](../../src/itemeval/_cacheprobe.py#L228)) computes its
  remaining-judge-calls projection by calling the *same* `pending_solutions`,
  passing the scoped solutions frame (which has the `solution` text). Once the
  predicate is hash-aware, the cache projection / estimate are honest with **no
  probe code change** — a stale cell reads as fresh work, not a $0 hit. (Mirror
  requirement from the BACKLOG sketch is satisfied by reuse, not duplication.)
- **Rubric staleness is already covered — do not fingerprint the rubric.** The
  grade condition payload hashes the rubric in
  ([design/_grid.py:176](../../src/itemeval/design/_grid.py#L176):
  `"rubric": {"name": rubric_name, "hash": template.hash12}`), and materializing
  rubrics add `materialize: {model, build_hash}`
  ([:184-187](../../src/itemeval/design/_grid.py#L184)). A changed rubric ⇒ a new
  `grade_condition_id` ⇒ no done rows for it ⇒ auto re-grade. So folding
  `rubric_hash` (present on the row at
  [store/_gradings.py:26](../../src/itemeval/store/_gradings.py#L26)) into the
  skip predicate would be redundant. Resolves the BACKLOG open question:
  **solution_hash only.**
- **Schema-evolution gate.** Gradings parquet is a study-facing machine surface
  (DEVELOPMENT.md). `solution_hash` is *additive with a default* → discharge via
  read-time backfill (mirror `_backfill_provenance`,
  [store/_solutions.py:84-91](../../src/itemeval/store/_solutions.py#L84)) **plus a
  guard test** that freezes an older-schema gradings fixture and asserts it still
  loads and compares as *matching* (model: the existing
  [tests/test_store.py:408](../../tests/test_store.py#L408)
  `test_read_backfills_provenance_columns_on_old_store`). A **null** hash on an
  old row means "unknown → treat as matching" so existing stores never force a
  global re-grade. `solution_hash` is **not** a condition-id input, so it never
  re-keys a condition — no `Study migration` note required.
- **The fresh grade wins the upsert.** Re-grading a stale cell writes a new row
  at the same `GRADING_KEY`; `upsert_gradings`
  ([store/_gradings.py:67-70](../../src/itemeval/store/_gradings.py#L67)) keeps
  the best-quality/most-recent row, and a fresh valid grade (concatenated last,
  ≥ recency) lands over the stale one — self-heals with no extra logic.

**Shared mechanism (both workstreams import these — define once, no drift, the
lesson from the centralized `resolve_cache_prompt`):** in
`store/_gradings.py`,

```python
def solution_fingerprint(solution) -> str:
    """sha256 of the graded solution text. Raw bytes, no normalization, to match
    the response cache's byte-exactness. None/NaN normalizes to ""."""
    text = "" if solution is None or (isinstance(solution, float) and pd.isna(solution)) else str(solution)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def grade_matches_solution(stored_hash, current_solution) -> bool:
    """A done grading is current iff its stored hash is unknown (null = old store,
    treat as matching) or equals the current solution's fingerprint."""
    if stored_hash is None or (isinstance(stored_hash, float) and pd.isna(stored_hash)):
        return True
    return stored_hash == solution_fingerprint(current_solution)
```

---

## W1 — Solution-fingerprinted skip (the core self-invalidation)

**Goal.** A solution that changed under a fixed key auto-re-grades on the next
`grade` run instead of silently keeping a grade computed against gone text.
User-visible outcome: grades can no longer be silently stale; the estimate /
money gate already accounts for the re-grade as real work (the probe inherits).

**Config / public surface.** **No new knob.** One additive store column
`solution_hash` (nullable string) on `GRADINGS_SCHEMA`
([store/_gradings.py:12-55](../../src/itemeval/store/_gradings.py#L12)). No new
exit code, no new CLI flag.

**Mechanism (file:line).**
1. `store/_gradings.py`: add `pa.field("solution_hash", pa.string())` to the
   schema; add `solution_fingerprint` + `grade_matches_solution` (above) +
   `_backfill_solution_hash(df)` (adds the column = None when absent); call the
   backfill in `read_gradings` ([:58-64](../../src/itemeval/store/_gradings.py#L58)).
2. `store/_gradings.py` `pending_solutions`
   ([:73-104](../../src/itemeval/store/_gradings.py#L73)): replace the `done_keys`
   set with a `done_map` `{(gen_cond, item, epoch): solution_hash}` over the done
   rows (already one row per key after upsert), then a cell is pending iff its
   key is absent from `done_map` **or** `not grade_matches_solution(stored,
   row.solution)`. The frame already carries `solution` (callers pass the
   solutions frame), so no new argument.
3. `grade/_run.py` `_base_row` ([:125-148](../../src/itemeval/grade/_run.py#L125)):
   add `"solution_hash": solution_fingerprint(getattr(sol_row, "solution", None))`.
   Imported from `store/_gradings`. All three row builders inherit it.
4. `_cacheprobe.probe_grade`: **no change** — verify by test that it now counts a
   stale cell as a miss.

Rejected generality: hashing a *normalized* solution (the BACKLOG lean is raw,
to match the response cache); fingerprinting the rubric (already covered by the
condition id — see Context); a `force_regrade_stale` knob (the behavior is
unconditionally correct, not a toggle).

**UX contract.** Interaction strength: **none new** — no hint, no warning, no
new gate. A re-grade replaces gradings rows, which already rides the *single
money gate* via the existing "Replacing existing result rows" ledger row
(UX-PATTERNS side-effect ledger) and the `rows_replaced` JSON field; the stale
re-grade is just one more replaced row, surfaced at estimate/gate time because
the probe now counts it. No new side-effect ledger row (the hash is computed
locally — no network/global-cache touch). Law-1 discharge: the *removal* of a
silent stale grade is the win; the re-grade itself is announced by the existing
gate. UX-PATTERNS ledger/hint catalog: **no rows flip** for W1.

**Tests.** `tests/test_grade_run.py` and `tests/test_store.py`:
- `pending_solutions` re-marks a cell pending when its solution text changed
  (stored hash mismatches), and keeps it done when unchanged.
- A **null** `solution_hash` (old store) counts as matching → not re-graded
  (the schema-evolution guard, modeled on `test_store.py:408`): write an
  old-schema gradings fixture lacking the column, assert `read_gradings`
  backfills it to null and `pending_solutions` treats it as done.
- `_base_row` stamps the sha256 of `sol_row.solution` on verifiable, oversized,
  and judge rows (one assertion each, or one parametrized).
- `tests/test_cacheprobe.py`: a stale solution counts as a `cache_misses`
  (fresh work), not a hit. All mocked/offline — no paid APIs.

**Docs/CHANGELOG.** `[Unreleased]` → `Added` (or `Fixed`) entry with a
`Closes: grade-solution-fingerprint` trailer, describing self-invalidating
grades + the `solution_hash` column. Wiki: document the new column in
`docs/wiki/Outputs-and-Schemas.md` (the gradings-schema table) and a one-line
"grades self-invalidate when a solution changes" note where resume/never-pay-
twice is described (the doc anchor). Remove the `grade-solution-fingerprint`
BACKLOG section ([docs/BACKLOG.md:457-494](../../docs/BACKLOG.md#L457)) in the
shipping commit; ROADMAP does not name the key (it's only in BACKLOG), so no
ROADMAP move.

## W2 — Honest `status` stale surface

**Goal.** `status` must not report a stale grade as `graded` (done) while the
next `grade` run would re-do it — that contradiction is the silent gap W1's
re-grade would otherwise leave on the read-only surface. Surface "N grades stale
(solution changed)" and exclude stale from the graded count.

**Config / public surface.** Additive field `stale: int = 0` on `ConditionStatus`
([_status.py:30-43](../../src/itemeval/_status.py#L30)) (append-only, default 0,
so the public-API snapshot of `build_status` is unaffected — **[verify]** by
running `tests/test_public_api_snapshot.py`; a returned-model field add should
not move the golden set, but confirm). Optionally the same on `WaveStatus`
([_status.py:73-74](../../src/itemeval/_status.py#L73)) — include only if cheap;
the headline grade conditions are the required surface.

**Mechanism.** In `build_status`
([_status.py:174-205](../../src/itemeval/_status.py#L174)), the grade conditions
already scope `gradings` to effective items/epochs/grid. Join that scoped frame
to the solutions frame (already read at
[_status.py:115](../../src/itemeval/_status.py#L115) region) on
`(gen_condition_id/condition_id, item_id, epoch)` and count, per grade condition,
the done rows where `not grade_matches_solution(stored, current_solution)` —
reusing the W1 helper (no second rule). `stale` = that count; subtract it from
the reported `graded`/`completed` so percentages stay honest. Text line: append
`· N stale` to the grade summary line when `N > 0` (Law 8 quotable); JSON field
`stale` (Law 6 parity).

**UX contract.** Strength: **warning-grade information** in the summary block —
never blocks, never acts (Law 2). JSON parity: `stale` field. Doc anchor: the
`status` section of `docs/wiki/CLI.md` / `Outputs-and-Schemas.md`. No new hint
code (status is the chosen channel; a `grades-stale` hint was considered and
rejected as redundant with the explicit count — keeps the catalog lean). No
ledger row (read-only).

**Tests.** `tests/test_status.py` (or wherever `build_status` is tested): a study
with one changed solution reports `stale=1` for the affected grade condition and
does not count it as `graded`; an unchanged study reports `stale=0`.

**Docs/CHANGELOG.** Fold into W1's `[Unreleased]` entry (one feature) or a second
bullet; note the new `status` `stale` field. UX-PATTERNS: no ledger/hint row
change (status output is already covered by Law 8); add a line to the `status`
JSON field list in the wiki if one is enumerated.

---

## Sequencing (canonical)

1. **W1** — schema column + helpers + backfill + `pending_solutions` + `_base_row`
   + tests. Self-contained and correct on its own (the probe/estimate/gate are
   honest immediately).
2. **W2** — `status` stale surface, building on W1's helper.

Both are small (~60 lines total, per the BACKLOG estimate) and may land in **one
commit** if kept tight, or W1 then W2. After each step: `make check` (lint + fast
tests), CHANGELOG + BACKLOG removal + wiki in the *same* commit as the behavior.
Expect `tests/test_public_api_snapshot.py` to stay green (additive model field);
if it goes red, update the golden set deliberately in the same change.

## Out of scope (explicitly, to prevent creep)

- **Re-fixing the overwrite triggers.** `cell-granular-resume` and the
  `cache_prompt` fix already handle them (sibling `[Unreleased]`); this feature
  only *detects* a changed solution.
- **Fingerprinting the rubric** in the skip predicate — already covered by
  `grade_condition_id` (Context). Stays out.
- **A `solution_hash` column in the long export.** Additive and analyzable, but
  not needed for self-invalidation; track separately if a study asks.
- **Normalized-text hashing** — raw bytes only (matches the response cache).
- **A new hint code / a new knob / a `--regrade-stale` flag** — the behavior is
  unconditionally correct; no toggle.
- **Tidying the `cell-granular-resume` CHANGELOG sentence** that calls this a
  "candidate" — accurate when written; reconcile only if both entries sit in the
  same `[Unreleased]` on `main` at ship time.
