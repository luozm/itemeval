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

## A paid run under `--json` goes dark — no progress, no ETA
**Found:** 2026-06-19

**Symptom.** `generate`/`grade` under `--json` silence *both* the pre-flight ETA
line and inspect's live progress display (`--json` forces `display="none"`, and
the ETA print sits in the `if not args.json` block). A long paid run then emits
nothing until it returns — a liveness gap that violates the UX-PATTERNS
expectation that a long-running spend shows it is alive. Worked around in docs
(the Agent-Guide now tells agents to run the paid stages without `--json`), but
the tool still can't give a `--json` caller any liveness signal.

**Where.** `src/itemeval/cli.py` (`_run_stage` — the ETA `print` under
`if not args.json`, and `display = … "none" if args.json else None`).

**Status.** Deferred — needs a small design decision on the display/stdout
contract. Fix sketch: route the ETA line and a coarse progress heartbeat to
**stderr** (always, independent of `--json`), so `--json` stdout stays pure JSON
*and* both a human and a captured-stderr caller still see liveness. Then the
Agent-Guide carve-out can relax back to "`--json` everywhere is safe." Confirm no
agent harness parses stderr as part of the structured contract before moving the
ETA there.
