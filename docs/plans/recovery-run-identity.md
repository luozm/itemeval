# Implementation plan — recovery-run-identity (experiment-scoped run identity with attempts)

**Status: NOT STARTED.** Written 2026-06-19, **decisions locked 2026-06-19**
(this rewrite supersedes the first draft — the three design questions are now
settled; see *Locked decisions* below). Written against the current `main`
(post-#18/#19) and inspect_ai as pinned in `uv.lock` — re-verify the pinned
file:line facts if the tree moved. This file is the working brief for a fresh
implementation session and carries all the context that session needs (assume
no conversation history — only this file and the repo). Read these first, in
order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, hint
   framework, the money gate is the only gate, JSON parity, append-only
   machine surface). Every workstream states its bucket and interaction
   strength.
3. `DEVELOPMENT.md` — **mandatory here**: this plan does a *non-additive*
   change to the study-facing schema (it renames the `run_id` column). Read
   "Study-facing schema evolution", including the **Pre-1.0 carve-out** — this
   plan discharges that gate via a result-preserving clean-break migration (see
   *Study migration* below). Also the inspect_ai boundary rules (wrap don't
   fork; pass through don't rename; inspect imports stay in the
   task-builder/orchestrator modules).
4. `docs/plans/recoverable-harvest.md` — **prerequisite feature (S)**. It makes
   a crashed run's progress durable and owns the `.eval` lifecycle classifier
   (`classify_logs`) that W3 consumes. **Build `recoverable-harvest` before this.**
5. This file end-to-end before coding any part — the workstreams share one
   identity model.

Scope: 3 workstreams. **W1** experiment identity (rename `run_id` →
`experiment_id`+`attempt`, semantic config digest) · **W2** recovery-vs-new
detection (Choice A) · **W3** per-experiment index (attempt rollup on top of S).

**Why this is designed first.** It is foundational to the run-UX cluster: the
mid-run tracker (`watch`, a separate run-state reader — item **C**) must read
run state as *experiments and attempts*, not raw invocations, or it re-encodes
the fragmentation this plan removes. R lands before any run-state surface is
built on top of it.

---

## Locked decisions (2026-06-19)

1. **Identity = clean rename, no shim.** Replace the `run_id` column with
   `experiment_id` (stable, derived) + `attempt` (int). **No back-compat
   shim** — old stores are not read; the migration is *delete + re-run*, which
   replays identical results from cache (see *Study migration*). Chosen over
   "add `experiment_id` alongside `run_id`": we are pre-alpha and carry no
   transparent-upgrade burden.
2. **`experiment_id` from a *semantic* config digest.** Derive from the
   **parsed/normalized** config — identity-bearing fields only — so comments,
   whitespace, key order, and pure execution knobs (e.g. `max_concurrency`)
   never change identity. Redefine the existing raw-bytes `config_sha256` into
   this semantic digest and reuse it for both drift detection and identity.
3. **Rollup = persisted per-experiment index.** Write
   `manifests/experiments/<experiment_id>.json` listing attempts + the current
   result set; `status`/`export` resolve "current" from it. Chosen over
   derive-on-read.
