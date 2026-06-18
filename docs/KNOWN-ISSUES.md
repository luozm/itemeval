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
