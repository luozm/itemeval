# Implementation plan — study growth UX (scale-up · snapshots · waves)

**Status: IMPLEMENTED 2026-06-12** (all workstreams, in the combined order
below, together with the ux-compliance plan; see CHANGELOG `[Unreleased]`).
Kept as the design record. Originally written 2026-06-11.
This file is the working brief for implementation sessions: it carries all
context a fresh session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Every workstream below
   ends with its nine-question checklist answers; implementations must honor
   them (hints on stderr with stable codes, warnings never block, the money
   gate is the only gate, JSON parity, append-only machine surface).
3. This file end-to-end before coding any part — the workstreams share
   design decisions.

Related: `docs/FUTURE.md` §2.5 records the scope decision behind this plan
(grow-in-place is the pilot→full happy path; combine-on-export stays narrowed
and is **out of scope here**). `docs/COST-OPTIMIZATION.md` explains the cache
layers referenced below.

**Sibling plan:** `docs/plans/ux-compliance.md` (UX-PATTERNS compliance
backlog, same date). The two are implemented as **one program in one
combined order** — see Sequencing below, which is canonical for both files.
Two items in this plan have hard prerequisites there: 1.2 needs the hint
framework (ux-compliance Step 3) and 1.3's `--json` gate fix needs `--json`
to exist on generate/grade (ux-compliance Step 2).

---

## Context: how a study grows today (read this, it decides everything)

One study directory (`studies/<study>/`) holds three storage classes:

| Class | Files | Lifecycle |
|---|---|---|
| Append-only evidence | `manifests/<run_id>.json`, `logs/**/*.eval`, ledger rows | immutable history of every run |
| Mutable current state | `solutions.parquet`, `gradings.parquet` | keyed **(condition_id, item_id, epoch)** / (+ grade_condition_id); upserts **replace**; time is provenance (`run_id`, `created_at` columns), never identity |
| Disposable views | `export/*` | overwritten by every `export` |

Growth mechanics today: editing the config and re-running pays only the
delta. New items/models/prompts/graders → new keys, purely additive. Raising
`facets.replications` R1→R2 is the exception: `items_to_run`
(`store/_solutions.py`) is *item*-granular, so the item re-runs **all** R2
epochs in one inspect eval (`Epochs(R2)` always runs 1..N — inspect cannot run
a subset). On the machine that ran the originals, epochs 1..R1 replay
byte-identically from inspect's local response cache (keyed on prompt + config
+ **epoch number**, `expiry=None`) at $0 and get harmlessly rewritten;
without that cache they would be **re-drawn and silently replace** the
original rows. `--force` re-runs everything selected, also replaying from
cache where possible.

Case analysis (from the 2026-06-11 design discussion) sorted every growth
use case into three semantic classes by what they do to the store key:

- **Scale-up** — add new keys, never touch existing ones (planned pilot→full,
  "didn't know I could pilot", extend for publication 2, budget-dribbled
  scaling). One design: Workstream 1.
- **Freeze** — materialize an immutable named view of the mutable layer
  (publication artifacts). Workstream 2.
- **Re-observation** — observe the *same* design cell again later, keeping
  both observations (drift / model-downgrade detection over time). Needs time
  in the key; today the package actively prevents it (resume skips; `--force`
  + cache replays old bytes; cache-off replaces rows). Workstream 3.

These are deliberately separate: scale-up wants old cells **stable**;
re-observation wants old cells **re-measured without loss**. No shared verb.

---

## Workstream 1 — Scale-up affordances

### 1.1 `--policy` CLI override

**What.** `generate`/`grade`/`estimate`/`status` accept
`--policy {dev,full-interactive,full-batch}`, overriding `budget.policy` for
this invocation only. Python parity: `prepare_study(cfg, policy=...)`.
Unlocks the semi-automatic pilot flow with zero config edits:
`generate cfg.yaml --policy dev` → inspect results → `generate cfg.yaml`.