4. **Detection = Choice A.** Recovery iff an `experiment_id` already has a prior
   manifest (i.e. config digest unchanged). Grown items / roster drift under an
   unchanged config = **soft warning, not a fork** (mirrors `growth-ux`'s
   additive model and P0's soft `universe_drift`). `--new-run` salts a fresh
   id; a config edit forks automatically. Chosen over the stricter
   grid-identical / gaps-are-holes rules.

---

## Context: what already converges, what forks, and the identity model

itemeval keeps two artifact classes under two identity rules. The asymmetry is
the whole problem.

### Data already converges (content-keyed) — unchanged by this plan

- Solutions/gradings ingest through `upsert_parquet`, which dedups
  **last-write-wins on the content key**
  ([store/_base.py:44](../../src/itemeval/store/_base.py#L44)):
  - solutions key `(condition_id, item_id, epoch)`
    ([store/_solutions.py:9](../../src/itemeval/store/_solutions.py#L9));
  - gradings key `(grade_condition_id, gen_condition_id, item_id, epoch)`
    ([store/_gradings.py:10](../../src/itemeval/store/_gradings.py#L10));
  - materialized rubrics content-addressed on a build-template hash
    (`materialize_id`, [_materialize.py:21](../../src/itemeval/_materialize.py#L21)).
- **`run_id` is *not* part of any content key** — it is a pure provenance
  column. So the rename in W1 does **not** re-key conditions or rows; existing
  keys are stable, and a recovered cell already overwrites the failed row at
  the same key. ✅ This convergence stays exactly as-is.

### Recovery detection primitives already exist — reuse them

- `config_sha256` — currently SHA256 of the **raw config YAML**
  ([_config.py:485](../../src/itemeval/_config.py#L485), `sha256_hex(raw)`),
  recorded in every manifest ([_manifest.py:151](../../src/itemeval/_manifest.py#L151)),
  consumed by drift detection (`_driftcheck.py`) and export
  ([_export.py:204](../../src/itemeval/_export.py#L204)). W1 **redefines** this
  to a semantic digest (below).
- Grid identity — the manifest already stores `grid_generate`/`grid_grade` and
  `conditions_run` ([_manifest.py:52-95](../../src/itemeval/_manifest.py#L52-L95)).
- Gap detection — `epochs_to_run`/`items_to_run`/`resolve_wave`
  ([store/_solutions.py:87-140](../../src/itemeval/store/_solutions.py#L87-L140))
  and `pending_solutions`
  ([store/_gradings.py:61](../../src/itemeval/store/_gradings.py#L61)) compute
  which cells are missing and resume a wave by label.

### Provenance forks today (the problem)

- A fresh `run_id` per invocation, format `{stage}_{UTC}_{uuid8}`
  ([_util.py:26](../../src/itemeval/_util.py#L26)), minted at
  [generate/_run.py:511](../../src/itemeval/generate/_run.py#L511) /
  [grade/_run.py:353](../../src/itemeval/grade/_run.py#L353). It is a
  `nullable=False` column in **five** stores: solutions
  ([_solutions.py:14](../../src/itemeval/store/_solutions.py#L14)), gradings
  ([_gradings.py:15](../../src/itemeval/store/_gradings.py#L15)), ledger
  ([_ledger.py:13](../../src/itemeval/store/_ledger.py#L13)), log_index
  ([_logs.py:14](../../src/itemeval/store/_logs.py#L14)), materialized_rubrics
  ([_materialized.py:31](../../src/itemeval/store/_materialized.py#L31)).
- One manifest file per `run_id` at `manifests/{run_id}.json`
  ([_manifest.py:200](../../src/itemeval/_manifest.py#L200)).
- `.eval` logs accumulate in the **shared** stage dir `logs/<stage>/`
  ([store/_layout.py:50](../../src/itemeval/store/_layout.py#L50)); the dir is
  never cleared.

A **recovery re-run** (same config, finishing error/interrupt holes) converges
the data but forks the provenance anyway — a second `run_id`/manifest/`.eval`
pile for *one experiment*, with nothing marking them as attempts of the same
intent. `solutions.run_id` ends up mixed. Flaky endpoints make recovery the
*common* path, so it bites constantly.

### The identity model (locked)

| Concept | Definition | Scope |
|---|---|---|
| `experiment_id` | `sha256(config_digest : study : stage)[:12]` — deterministic, no wall-clock/uuid | **per stage** (generate and grade get distinct ids, exactly like today's per-stage `run_id`) |
| `attempt` | `1 + count of prior top-level manifests with this experiment_id` | per `experiment_id` |
| invocation handle | reconstructed as `f"{experiment_id}.a{attempt}"` where a unique string is needed (manifest filename) | replaces the old `run_id` string |

`experiment_id` is **stage-scoped** because recovery is stage-scoped (you
recover generation holes or grading holes independently), matching the existing
per-stage `run_id`/manifest granularity. Do not over-read "experiment" as
"whole study" — it is the deterministic successor to the per-stage run id.

### `.eval` retention — the `recoverable-harvest` (S) prerequisite

`.eval` is not "disposable forensics" — until harvested it is the **write-ahead
log** of an in-progress or crashed run (the only record of partial progress; see
`recoverable-harvest`). Post-harvest, our runtime does not re-read it: harvested
rows live in the content-keyed stores joined by `log_file`. **S owns** reading
`.eval` back (`harvest_stage`) and the harvested/unharvested classifier
(`classify_logs`). R's W3 only adds the per-attempt `superseded` dimension and
the opt-in prune — never auto-deletes (good cells are uniquely logged by the
attempt that produced them).

### Why Choice A is safe (verified in source 2026-06-19)

"Same config" cannot silently produce a different sampled **set**:
- The **model-sampling seed is required** ([_config.py:142](../../src/itemeval/_config.py#L142),
  `seed: int`, no default); the draw is deterministic
  (`random.Random(seed).sample` over the *sorted* universe,
  [_modelsample.py:290-301](../../src/itemeval/_modelsample.py#L290-L301)) and
  **pinned** in `model_locks.json`. Same config ⇒ same model set.
- Item-sampling is unshipped (BACKLOG `item-sampling`); today items are
  deterministic (all, or `dev` first-N). Its design pins the item-id list in
  the manifest.

The *content* of results can still vary under an unchanged config — stochastic
generation (the generation seed [_config.py:214](../../src/itemeval/_config.py#L214)
defaults `None`, "only some providers honor it"), provider routing, upstream
model swaps. But **recovery only fills missing cells**:
`epochs_to_run(require_solution=True)`
([store/_solutions.py:87-108](../../src/itemeval/store/_solutions.py#L87-L108))
skips any cell that already has a valid solution. So good data is never
silently overwritten — content variation only determines what lands in the
holes being filled, which is the point.

### Shared primitive with P0 (lock-spec brick)

W1's semantic digest is the same **technique** P0 needs: *compare/hash data
re-parsed through today's pydantic model, never raw bytes/dict*. P0's layer-1
applies it to the `sample` spec (the lock check at
[_modelsample.py:401-411](../../src/itemeval/_modelsample.py#L401-L411) does a
raw `dict != dict`); W1 applies it to the whole identity-config. Build one
helper module (e.g. `_identity.py`) exposing both `normalized_config_digest()`
and a `normalized_spec()` the lock check can reuse; R is sequenced before P0 so
P0 imports it. Different scopes, one technique — do not duplicate.

### Cross-cutting primitive with D (error classification)

W2's "are the gaps recoverable holes" check wants a **terminal-vs-transient**
classifier (dead endpoint = terminal). Today every errored row is retryable —
`error = sample.error.message if sample.error else None`
([generate/_run.py:440](../../src/itemeval/generate/_run.py#L440)),
`stop_reason` stored but unclassified
([generate/_run.py:474](../../src/itemeval/generate/_run.py#L474)). Under Choice
A this classifier is **not load-bearing for the fork decision** (config-digest
alone decides recovery-vs-new), so R does not need to build it. It is owned by
**D**; if R wants to *label* why a cell is missing in the index, consume D's
classifier if present, else omit the label. Do not duplicate D's table.

---

## W1 — Experiment identity (rename + semantic digest)

**Goal.** Replace per-invocation `run_id` with a deterministic, content-derived
`experiment_id` + `attempt`, so recovery attempts of one experiment are
linkable and the mixed-id `solutions.run_id` problem disappears.

**Config / public surface.** **No new knob.** Schema change (non-additive):
- five stores: drop `run_id`, add `experiment_id: str (nullable=False)` +
  `attempt: int (nullable=False)`;
- manifest: drop `run_id`, add `experiment_id` + `attempt` (natural home beside
  the existing `wave`/`epoch_offset` fields,
  [_manifest.py:52-95](../../src/itemeval/_manifest.py#L52-L95)); keep
  `created_at` (recorded provenance, not identity);
- JSON run-summary: `experiment_id`, `attempt` replace `run_id`.

**Mechanism (file:line).**
- New `_identity.py`: `normalized_config_digest(config) -> str` — dump the
  validated config model to canonical JSON over **identity-bearing fields
  only** (exclude execution knobs: `max_concurrency` and any pure
  orchestration/display field — enumerate the include/exclude set in the module
  docstring and a test), `sha256_hex`. Redefine `config_sha256`
  ([_config.py:485](../../src/itemeval/_config.py#L485)) to call this instead of
  hashing `raw`. Confirm `_driftcheck.py` and
  [_export.py:204](../../src/itemeval/_export.py#L204) still read correctly
  (comment-only edits no longer counting as drift is the intended, more-correct
  behavior — note it where drift is surfaced).
- `experiment_id(config, study, stage) = sha256(config_digest : study : stage)[:12]`,
  mirroring the `materialize_id` pattern
  ([_materialize.py:21](../../src/itemeval/_materialize.py#L21)).
- Replace `new_run_id(stage)` ([_util.py:26](../../src/itemeval/_util.py#L26))
  usage at [generate/_run.py:511](../../src/itemeval/generate/_run.py#L511) /
  [grade/_run.py:353](../../src/itemeval/grade/_run.py#L353): compute
  `experiment_id`, then `attempt = 1 + (# top-level manifests with this
  experiment_id)`. Removing the uuid/timestamp draw also drops a
  `datetime.now()`-style call — good for determinism. **Manifest glob must be
  non-recursive** (`manifests/*.json`) so the `experiments/` subdir from W3 is
  never counted.
- Manifest filename becomes `manifests/{experiment_id}.a{attempt}.json`.
- Thread `experiment_id`/`attempt` into the solutions/grading/ledger/log_index
  row builders in place of `run_id` (ledger key
  [_ledger.py:13](../../src/itemeval/store/_ledger.py#L13) becomes
  `(experiment_id, attempt, stage, condition_id, model)` so an attempt's costs
  never overwrite a prior attempt's).

**UX contract.** Strength: **announcement** (the only gate is money). On
`attempt > 1`: one line, e.g. `recovery attempt 3 of experiment a7b3c9d2 —
converging into existing results`. Hint with a stable code + wiki anchor
explaining experiments/attempts. JSON parity: `experiment_id`, `attempt`. Flip
the relevant UX-PATTERNS ledger/hint rows in the same commit.

**Tests.** Pure-function tests (hermetic, tmp study dir, no paid APIs): digest
is comment/whitespace/key-order/`max_concurrency`-invariant but changes on a
real field edit; `experiment_id` stable across runs of the same config; attempt
counter = N+1 over N seeded manifests; the non-recursive glob ignores
`experiments/`.

**Docs/CHANGELOG.** `Changed` (breaking) + the `Study migration` note (below).
`Closes: recovery-run-identity` goes on the **final** commit only (W1–W3 may be
separate commits).

---

## W2 — Recovery-vs-new detection (Choice A)

**Goal.** Decide, before the paid loop, whether this invocation is **recovery**
of an existing experiment or **new**, and announce it.

**Config / public surface.** One **design-declaration** flag `--new-run` (force
a fresh experiment of an identical config — salts the `experiment_id`). No YAML
knob. **`--wave` is *not* a fork signal** (confirmed 2026-06-19) — a wave adds
an epoch block (more observations) within the *same* config, so it keeps the
same `experiment_id` (new `attempt`, existing `resolve_wave` epoch machinery
unchanged). Under Choice A an unchanged config deterministically yields the same
id, so a wave cannot fork the experiment; only `--new-run` or a config edit
does.

**Mechanism (file:line).** Runs in the run entrypoint after `prepare_study`
([_prepare.py:73-160](../../src/itemeval/_prepare.py#L73-L160)), before identity
is finalized. `run_kind = "recovery"` iff a top-level manifest with this
`experiment_id` already exists, else `"new"`. `--new-run` → salt the id (append
a per-invocation nonce to the digest input) → always `"new"`. **No grid or
gap inspection in the fork decision** (Choice A). Grid/roster drift under an
unchanged config is surfaced as a **soft warning** exactly like the existing
`universe_drift` flag ([_modelsample.py:426](../../src/itemeval/_modelsample.py#L426)),
never a fork.

**UX contract.** Announcement stating the decision + why (Law 1). Hint (stable
code + wiki anchor) covering recovery-vs-new and `--new-run`. No gate. JSON
parity `run_kind: "recovery" | "new"`.

**Tests.** Table-driven over synthetic manifests: existing id ⇒ recovery; no id
⇒ new; `--new-run` ⇒ new even with an existing id; grown items / dropped model
under unchanged config ⇒ recovery + a drift warning (not new). Hermetic.

---

## W3 — Per-experiment index (attempt rollup on top of S)

**Goal.** Make provenance legible: one experiment has one *current* result set
and a rollup of its attempts. **W3 does not own `.eval` harvest or the
harvested/unharvested classification — `recoverable-harvest` (S) does.** W3 adds
only the **attempt grouping** and the **superseded** dimension on top of S's
`classify_logs`.

**Depends on S.** S ships `_harvest.classify_logs(prep) -> {harvested,
unharvested}` and keeps the store current. W3 consumes it; do not reimplement
disk-`.eval` reading here.

**Config / public surface.** No new knob. New artifact:
`manifests/experiments/<stage>.<experiment_id>.json`.

**Mechanism (file:line).**
- **Per-attempt manifests stay** (append-only evidence — `growth-ux`'s storage
  model). After `write_manifest`
  ([_manifest.py:199-219](../../src/itemeval/_manifest.py#L199-L219)),
  write/update the **experiment index**: `experiment_id`, `study`, `stage`,
  `config_digest`, and an `attempts` list (`{attempt, manifest_file,
  created_at, run_kind, log_files}`), plus a `current` pointer (latest attempt).
  `status`/`export` resolve "current" from this index instead of guessing from
  the newest manifest.
- **Supersession = the `superseded` dimension S's classifier lacks.** Per-cell
  currency is already the content-keyed store (last-write-wins); W3 records, per
  attempt, whether a *later* attempt re-ran all of its cells. **Do not
  auto-delete `.eval`**: a prior attempt is the *only* log for the good cells it
  alone produced. Opt-in `--prune-superseded` drops only `.eval` whose every cell
  a later attempt re-ran (e.g. a fully-failed attempt) — a side effect, so
  announce count + bytes (Law 1) and require the flag (consent rule). Default is
  grouping + marking only.

**UX contract.** Announcement only. `--prune-superseded` announces what it
removed. JSON parity: `current_attempt`, `superseded` count.

**Tests.** Recovery into an experiment ⇒ index lists 2 attempts, `current`
points at attempt 2, "current" resolution returns the recovered rows; a
fully-superseded attempt's `.eval` is eligible for prune while a
partially-superseded one is not. Hermetic.

---

## Study migration (discharges the schema-evolution gate)

This is a **non-additive** change (the `run_id` column is renamed across five
stores + manifests + condition-id-independent provenance). Per `DEVELOPMENT.md`
"Study-facing schema evolution" and its **Pre-1.0 carve-out**, it ships:

- a CHANGELOG `Changed` entry carrying a **`Study migration`** note;
- the **result-preserving clean-break tip**: *"itemeval now identifies runs by
  `experiment_id`/`attempt` instead of `run_id`. Delete `manifests/` and the
  parquet stores (`solutions`/`gradings`/`ledger`/`log_index`/
  `materialized_rubrics`), then re-run — cached generations replay at ~$0 and
  the content keys are unchanged, so results are identical."* This is safe
  because the rename touches only provenance columns, not content keys, and the
  response cache replays generation deterministically.
- a **loud, non-opaque guard**: when a command meets an old-schema store (has
  `run_id`, lacks `experiment_id`), fail with the migration note above — never
  silently, and never by clearing a pin (`model_locks.json`) to re-draw (that
  would change the panel).

A guard test freezes an old-schema (run_id) store fixture and asserts the tool
emits the briefing rather than crashing opaquely.

---

## Sequencing (canonical)

**Prerequisite: `recoverable-harvest` (S) ships first** — W3 consumes its
`classify_logs`, and R's recovery model assumes harvested rows exist.

W1 (identity + digest, the shared primitive) → W2 (detection consumes the
identity) → W3 (index, on top of S's classifier). One conventional commit per
workstream; only the final commit carries `Closes: recovery-run-identity` and
removes the BACKLOG section.

After each step: `make check` (lint + fast tests), CHANGELOG + UX-PATTERNS rows
updated in the same commit.

**Relationship to P0.** R lands first and owns the normalization helper
(`_identity.py`); P0's layer-1 lock fix imports `normalized_spec()` from it
rather than re-implementing. If P0 must ship first for urgency, build the
helper there and have R extend it — say which in the commit.

**Gates C.** The mid-run tracker reads run state as experiments+attempts from
W1/W3; do not start C until this is IMPLEMENTED.

## Out of scope (explicitly, to prevent creep)

- **The terminal-vs-transient classifier** — owned by **D**; under Choice A it
  is not needed for the fork decision (config digest decides). R consumes it
  only to *label* missing cells, and only if D has shipped.
- **Mid-cell checkpointing** — `midcell-resume` (BACKLOG key), a different layer
  (intra-cell inspect `eval_retry`).
- **Pre-flight cost/cache projection** — **G**.
- **Concurrent same-experiment invocations** — the attempt counter is not
  race-safe against two simultaneous recoveries of one experiment; out of scope
  for a single-user research tool (note it, don't engineer for it).
- **Auto-deleting `.eval` history** — W3 marks; physical prune is opt-in
  (`--prune-superseded`) and only for fully-superseded files.
- **A pause/resume command** — Ctrl-C + re-run already covers it.
