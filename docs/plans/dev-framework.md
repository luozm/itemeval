# Implementation plan — dev-framework (drift-proof SDLC: docs model + guard automation)

**Status: IN PROGRESS (started 2026-06-15).** Tier 0 + Tier 1 shipped; Tier 2's
buildable items shipped 2026-06-15 (two Tier-2 items deferred to external
triggers, see W2); the Claude Code skill layer (W3) shipped 2026-06-16. This is the working brief for the development framework — the
rules that keep planning/coding/releasing consistent, and the automation that
enforces them. Read first: `CLAUDE.md` (the rules live there),
`docs/UX-PATTERNS.md`, `DEVELOPMENT.md`.

Not the same as `CONTRIBUTING.md`: this file is the *rollout plan* (what we're
building and its status); `CONTRIBUTING.md` is the steady-state *contributor
playbook* (how to add a feature / fix / release) and is itself a Tier-2
deliverable below.

---

## Context: why this exists

Every drift we hit (README stuck at v0.1.0; ROADMAP/FUTURE duplicating each
other and the changelog; shipped features lingering in the backlog) is one
disease: **a fact lives in more than one hand-edited place with nothing that
fails when the copies disagree.** itemeval already applies single-source +
fail-loud discipline to eval provenance (dataset locks, content-hashed
condition ids, ledger reconciliation, the 0.2 drift warnings). This framework
points the same discipline at the project's own SDLC.

**Principle.** Every fact has one authoritative home; every other place is
derived from it or checked against it in CI. A hand-maintained copy with no
check is a future drift.

**The docs model (one job per doc, on a time axis):**

| Doc | Time | Owns |
|---|---|---|
| `CHANGELOG.md` | past | what shipped (+ `Closes: <key>`) |
| `docs/BACKLOG.md` | candidate future | features not yet built — why/how, one `**Key:**` each |
| `ROADMAP.md` | committed future + direction | vision, themes, release plan (goal/keys/exit criteria), history pointers |
| plan files (`docs/plans/<key>.md`) | in-flight | a feature's implementation brief + work status |

A feature's identity is its **key** (kebab-case), threaded:
BACKLOG section → ROADMAP plan → branch `feat/<key>` → `docs/plans/<key>.md` →
CHANGELOG `Closes: <key>`. Scheduling lives only in ROADMAP; work-progress only
in the plan file; a shipped feature **leaves** BACKLOG. The **same-change rule**
(CLAUDE.md): a user-visible change updates CHANGELOG `[Unreleased]` in the same
commit, removes any shipped BACKLOG section, and touches wiki/UX-PATTERNS as
needed.

**Machine-checked vs human-curated.** Only docs with a tiny stable format are
parsed: `pyproject.toml` (version), README `**Status: vX.Y.Z.**`, CHANGELOG
`## [X.Y.Z] - DATE` + `Closes:`, BACKLOG `**Key:**`, doc YAML fences (`# sketch`
to skip). ROADMAP's *prose* stays human-curated, with one parsed convention: its
`**Already landed**` line is the sole place a shipped (`Closes:`) key may be
named, so a shipped key stranded in a planning section goes red (W2). Everything
else in ROADMAP is kept honest by the release checklist, not a regex.

**Locked decisions:** Makefile (not just); scheduled-PR for dependency updates
(not Renovate); `docs/BACKLOG.md` name; `**Key:** \`slug\`` markers; hermetic
CI end-to-end smoke deferred until `local-adapter` (over caching a Hub dataset).

---

## W0 — Tier 0: cheap guards (SHIPPED 2026-06-15)

The guards that turn drift into a red build. **Done.**

- **Makefile** — single home for dev commands (`make help|sync|lint|fmt|test|
  test-all|docs-check|check|build`), all via `uv run`. (`de1a182`)
- **`tests/test_docs_consistency.py`** + `make docs-check` — README status ==
  latest CHANGELOG release; every `configs/*.yaml` and runnable doc ```yaml```
  block validates through `load_config`. (`4560606`)
- **CI `docs` job** — runs `make docs-check` as its own check. (`92e17f2`)
- **`scripts/release_gate.py`** + `release.yml` step + release prompt — refuses
  a half-prepared release (version final/consistent, `[Unreleased]` consumed).
  (`1d8ba5d`)

---

## W1 — Tier 1: local fast-feedback + contract safety (IN PROGRESS)