**How.** `cli.py`: add the arg to the shared parser for run/report commands;
thread into `_load()` → `prepare_study`. `_prepare.py`: optional `policy`
kwarg wins over `cfg.budget.policy` when building the plan
(`budget/_policies.py` consumes it; no change there). The **manifest must
record the effective policy and its source** (`policy_source:
"config"|"override"`) — `_manifest.py`. The summary block already prints the
policy via status/estimate paths; verify the line shows the effective one.

**Checklist.** Side effects: none new. Quotable line: existing
`(policy: dev)` line now reflects the override. JSON parity: `policy` +
`policy_source` in estimate/status JSON and the manifest. Doc anchor:
Budget-and-Costs#policies + CLI page. Hint candidate: none. Knob bucket:
n/a (an invocation argument, not a config knob). Consent: none (policy
change feeds the existing estimate→gate path). Surface parity: CLI flag +
`prepare_study(policy=)`. Stability: new JSON keys append-only.

**Tests.** Override beats config; manifest records source; `--policy` on
`estimate` changes the projection scope; invalid value → argparse exit 2.

### 1.2 Pilot hint at the gate (`pilot-available`)

**What.** When the money gate fires (projection > `confirm_above_usd`) on a
study whose stores contain **zero completed rows for the selected
conditions**, emit one hint (stderr, after the gate's own output; in
`--json`, in the `hints` array):

```
hint: first run of this study — you can pilot cheaply first (--policy dev runs N items), then re-run at full scope; completed work is never re-paid — learn more: Cost-Savings#never-pay-twice
```

**How.** `cli.py` `_run_gate` call sites know the estimate and can ask the
store for row counts cheaply (`store/_solutions.read_solutions` /
`_gradings.read_gradings`, already loaded by the runners — pass a
`store_is_empty` bool into the gate-printing path rather than re-reading).
Hint code `pilot-available`, registered in the UX-PATTERNS catalog table (the
table is normative — update it in the same change).

**Prerequisite:** the hint framework (`_hints.py`: codes, stderr rendering,
`ITEMEVAL_HINTS=off`, `hints[]` in JSON) — ux-compliance Step 3. It does not
exist in the code today; build it first, then this hint is one detector.

**Checklist.** Side effects: none. Quotable: the hint line itself. JSON:
`hints[]` entry. Doc anchor: Cost-Savings. Hint: this *is* one (≤2/command
budget respected — it fires only at the gate, where no other hint competes).
Knob: none (`ITEMEVAL_HINTS=off` is the only switch, already specified).
Consent: unchanged. Parity: hint rides both renderings. Stability: new hint
code documented.

**Tests.** Fires only when gate engages AND stores empty; absent on re-runs;
present in `--json` hints; suppressed by `ITEMEVAL_HINTS=off`.

### 1.3 Delta-aware estimate (and gate on the remainder)

**What.** Estimate currently projects the **full** policy-effective grid and
the gate compares that against `confirm_above_usd`/`max_usd` — so growing a
half-finished study shows (and gates on) money that won't be spent. Change:
compute `remaining = full − already-complete cells` per stage and make the
**gate operate on `remaining`** (that is what this run can spend; `--force`
sets remaining = full). Render both:

```
projected generate cost: $4.10 remaining of $11.30 full grid (63% complete)
```

**How.** `budget/_estimator.py`: per-condition projections already exist;
subtract completed work by reusing the same predicates the runners use
(`items_to_run` for generate; `pending_solutions` for grade) — estimate
already reads the solutions store for judge sizing, so the data is at hand.
Add `Estimate.generate.remaining_usd`, `…full_usd` (rename nothing —
`usd` keeps meaning *full* for backward compat; append-only), plus
`completed_cells`/`total_cells`. `cli.py`: pass `remaining` into
`_run_gate`; print both numbers; `run_generate`/`run_grade` callers pass
`estimate_usd` for the manifest from the *remaining* figure with the full
figure alongside (`estimate_full_usd` — manifest schema is additive JSON).

