---
description: Start or continue a feature on the itemeval key-threaded lifecycle (BACKLOG → branch → plan → build → same-change rule)
argument-hint: <backlog-key>
disable-model-invocation: true
---
Drive the **Add a feature** lifecycle in @CONTRIBUTING.md for the key: **$ARGUMENTS**

The key is the feature's identity, threaded the whole way: BACKLOG section → ROADMAP plan → branch `feat/$ARGUMENTS` → `docs/plans/$ARGUMENTS.md` → CHANGELOG `Closes: $ARGUMENTS`.

1. **Key.** Confirm a `**Key:** `$ARGUMENTS`` section exists in @docs/BACKLOG.md. If it doesn't, stop and help me write the BACKLOG section first (motivation + design sketch + the key) — there must be something to point at before building.
2. **Plan.** If `docs/plans/$ARGUMENTS.md` doesn't exist, create it from @docs/plans/TEMPLATE.md and fill in the Context + workstream sections by investigating the *real* code (file:line in this repo, and in the installed dep under `.venv` when wrapping inspect_ai) — a plan built on assumed APIs dies on contact. Set the status line to `IN PROGRESS (started <today's date>)`. If it already exists, read it and continue from its status.
3. **Branch.** `git checkout -b feat/$ARGUMENTS` (skip if already on it).
4. **Build** the simplest thing that satisfies the spec (CLAUDE.md "don't over-engineer" — no speculative knobs). Code touching inspect_ai obeys the boundary rules in @DEVELOPMENT.md: wrap don't fork; pass through don't rename; flatten at the public API; inspect imports confined to the task-builder / orchestrator / extension modules. New config/data schemas are pydantic models.
5. **UX contract.** Walk the 9-question development checklist in @docs/UX-PATTERNS.md — it is binding, not advisory. Flip any ledger / hint-catalog rows in the same change.
6. **Same-change rule** (same commit as the behavior): add a `[Unreleased]` entry to @CHANGELOG.md with a `Closes: $ARGUMENTS` trailer; **remove** the shipped section from @docs/BACKLOG.md; update the wiki and UX-PATTERNS rows if the user-facing surface changed.
7. **Green:** `make check`. If you changed the public API or CLI surface, expect `tests/test_public_api_snapshot.py` to go red — update the golden set deliberately, in the same change.

Checkpoint with me after the plan (step 2) and before pushing. Don't push unless I ask.