- **pre-commit** (`.pre-commit-config.yaml` + `make hooks`): ruff check/format
  via the project's pinned ruff (local hook = single version source), light
  hygiene (trailing-whitespace, EOF, check-yaml, large-files), and a
  conventional-commit message check (allowing the repo's `release:` type).
  Mirrors CI so red happens at commit, not in CI.
- **pre-push gate** (`.pre-commit-config.yaml` `pre-push` stage): `git push` runs
  the full `make check`, so a red push is caught locally before CI.
- **Public-surface snapshot** (`tests/test_public_api_snapshot.py`): golden
  `itemeval.__all__` and CLI subcommand set; an accidental API/CLI change turns
  the build red and forces an intentional update + CHANGELOG entry — the pre-1.0
  SemVer tripwire.
- **Claude Code Stop-hook** (`.claude/settings.json`): a non-blocking reminder
  when `src/` changed without `CHANGELOG.md`, nudging CC toward the same-change
  rule. Soft (never blocks), quiet when nothing to report.

## W2 — Tier 2: collaboration + cross-repo + dep hygiene

Buildable items SHIPPED 2026-06-15; two items deferred to external triggers.

- **`CONTRIBUTING.md`** (SHIPPED `39e9aed`) — the steady-state contributor
  playbook (lifecycle: add a feature / fix a bug / release; links to the deep
  docs, never duplicates them). The human entry point GitHub surfaces on
  PRs/issues.
- **`.github/` templates** (SHIPPED `98df7f6`) — `PULL_REQUEST_TEMPLATE.md`
  (same-change DoD checklist + key), `ISSUE_TEMPLATE/{feature_request,
  bug_report}.md`, `CODEOWNERS`.
- **Key-disjointness check** (SHIPPED `ea1d759`) — `test_docs_consistency.py`
  asserts no `docs/BACKLOG.md` `**Key:**` appears in a CHANGELOG `Closes:`.
- **ROADMAP shipped-key check** (SHIPPED 2026-06-17) — `test_docs_consistency.py`
  asserts no CHANGELOG `Closes:` key still sits as a future candidate in
  `ROADMAP.md` (the `**Already landed**` bridge line excepted). The ROADMAP-side
  mirror of the key-disjointness check; added after `composite-item-id` shipped
  but lingered in ROADMAP's 0.4 candidate list (a hand-caught drift).
- **Scheduled dependency-update PR** (SHIPPED `4190a91`) — `.github/
  dependabot.yml`: weekly inspect-ai PR + grouped rest + monthly actions, per
  the now-archived `docs/plans/archive/upgrade-automation.md` (Dependabot over
  Renovate). Operational verification happens after the first push to remote.
- **Downstream smoke** (DEFERRED) — the consuming study installs the new wheel
  and runs an API smoke, so cross-repo breakage surfaces fast. Blocked on the
  consuming study being a wired-up sibling repo; it lives in a separate repo, so
  this can't be built or tested hermetically from here. Revisit when that repo
  is ready to call a published wheel in its own CI.
- **Hermetic end-to-end CLI smoke** (DEFERRED) — ships with `local-adapter`
  (mock models + committed JSONL fixture, zero network); see that BACKLOG entry
  (`local-adapter` "CI follow-on") and the `ci.yml` note. Tracked there, not
  here.

**Deliberately NOT building** (CLAUDE.md "don't over-engineer"): a generated
docs site, a custom doc DSL, per-sentence doc tests, branch protection while
solo. Add only when a collaborator or scale demands them.

---

## W3 — Claude Code workflow skills (SHIPPED 2026-06-16)

A thin agent-facing layer over the same rules: one `.claude/skills/<name>/SKILL.md`
per lifecycle job, each a *thin orchestrator* that points at the SSOT docs above
rather than restating them, so a skill can't drift from the rule it runs. (Custom
slash commands and skills are the same mechanism in current Claude Code; this uses
the skills form for its invocation control.)

- **Lifecycle → slash command:** `/brainstorm` (idea → `docs/BACKLOG.md` key),
  `/roadmap` (direction + scheduling in `ROADMAP.md`), `/feature <key>` (the full
  implement-then-archive lifecycle), `/fix`, `/same-change` (pre-commit audit),
  `/release` (runs `docs/prompts/release.md`), `/upgrade-inspect` (the inspect-ai
  pipeline). CONTRIBUTING's cheat-sheet is the job → skill map.
- **Invocation control.** Side-effecting skills set `disable-model-invocation:
  true` (manual only — the agent must not auto-cut a release). `/same-change`
  stays model-invocable so the agent runs the judgment audit proactively. The
  deterministic pre-push gate is the *hook* (W1), not a skill.
- **No new rules.** Skills encode no policy of their own; the bodies cite
  CLAUDE.md / DEVELOPMENT.md / UX-PATTERNS as the authority.

---

## Sequencing

W0 done. W1 done (one commit per piece: pre-commit · snapshot tests · Claude
hook). W2 buildable items done (one commit per piece: key check · dependabot ·
CONTRIBUTING · templates); the two deferred items land when their external
trigger does (`local-adapter` for the hermetic smoke; a wired consuming repo for
the downstream smoke). After each step: `make check` green; CHANGELOG/docs
updated in the same commit.

## Out of scope (tracked elsewhere)

- The hermetic e2e smoke — on `local-adapter` in `docs/BACKLOG.md`.
- The scheduled upgrade PR detail — `docs/plans/archive/upgrade-automation.md`
  (implemented; archived).
- The PyPI approval gate — `docs/BACKLOG.md` (`pypi-approval-gate`).