**Also fix here (same code, mandated by UX-PATTERNS):** the known gap that
`check_gate` ignores `--json` — under `--json` the gate must never prompt
(proceed under threshold or with `--yes`, else exit 3). `budget/_gate.py`.
**Prerequisite:** `--json` on generate/grade (ux-compliance Step 2) — the
flag does not exist on these commands today; without it this fix has nothing
to key off. On a gate stop under `--json`, still emit the JSON document
(projected cost, gate reason, rerun command, hints) before exit 3.

**Checklist.** Side effects: reading the stores during estimate (study-dir
read — exempt from Law 1). Quotable: the line above. JSON: new append-only
fields. Doc anchor: Budget-and-Costs#estimation (update the "resume state is
not subtracted" paragraph — it becomes false). Hint: none (the line is the
visibility). Knob: none — this is a default-behavior improvement, no option
added (Law 5: optimization absorbed into default). Consent: gate semantics
*change* (gates on remaining) — changelog entry required, arguably fairer in
every case. Parity: fields on `Estimate`. Stability: append-only.

**Tests.** Fresh study: remaining == full. Half-complete: remaining < full;
gate uses remaining (a study with $100 full / $1 remaining passes a $5
threshold); `--force` restores full; `--json` never prompts (gap test).

### 1.4 Drift warnings (config drift · endpoint drift)

**What.** Two *warnings* (never block, summary block, both renderings),
emitted by `generate`/`grade` before the gate:

- **Config drift:** a facet name matches stored rows but its content hash
  differs — `warning: prompt 'standard' changed since last run (hash
  a1b2→c3d4): its 240 existing rows stay under the old condition; this run
  starts a fresh condition`. Detect by comparing the grid's
  (prompt_name → prompt_hash) / (rubric_name → rubric_hash) against distinct
  pairs in the stores. Same for a changed sampling param producing a new
  condition id under an unchanged slug-with-different-hash.
- **Endpoint drift:** the previous manifest's `endpoints_effective` recorded
  a different `served_model` for a model id this run uses — `warning:
  openai/gpt-5-mini previously answered as gpt-5-mini-2026-01-15; provider
  may now serve a newer snapshot — rows are distinguishable by run_id`.
  Read the latest manifest per condition (manifests are immutable; pick by
  `created_at`). This is best-effort: served_model is only known *after* a
  run, so the warning compares past runs to past runs and fires on
  inconsistency among them (and on a >30-day gap since the last run as a
  cheap proxy — decide threshold in implementation, record it in the line).

**How.** New module `_driftcheck.py` (~80 lines): pure functions
`(grid, solutions_df|gradings_df) -> list[Warning]` and
`(grid, manifests_dir) -> list[Warning]`; called from both runners; results
go into the result models (`GenerateResult.warnings: list[str]`, same for
grade — append-only fields) and print in the summary block.

**Checklist.** Side effects: none (study-dir reads). Quotable: each warning
is one self-contained line with counts/hashes. JSON: `warnings[]` on the
result objects. Doc anchor: Pipeline-Concepts#condition-ids (config drift),
Outputs-and-Schemas#manifests (endpoint drift). Hint: no — these are
warnings (observed mismatch, not advice). Knob: none. Consent: none — Law 2,
advice never acts. Parity: result-model fields. Stability: append-only.

**Tests.** Edited template → exactly one warning naming the facet and row
count; untouched study → none; manifest with divergent served_model → one
warning; warnings present in JSON.

### 1.5 Replacement statement at the money gate

**What.** Discharge the UX-PATTERNS side-effect-ledger row "Replacing
existing result rows — planned". Whenever the work a run is about to do
includes **re-running cells that already have rows** (epoch extension
re-running existing epochs, `--force`, `on_empty: rerun` re-attempts), the
gate block states it as part of the *single* confirmation — never a second
prompt:

```
this run replaces 48 existing rows (epoch extension re-runs all epochs of 12 items; replays from local cache are byte-identical and free)
```

**How.** The runners compute to-run sets already; intersect with existing
keys to count replacements before eval (`generate/_run.py`,
`grade/_run.py`), surface through the same pre-gate path as 1.3's numbers.
After Workstream 3 lands, epoch extension stops replacing and this line
fires only for `--force`/`on_empty: rerun`.

