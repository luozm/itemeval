# KNOWN-ISSUES.md — deferred bugs

Bugs that are known but **not yet fixed**. This is the bug mirror of
[BACKLOG.md](BACKLOG.md): BACKLOG holds deferred *features*, this file holds
deferred *defects* — code that violates its own contract, dead plumbing, or a
misleading result.

Bugs do **not** get a key, a `feat/` branch, or a plan file (those are for
features). An entry here is just: symptom · where (`file:line`) · why it's
deferred · fix sketch. When the bug is fixed it **leaves this file** in the same
change that adds a [CHANGELOG.md](../CHANGELOG.md) `[Unreleased]` → `Fixed`
entry and lands the `fix:` commit. If a "fix" turns out to need design work, it
graduates to a feature (BACKLOG key + plan) instead.

Feature-entangled defects — a missing capability that also reads as a bug — are
tracked in the owning BACKLOG feature/plan, not here, so they're fixed once.

---

## `items.parquet` keeps stale rows when a study's datasets change
**Found:** 2026-06-17

**Symptom.** The items store is upserted by `[item_id, dataset_id]` and never
reconciled, so items from a dataset later removed from the config linger in the
store.

**Where.** `src/itemeval/store/_items.py`.

**Status.** Latent today — `read_items()` is defined but never called, so the
stale rows are never read back. It becomes a real correctness bug the moment
item metadata is wired into the export (the `item-covariates-export` feature),
so fix it as part of that change or before it.

---

## max_tokens context-fit clamp ignores the reasoning-token budget
**Found:** 2026-06-19

**Symptom.** `generate`'s context-fit clamp shrinks `max_tokens` so a request
fits a model's `context_length` (preventing the guaranteed HTTP 400 on a
small-context model in a mixed roster). But a *reasoning* model also spends a
separate `reasoning_tokens` / `reasoning_effort` budget that counts against the
same window, and the clamp does not subtract it — so a small-context *reasoning*
model with a large reasoning budget could still exceed its window and 400 after
the clamp. Non-reasoning models (the common small-context case, e.g.
`gpt-3.5-turbo`, `qwen-2.5-7b`, `gemma-3n`) are fully covered.

**Where.** `src/itemeval/generate/_params.py` (`fit_max_tokens`) and the clamp
call site in `src/itemeval/generate/_run.py` (`run_generate`).

**Status.** Deferred — no small-context reasoning model in the roster that
surfaced this. Fix sketch: fold the resolved reasoning-token budget into the
clamp's output reserve (subtract it alongside `max_tokens`), or mark such a
(model, config) cell structurally infeasible and skip it.

**Also decide (design — UX-PATTERNS Law 5).** The clamp auto-adjusts a *design
declaration* (`max_tokens`) at runtime. It is kept id-stable (the condition id
keeps the requested value) and announced via a `warnings[]` line, which keeps it
within the spirit of Law 5 — but revisit whether it should be opt-out
(`solvers.fit_max_tokens: auto | off`, default `auto`) rather than always-on, and
whether UX-PATTERNS should gain a row for "announced runtime accommodation of an
infeasible design value."

---

## Cost estimate can read a worst-case ceiling as the expected cost
**Found:** 2026-06-20

**Symptom.** Two ways the pre-flight estimate misleads: (1) cost calibration is
keyed by `model` alone, so changing `reasoning_effort` (or another
`model_config`) borrows the **stale mean** from a different-effort prior run; (2)
the never-truncate **ceiling** (full `max_tokens` → ~80× a typical answer) and
the cold-start `expected == ceiling` case are not labeled — a worst-case ceiling
is shown as if it were a real expected cost.

**Where.** `src/itemeval/budget/_estimator.py` — the `_stats_by(sol_ok,
"model", …)` keying (~`:523`), the ceiling (~`:622`); a `detect_estimate_is_ceiling`
helper exists but is not surfaced.

**Status.** Deferred. Fix sketch: re-key calibration by `(model, model_config)`;
surface `detect_estimate_is_ceiling` so a ceiling/cold-start estimate is labeled,
not presented as expected. The re-key overlaps the `per-model-config` BACKLOG
feature — do it there if that lands first, else here.

---

## Cache-served re-run overwrites a cell's real token usage with zeros
**Found:** 2026-06-20

**Symptom.** A row answered from inspect's local response cache records `usd=0`
with empty usage. Re-running an **already-good** cell (`--force`, or a
`replications` bump that replays epochs 1..R1 from cache) then upserts that
zero-usage row over the original, and content-key last-write-wins **overwrites
the cell's real token counts with zeros** — the store silently loses the usage.

**Where.** `src/itemeval/generate/_run.py` (the cache-hit usage path, ~`:104`,
~`:247`); the overwrite is the content-key dedup in `store/_base.py:44`.

**Status.** Deferred. `recoverable-harvest` recovery is **safe** (it only fills
missing cells, never re-touches good ones); the bug bites `--force`/replication
re-touch. Flip side of the `reuse-savings` feature (which wants to *attribute*
cache hits): fix sketch — on a cache-served row, preserve the prior row's usage
rather than overwriting with zeros. Coordinate with `reuse-savings`.

---

## Superseded `.eval` logs are never pruned across recovery attempts
**Found:** 2026-06-20

**Symptom.** `recovery-run-identity` records every attempt of an experiment in a
per-experiment index but never deletes the `.eval` logs a later attempt
superseded, so `logs/<stage>/` grows unbounded across many recovery passes of a
flaky study. Disk only — no correctness impact (the content-keyed stores are
already converged; old logs are inert once harvested).

**Where.** `src/itemeval/_experiments.py` (the index records attempts but no
supersession dimension); the logs live in `logs/<stage>/`.

**Status.** Deferred — the W3 plan's opt-in `--prune-superseded` was not built
(marginal value for a single-user tool; the prune is the complex part, needing
per-attempt cell-coverage analysis). Fix sketch: an opt-in `--prune-superseded`
that, per attempt, computes whether a *later* attempt re-ran **all** of its cells
(joinable from the stores' `experiment_id`/`attempt` columns + `recoverable-
harvest`'s `classify_logs`), and deletes only the fully-superseded `.eval` —
never a partially-superseded one (a prior attempt is the only log for the good
cells it alone produced). A side effect → announce count + bytes (Law 1) and
require the flag (consent).
