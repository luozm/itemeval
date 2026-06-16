---
description: Brainstorm a new feature and, if it survives scrutiny, turn it into a docs/BACKLOG.md entry with a key
argument-hint: <rough idea>
disable-model-invocation: true
---
Brainstorm a candidate feature — the upstream of `/feature`, which only *implements* a key that already exists. Goal: go from the rough idea to either a `docs/BACKLOG.md` section with a new key, or a clear "not now / out of scope" with the reason. The idea is: **$ARGUMENTS**

1. **Restate** the idea in a sentence or two so we agree on what it is.
2. **Pressure-test it before writing anything:**
   - **In scope?** itemeval is a publishable package — item-level LLM eval on inspect_ai. Reject study-specific content (datasets, rubric texts, analysis); that belongs in a consuming study's own repo, not here (CLAUDE.md).
   - **Simplest thing?** Push back on speculative knobs, abstractions, or generality beyond the current need (CLAUDE.md "don't over-engineer"). Name the smallest version that delivers the value.
   - **Boundary fit?** If it touches inspect_ai, sketch how it stays inside the boundary rules (wrap don't fork; pass through don't rename; flatten at the public API) — @DEVELOPMENT.md.
   - **New?** Grep @docs/BACKLOG.md and @CHANGELOG.md so we don't re-propose something already queued or shipped.
3. **Decide.** If it doesn't survive, say so plainly and stop — don't write a backlog entry for something we wouldn't build (note *why* it's out of scope only if that saves re-proposing it later).
4. **If it survives, draft a BACKLOG section** in the file's existing style: the motivation (why), a design sketch (how — the simplest form), and a stable kebab-case key declared with the `**Key:**` marker exactly as the other sections do. That key is the feature's identity everywhere (branch `feat/<key>`, plan `docs/plans/<key>.md`, CHANGELOG `Closes: <key>`), so confirm it isn't already used in a CHANGELOG `Closes:` — the disjointness invariant `tests/test_docs_consistency.py` enforces. Any non-runnable example YAML starts with `# sketch`.
5. **Confirm with me before writing** to @docs/BACKLOG.md (no silent side effects). Scheduling does **not** go here — if it's worth committing to a release, hand off to `/roadmap`; BACKLOG is only the candidate.

This touches planning docs only — **no CHANGELOG entry** (that happens when the feature *ships*, via `/feature` + the same-change rule). Handoff: once it has a key, `/feature <key>` implements it.