**Checklist.** Side effects: ledger row updated from "planned" to done.
Quotable: the line. JSON: `rows_replaced` field in estimate/run JSON. Doc
anchor: Error-Handling#retry-and-resume. Hint: none. Knob: none. Consent:
joins the existing money gate (Law 2 explicitly requires this shape).
Parity: field on results. Stability: append-only.

**Tests.** Epoch extension on populated store → count correct; fresh run →
line absent; `--force` → full count.

---

## Workstream 2 — Snapshots and study cards (freeze)

### 2.1 `export --snapshot NAME`

**What.** `itemeval export cfg.yaml --snapshot pub1` runs a normal export,
then materializes an immutable copy:

```
studies/<study>/export/snapshots/pub1/
  gradings_long.parquet     # frozen copy of the just-written export
  gradings_long.csv
  ledger.csv
  dataset_locks.json        # copied — pins as of snapshot time
  manifests/                # copies of every manifest covering included rows
  snapshot.json             # name, created_at, itemeval_version, config_sha256,
                            # run_ids included, row/condition counts, spend totals
  STUDY_CARD.md             # 2.2
```

Rules: name matches `^[a-z0-9][a-z0-9_-]{0,63}$`; an existing snapshot name
is **refused** (exit 2, "snapshot 'pub1' exists — choose a new name");
snapshots are never read by any compute path (not resume, not merge — purely
an analysis/sharing artifact; consumption = read the parquet like any
export, zip the folder to share). `status` gains a snapshots line
(`snapshots: pub1 (2026-06-11, 1,920 rows), pub2 (…)`). Python parity:
`export_study(cfg, snapshot="pub1")` → `ExportResult.snapshot_path`.

**Why copy, not reference:** the current-state layer is mutable (upserts
replace), so a row-filter reconstruction of "the table as of pub-1" is
impossible after later replacements. History must be materialized at freeze
time.

**How.** `store/_export.py` (+~60 lines): after writing `export/`, copy into
the snapshot dir atomically (write to `snapshots/.tmp-NAME`, rename);
assemble `snapshot.json` from the manifests dir + ledger. `cli.py` flag;
`_status.py` listing.

**Checklist.** Side effects: none outside the study dir. Quotable:
`snapshot: pub1 written — 1,920 rows · 4 runs · $12.40 total · export/snapshots/pub1/`.
JSON: `snapshot` object in export JSON; snapshots list in status JSON. Doc
anchor: new section Outputs-and-Schemas#snapshots. Hint: none. Knob: a verb
argument, not a config knob. Consent: none (no spend, no replacement —
refusing overwrite removes the only destructive path). Parity: CLI flag +
`snapshot=` kwarg. Stability: new JSON keys + exit-2 reason documented.

**Tests.** Snapshot dir contents complete; second snapshot same name refused
(exit 2) while a different name succeeds; snapshot survives a later
generate+export unchanged (immutability); status lists it; Python kwarg
returns the path.

### 2.2 `STUDY_CARD.md` (HF-dataset-card analog)

**What.** A self-describing Markdown document written into every snapshot
(generation is part of 2.1, not a separate command for now). Structure —
**YAML front-matter** (machine-readable, the HF-card pattern; enables a
future push-to-Hub):

```markdown
---
itemeval_study_card: 1          # schema version, append-only
study: my_study
snapshot: pub1
created: 2026-06-11
itemeval_version: 0.2.0
datasets: [{id: MathArena/usamo_2025, revision: 0a2c60f2, items: 6}]
models: [openai/gpt-5-mini, anthropic/claude-haiku-4-5]
replications: 4
graders: [{name: judge_a, model: openai/gpt-5-mini}]
rows: 1920
spend_usd: 12.40
---
```

Body sections, every number derived from existing stores (no new data, no
interpretation, no plots, no new deps):

1. **Design** — facet grid as a table (models × prompts × model-configs ×
   graders × rubrics × replications), crossing, template names + content
   hashes + source (builtin/local).
