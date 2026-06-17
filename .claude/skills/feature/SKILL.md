---
description: Start or continue a feature on the itemeval key-threaded lifecycle (BACKLOG → plan → commit docs to main → branch → build → same-change rule)
argument-hint: <backlog-key>
disable-model-invocation: true
---
Drive the **Add a feature** lifecycle in @CONTRIBUTING.md for the key: **$ARGUMENTS**

The key is the feature's identity, threaded the whole way: BACKLOG section → ROADMAP plan → branch `feat/$ARGUMENTS` → `docs/plans/$ARGUMENTS.md` → CHANGELOG `Closes: $ARGUMENTS`.

1. **Key.** Confirm a `**Key:** `$ARGUMENTS`` section exists in @docs/BACKLOG.md. If it doesn't, stop — the idea isn't ready to implement: run `/brainstorm` first to pressure-test it and give it a key, then come back here.
2. **Plan.** If `docs/plans/$ARGUMENTS.md` doesn't exist, create it from @docs/plans/TEMPLATE.md and fill in the Context + workstream sections by investigating the *real* code (file:line in this repo, and in the installed dep under `.venv` when wrapping inspect_ai) — a plan built on assumed APIs dies on contact. Set the status line to `IN PROGRESS (started <today's date>)`. If it already exists, read it and continue from its status.
3. **Commit the planning docs to `main` first** — *before* branching, as separate `docs:` commits: the BACKLOG entry (it's `/brainstorm`'s deliverable; if your step-2 investigation forced a correction to it, commit that fix here too) and the plan. **Do not carry them onto the feature branch.** Planning artifacts are inputs to the feature, not its output; active plans live on `main`; and keeping them off the branch makes the branch diff exactly the implementation + its same-change paperwork (so the step-6 BACKLOG **removal** is a clean deletion, not the back-half of an add/remove churn). This is the easy default to get wrong — branching with the docs uncommitted silently combines them onto the branch.
4. **Branch.** `git checkout -b feat/$ARGUMENTS` (skip if already on it).
5. **Build** the simplest thing that satisfies the spec (CLAUDE.md "don't over-engineer" — no speculative knobs). Code touching inspect_ai obeys the boundary rules in @DEVELOPMENT.md: wrap don't fork; pass through don't rename; flatten at the public API; inspect imports confined to the task-builder / orchestrator / extension modules. New config/data schemas are pydantic models.
6. **UX contract.** Walk the 9-question development checklist in @docs/UX-PATTERNS.md — it is binding, not advisory. Flip any ledger / hint-catalog rows in the same change.
7. **Same-change rule** (same commit as the behavior): add a `[Unreleased]` entry to @CHANGELOG.md with a `Closes: $ARGUMENTS` trailer; **remove** the shipped section from @docs/BACKLOG.md; update the wiki and UX-PATTERNS rows if the user-facing surface changed.
8. **Archive the plan** when the feature is fully shipped: stamp `docs/plans/$ARGUMENTS.md` as `IMPLEMENTED <today's date>`, `git mv` it to `docs/plans/archive/`, and fix inbound links (grep the filename) — the plan lifecycle in @docs/plans/TEMPLATE.md. Leaving it active is the most common drift after a ship.
9. **Green:** `make check`. If you changed the public API or CLI surface, expect `tests/test_public_api_snapshot.py` to go red — update the golden set deliberately, in the same change.

Checkpoint with me after the plan (step 2) and before pushing. The step-3 doc commits and step-4 branch happen once you approve the plan. Don't push unless I ask.
