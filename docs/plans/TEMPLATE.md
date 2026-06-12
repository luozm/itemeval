# Implementation plan — <topic> (<one-line scope>)

<!--
This is the template for itemeval implementation plans. Copy it to
docs/plans/<topic>.md and delete every comment block. Conventions it encodes
were established by growth-ux / ux-compliance / cache-tail; keep them unless
a plan has a reason not to (and say the reason in the plan).

PLAN LIFECYCLE
- Active plans live flat in docs/plans/. There should rarely be more than
  one or two active at a time.
- Status line is always the first body line. Values:
  **NOT STARTED** → **IN PROGRESS (started <date>)** → **IMPLEMENTED <date>**.
- On IMPLEMENTED: stamp the status (keep the file as the design record,
  switch "is the working brief" framing to past tense), `git mv` the file to
  docs/plans/archive/, and fix inbound links (grep the repo for the
  filename). Same commit as the last workstream or its own `docs:` commit.
- A plan is a WORKING BRIEF for a fresh session: it must carry (or point to)
  every piece of context that session needs. Assume the implementer has no
  conversation history — only this file and the repo.

WHAT MAKES A PLAN GOOD HERE (learned the hard way)
- Investigate BEFORE writing: pin down the facts (file:line in this repo,
  and in the installed dependency source under .venv when wrapping
  inspect_ai). A plan built on assumed APIs dies on contact.
- Separate established facts from time-sensitive external facts. Mark the
  latter **[verify]** with what to check and where; the implementing session
  re-verifies and stamps the checked date in code comments.
- Design for the repo's contracts up front (UX bucket, boundary rules,
  simplicity) instead of retrofitting at review time.
- Name what is OUT OF SCOPE explicitly — scope creep enters through silence.
-->

**Status: NOT STARTED.** Written <date> against <load-bearing versions, e.g.
"inspect_ai 0.3.x (pinned in uv.lock)"> — re-verify the pinned facts below if
those moved. This file is the working brief for a fresh implementation
session: it carries all context that session needs. Read these first, in
order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, hint
   framework, the money gate is the only gate, JSON parity, append-only
   machine surface). Every workstream below states its bucket and
   interaction strength.
3. <other contract docs this plan discharges or extends, e.g.
   `docs/COST-OPTIMIZATION.md`, `DEVELOPMENT.md` (mandatory when anything
   touches inspect_ai: wrap don't fork; pass through don't rename; inspect
   imports confined to task-builder/orchestrator/extension modules)>
4. This file end-to-end before coding any part — the workstreams share
   design decisions.

Scope: <N> workstreams. **W1** <name> · **W2** <name> · …

---

## Context: <the facts that decide the design>

<!-- The load-bearing section. State the current-code facts (file:line,
quoted where it matters), the dependency facts (installed source, not docs
from memory), and the shared mechanism/design decisions all workstreams hang
on. If workstreams share a data table (provider facts, schema map), it lives
here once, marked up with [verify] where time-sensitive. -->

---

## W1 — <name>

**Goal.** <one paragraph: the user-visible outcome and why now.>

**Config / public surface.** <new knobs with their UX-PATTERNS bucket
(safety interlock / design declaration / optimization) and validation; new
result-model fields (append-only); new exports. "No new knob" is a valid and
often correct answer — say it explicitly.>

**Mechanism.** <how, at file:line level: which functions change, which
module owns new code, where the inspect boundary sits. Name the simplest
form that satisfies the spec; flag any tempting generality as rejected.>

**UX contract.** <interaction strength of every new output (hint with
stable code + owning wiki anchor / warning line / announcement line / gate —
remember: no new gates, money only); what gets announced (Law 1: every side
effect); JSON parity fields; ledger / hint-catalog rows to flip in
UX-PATTERNS.md **in the same commit**.>

**Tests.** <which files, what they assert, what is mocked. Unit tests never
call paid APIs; design the logic pure so it tests without inspect where
possible.>

**Docs/CHANGELOG.** <CHANGELOG [Unreleased] entry (same commit as the
behavior); wiki pages/anchors; rows to update in normative tables; FUTURE.md
items this closes.>

<!-- Repeat per workstream. -->

---

## Sequencing (canonical)

<!-- Numbered order with the dependency reasons ("W3 consumes W4's table").
One conventional commit per workstream unless stated. If a sibling plan
shares ordering, exactly ONE file owns the combined order — link the other
to it. End with the standing rule: -->

After each step: `./.venv/bin/python -m ruff check . && ./.venv/bin/python
-m ruff format .`, `./.venv/bin/python -m pytest`, CHANGELOG and normative
doc tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- <each rejected/deferred item, with where it IS tracked if anywhere
  (FUTURE.md item, upstream issue) — nothing gets dropped silently.>