2. **Execution** — one row per run from manifests + ledger: run_id, stage,
   date, rows written, spend; the policy used; `served_model` per condition
   from `endpoints_effective` (the reproducibility gold — exactly which
   provider snapshot answered).
3. **Results (descriptive)** — completion matrix from the status logic; mean
   score per condition labeled *descriptive, not analysis*; parse-failure /
   empty counts.
4. **Costs** — per-stage and per-provider spend; the savings report numbers.
5. **Reproduce** — the config (inline, fenced) + dataset pins + "run
   `itemeval generate config.yaml` with these pins".

**How.** New `report/_card.py` (~150 lines of f-string Markdown over
`build_status` output + manifests + ledger + `cost_report`). Called by 2.1.
Keep `itemeval card` (card for the *current* mutable state, outside a
snapshot) out of scope until asked — one entry point, fewer states.

**Checklist.** Side effects: none. Quotable: covered by 2.1's snapshot line
(card path included). JSON: `snapshot.card_path`. Doc anchor:
Outputs-and-Schemas#snapshots. Hint: none. Knob: none. Consent: none — but
note the card embeds the config: configs must never contain secrets (already
the rule; keys live in env). Parity: produced by both surfaces via 2.1.
Stability: front-matter schema versioned (`itemeval_study_card: 1`),
append-only fields.

**Tests.** Front-matter parses (yaml.safe_load) with required keys; body
contains the grid table and every run_id; no secrets (assert no `sk-`
pattern); deterministic given fixed stores (golden-file test with mock data).

---

## Workstream 3 — Waves: re-observation over time

**Goal (case):** "run the same study scope again later, keep both
observations, attribute differences" — drift / model-downgrade detection.
Today this is impossible without data loss (see Context). Design decision:
**a wave is an epoch block** — wave *w* with `replications: R` occupies
epochs `w·R+1 … (w+1)·R`. Rationale: the epoch axis is already in every
store key and both cache keys, so new waves are new keys (no migration, no
replacement) and fresh epoch numbers give fresh draws and correct
local-cache behavior *automatically*. `wave` itself is stored as a derived
**provenance column, default 0** — users who never use waves see one
constant column and nothing else (no config key, no prompt, no new verb
behavior unless `--wave` is passed). Per UX-PATTERNS Law 5, `--wave` is a
design declaration: always explicit, never auto-fired.

### 3.1 Substrate: additive epoch runs

