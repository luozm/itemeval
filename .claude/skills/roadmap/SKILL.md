---
description: Discuss direction and update ROADMAP.md — themes, the near-term release plan, and which keys land in which release
argument-hint: [topic]
disable-model-invocation: true
---
Discuss or update long-term goals. `ROADMAP.md` owns **direction + the near-term release plan** — vision, themes, each release's goal / keys / exit criteria, and history pointers. Topic: **$ARGUMENTS**

Hold these boundaries — the repo's "one fact, one home" rule; drift starts the moment a fact is restated:
- ROADMAP **references BACKLOG keys**; it never enumerates features or restates shipped detail. Candidate-feature design lives in @docs/BACKLOG.md; what shipped lives in @CHANGELOG.md.
- **Scheduling lives only here** — not in plan files, not in BACKLOG. Which keys land in which release is a ROADMAP decision.

For a **discussion**: reason about direction from what already exists — open @ROADMAP.md, the candidate keys in @docs/BACKLOG.md, and the shipped history in @CHANGELOG.md. If the conversation turns up a genuinely new feature idea, don't invent its detail here — hand to `/brainstorm` to give it a BACKLOG key first, then schedule the key.

For an **update** to @ROADMAP.md, the common moves:
- Schedule one or more BACKLOG keys into a release (goal + keys + exit criteria).
- Adjust themes, exit criteria, or direction.
- On a release: move the shipped version from **Release plan** to **History** as a one-line CHANGELOG pointer, and promote the next release into the plan.

Confirm the change with me before writing (no silent side effects). This touches planning docs only — **no CHANGELOG entry**.