**Why it doesn't work today (all three must be solved):**
1. inspect's `Epochs(N)` always runs epochs 1..N — no subset/offset support.
2. `items_to_run` is item-granular — it can't express "item X, epochs 9–12".
3. If a separate eval ran K epochs and we relabeled them +offset at harvest,
   inspect's **local response cache** would key them as epochs 1..K and
   **replay the original wave-0 draws** — silently duplicating old data as
   "new" observations. The offset eval must therefore run with the local
   response cache disabled (correct anyway: re-observations must be fresh
   draws; nothing legitimate to replay). Store-level resume still protects
   a crashed wave (written rows aren't re-run).

**How.**
- `store/_solutions.py`: `epochs_to_run(existing, cond_id, item_ids,
  epoch_range) -> dict[item_id, set[int]]` (keep `items_to_run` delegating
  to it for wave-0 compatibility).
- `generate/_task.py`: `build_generate_task(..., epoch_offset: int = 0)` —
  when offset > 0, force `cache_policy = False` and pass the offset through
  task metadata.
- `generate/_run.py`: `rows_from_generate_log` maps
  `epoch = sample.epoch + epoch_offset`; ledger/manifest record the offset.
- Grade side: `pending_solutions` already keys on the solution's epoch —
  judge rows inherit the remapped epoch with no change; verify with tests.

### 3.2 `generate --wave <label>` (and `grade --wave <label>`)

**What.** `generate cfg.yaml --wave 2026-07` = re-observe the current
policy-effective scope as a new epoch block: allocate the next free block
(max existing epoch → next multiple of R), run only its missing epochs
(resumable mid-wave), stamp rows with `wave` (int) and `wave_label` (the
user's string) columns. Without `--wave`, behavior is exactly today's
(wave 0). Estimate/gate cover the wave like any run (1.3's remaining logic
applies within the wave's epoch range). `grade --wave` grades that block's
solutions under the existing grade conditions. Config drift warnings (1.4)
are **load-bearing** here: a changed template between waves means the new
wave is a different condition — warn exactly as in scale-up.

Schema: `wave`/`wave_label` columns on solutions/gradings/export (additive,
default 0/null — backfill on read for old stores, no rewrite). `status`
prints per-wave completion only when >1 wave exists (zero noise for
everyone else). Analysis story: `df.groupby("wave")` plus `served_model`
from manifests; a drift view in the future `itemeval report` (FUTURE §1.5)
consumes the same column — not built here.

**Checklist (whole workstream).** Side effects: none new (the offset eval's
cache *bypass* is announced: `wave 2026-07: local response cache off — re-observations must be fresh draws`,
satisfying "reuse announced as loudly as fetching" in reverse). Quotable:
`wave 2026-07: epochs 9–12 · 240 rows · $3.80` summary line. JSON: `wave`,
`wave_label`, `epoch_offset` fields on results/manifest. Doc anchor: new
Pipeline-Concepts#waves + a Cost-Savings note (waves never replay — by
design they cost full price). Hint candidate: `wave-config-drift` is covered
by 1.4's warning. Knob bucket: design declaration (explicit verb argument).
Consent: spend flows through the normal gate. Parity: `--wave` flag +
`run_generate(prep, wave="2026-07")`. Stability: new columns/fields/exit
reasons documented in the same change; schema change noted in CHANGELOG
under a minor bump.

**Tests.** Wave allocation (next block after max epoch); wave rows never
replace wave-0 rows (key disjointness); offset eval runs cache-off (assert
fresh draws via mockllm call counter, not replays); mid-wave crash resumes
within the block; old stores read with wave defaulting to 0; export carries
the columns; status silent at one wave, per-wave at two.

---

## Sequencing — combined order (canonical for this file AND ux-compliance.md)

"UXC n" = ux-compliance.md Step n. One PR per phase unless noted.

1. **UXC 2** — `--json` on generate/grade (stdout purity, display=none,
   JSON document on gate stop). Unblocks phases 3–4.
2. **UXC 3** — hint framework + the three ☑ catalog hints. Unblocks 1.2.
3. **1.1 + 1.2** — policy override + pilot hint; no schema changes;
   immediately improves the pilot story.
4. **1.3 + 1.5** — delta-aware estimate, gate on remaining, the `--json`
   gate-gap fix, replacement statement; biggest trust win for growth.
5. **UXC 1, 4, 5, 6** — dataset provenance lines; local-cache announcement;
   Python `max_usd=` (compare against *remaining*, post-1.3); export
   wording + batch line. Independent of each other, any order, separate
   small PRs. UXC 4 must land before phase 8 (wave/epoch-extension replay
   is invisible without it).
6. **1.4** — drift warnings; independent; small.
7. **2.1 + 2.2** — snapshots + study card; independent of everything above.
8. **3.1 + 3.2** — last and largest; depends on 1.4 (drift warnings) and
   UXC 4 (local-cache visibility); subsumes the old FUTURE "finer-grained
   resume" idea for the generate stage.

After each lands: CHANGELOG `[Unreleased]`, wiki page updates per the doc
anchors named above, UX-PATTERNS hint-catalog/side-effect-ledger rows
updated in the same commit (both tables are normative).

## Out of scope (decided, do not drift into)

- **Combine-on-export** — stays FUTURE §2.5, narrowed to organizationally
  separate stores. Nothing here reads across study dirs.
- **`itemeval report` / drift analytics** — FUTURE §1.5 consumes the wave
  column later; this plan only records the data.
- **`--items` filter / random sampling** — FUTURE §1.3; pairs with pilots
  but is its own feature.
- **`itemeval card` outside snapshots; HF Hub push** — revisit after 2.2
  ships and gets real use.
