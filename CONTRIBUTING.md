# Contributing to itemeval

Thanks for working on itemeval. This is the **playbook** — the steps for the
common jobs. The *rules* live in deeper docs and this file links to them rather
than restate them (one fact, one home — that's the whole philosophy below).

itemeval is a publishable package: item-level LLM evaluation on `inspect_ai`. A
study that *uses* itemeval lives in its own repo; never add study-specific
datasets, rubrics, or analysis here.

## Cheat-sheet

```bash
uv sync && make hooks   # one-time setup
make check              # before every push — lint + fast tests (exactly what CI runs)
make fmt                # auto-format + safe lint fixes
```

| Job | Skill | Flow |
|---|---|---|
| **Brainstorm a feature** | `/brainstorm` | rough idea → pressure-test (in scope? simplest?) → a [BACKLOG](docs/BACKLOG.md) section with a `**Key:**` |
| **Long-term goals** | `/roadmap` | discuss direction; schedule BACKLOG keys into a release in [ROADMAP](ROADMAP.md) (references keys, never restates them) |
| **Feature** | `/feature <key>` | key in [BACKLOG](docs/BACKLOG.md) → plan `docs/plans/<key>.md` → commit docs to `main` → branch `feat/<key>` → build → same-change rule (CHANGELOG `Closes: <key>` + drop the BACKLOG section) → `make check` → push |
| **Quick fix** | `/fix <desc>` | `git checkout -b fix/x` → failing test → fix → CHANGELOG `Fixed` → `make check` → commit (`fix:`) → push |
| **Track a deferred bug** | — | a bug you're not fixing now → a section in [KNOWN-ISSUES](docs/KNOWN-ISSUES.md) (symptom · where · fix sketch); no key |
| **Release** | `/release` | hand [docs/prompts/release.md](docs/prompts/release.md) to an agent; `release_gate.py` blocks a half-baked one |
| **Pre-push gate** | — *(auto)* | the `pre-push` hook runs `make check` on every push — nothing to type |
| **Pre-commit audit** | `/same-change` | same-change rule as a ✓/✗ checklist (CHANGELOG · BACKLOG-disjoint · wiki · SSOT); **machine parts auto** |
| **Engine bump** | `/upgrade-inspect` | deliberate inspect-ai lockfile bump + `make test-all` ([DEVELOPMENT.md](DEVELOPMENT.md)) |

Each job is detailed below. `make check` is the one habit — and the `pre-push`
git hook now runs it for you on every push (`make hooks` installs that alongside
the formatting + commit-msg hooks), so a red push can't slip into CI.

The **Skill** column is the [Claude Code](https://code.claude.com/docs/en/skills)
shortcut for each job. The new-feature pipeline runs top-down: `/brainstorm`
shapes an idea into a BACKLOG key, `/roadmap` schedules keys into a release,
`/feature <key>` implements one. The pre-push gate needs no skill — the hook is
the gate; `/same-change` stays a skill for the wiki/UX judgment a hook can't make
(and is the one skill Claude also runs proactively — its machine checks already
ride the pre-push hook plus the Stop-hook nudge). Every other skill is
manual-trigger: you type it, Claude won't auto-run its side effects. All live in
`.claude/skills/` and just run their row's flow, adding no rules of their own.

## The one idea

Every fact has **one authoritative home**; every other place is derived from it
or checked against it in CI. A hand-maintained copy with no check is a future
drift. The guards (`make check`, the docs-consistency tests, pre-commit, the
release gate) exist so a disagreement becomes a red build, not a surprise later.

## Setup (once)

```bash
uv sync            # create ./.venv from the lockfile (never use system Python)
make hooks         # install the git hooks (pre-commit, commit-msg, pre-push)
make help          # list every dev command
```

All Python runs through `./.venv` via `uv run` / the `make` targets — never
activate the venv or call `pip`. Python floor is 3.11; develop on 3.12 (no
3.12+-only syntax). See [CLAUDE.md](CLAUDE.md) for the full environment rules.

## Where things live

| Doc | What it owns |
|---|---|
| [CLAUDE.md](CLAUDE.md) | environment, conventions, the planning & docs workflow |
| [DEVELOPMENT.md](DEVELOPMENT.md) | inspect_ai boundary rules, dependency/upgrade policy, the release process |
| [docs/UX-PATTERNS.md](docs/UX-PATTERNS.md) | the **binding** UX contract every feature must pass |
| [ROADMAP.md](ROADMAP.md) | direction + what's committed for the next release |
| [docs/BACKLOG.md](docs/BACKLOG.md) | candidate features not yet built (one `**Key:**` each) |
| [docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md) | deferred bugs — the bug mirror of BACKLOG (no key) |
| [CHANGELOG.md](CHANGELOG.md) | what shipped |
| `docs/plans/<key>.md` | a feature's in-flight implementation brief |

A feature's identity is its **key** (kebab-case), threaded the whole way:
BACKLOG section → ROADMAP plan → branch `feat/<key>` → `docs/plans/<key>.md` →
CHANGELOG `Closes: <key>`.

## Add a feature

1. **Find or declare the key.** If it's in [docs/BACKLOG.md](docs/BACKLOG.md),
   use that `**Key:**`. If not, add a backlog section first (motivation, design
   sketch, a new key — or `/brainstorm` it) so there's something to point at.
2. **Plan it.** Copy [docs/plans/TEMPLATE.md](docs/plans/TEMPLATE.md) to
   `docs/plans/<key>.md`; that file tracks work status (NOT STARTED → IN
   PROGRESS → IMPLEMENTED). Scheduling stays in ROADMAP, not the plan.
3. **Commit the planning docs to `main` first** — *before* branching, as
   `docs:` commits: the BACKLOG entry (it's `/brainstorm`'s deliverable) and the
   plan. **Don't combine them onto the feature branch.** Planning artifacts are
   *inputs* to the feature, not its output; active plans live on `main`
   (TEMPLATE.md); and keeping them off the branch makes the branch diff exactly
   *the implementation + its same-change paperwork* — so the shipping commit's
   BACKLOG **removal** (step 7) reads as a clean deletion, not the back-half of
   an add/remove churn. If the plan investigation forced a correction to the
   BACKLOG entry, commit that fix here too.
4. **Branch:** `git checkout -b feat/<key>`.
5. **Build it.** Keep the simplest thing that satisfies the spec — no
   speculative knobs (CLAUDE.md "don't over-engineer"). Code touching
   `inspect_ai` follows the boundary rules in [DEVELOPMENT.md](DEVELOPMENT.md)
   (wrap don't fork; pass through don't rename; flatten at the public API).
   New config/data schemas are pydantic models.
6. **Pass the UX contract.** Run the development checklist in
   [docs/UX-PATTERNS.md](docs/UX-PATTERNS.md) — no silent side effects, consent
   rules, hint framework, knob buckets. This is binding, not advisory.
7. **Apply the same-change rule** (in the *same commit* as the user-visible
   change): add a `[Unreleased]` entry to [CHANGELOG.md](CHANGELOG.md) with
   `Closes: <key>`; **remove** the shipped section from
   [docs/BACKLOG.md](docs/BACKLOG.md) (its design record lives on in the plan,
   which moves to `docs/plans/archive/`); update the wiki and the UX-PATTERNS
   ledger if the surface changed.
8. **Green before PR:** `make check` (lint + fast tests, what CI runs). If you
   changed the public API or CLI surface, expect
   `tests/test_public_api_snapshot.py` to go red — update the golden set
   deliberately, in the same change.

## Fix a bug

Bugs get **no key, no BACKLOG entry, no plan** — keys are for features.

`git checkout -b fix/<short-desc>` → write a failing test first, then fix → add
a `[Unreleased]` → `Fixed` entry to [CHANGELOG.md](CHANGELOG.md) (no key) →
`make check` → PR. If the fix changes a user-facing surface (output text, a
`--json` field, an exit code, a hint or knob) the same-change rule still applies
— update the wiki / UX-PATTERNS rows in the same commit. A regression in the
latest release ships as a patch release promptly, not batched (see
DEVELOPMENT.md "When to release").

**Not fixing it now?** Record it in
[docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md) — the bug mirror of BACKLOG
(symptom · where · fix sketch), no key. It **leaves** that file in the same
change that adds its CHANGELOG `Fixed` entry. If the "fix" turns out to need real
design, it graduates to a feature (BACKLOG key + plan) instead.

## Commits & PRs

- **Conventional Commits:** `feat:` `fix:` `docs:` `test:` `refactor:` `chore:`
  `ci:` `build:` `perf:` (the commit-msg hook enforces this; `release:` is
  reserved for release commits).
- Run `make fmt` before committing if you skipped the hooks.
- The PR template's checklist is the same-change rule in checkbox form — fill it
  in honestly. CI must be green (lint + test matrix + docs consistency).

## Reporting issues

Use the issue templates (feature request / bug report). For anything touching
provenance, cost accounting, or the export schema, include the itemeval version
(`uv run python -c "import itemeval; print(itemeval.__version__)"`, also recorded
in every run manifest) and the relevant config — those are the load-bearing
surfaces consuming studies pin against.


# Maintainer only
## Release (maintainer)

The full process is the checklist in
[DEVELOPMENT.md](DEVELOPMENT.md#versioning-discipline); to run it hands-off,
hand [docs/prompts/release.md](docs/prompts/release.md) to an agent.
`scripts/release_gate.py` refuses a half-prepared release.

## Going multi-contributor (maintainer)

Solo today: direct pushes to `main`, no required reviews — nothing forces a PR
or an approval. When a second contributor joins, flip on the protections that
have been waiting for exactly this moment (the PR template, CODEOWNERS, and the
CI checks already exist):

1. **Branch protection on `main`** (Settings → Rules → Rulesets):
   - *Require a pull request before merging* — makes CODEOWNERS an auto-reviewer.
   - *Require status checks to pass* — the CI matrix + the docs-consistency job.
   - *Block force pushes* + *restrict deletions* — worth enabling **even while
     solo**: pure safety, zero workflow change.
2. **Dependabot auto-merge** (optional, and only once branch protection exists,
   since auto-merge needs a required check to gate on): scope it to the grouped
   `dependencies` and `actions` PRs — **never inspect-ai**, which needs the
   manual live smoke before paid runs (DEVELOPMENT.md). Until then, merging the
   weekly PRs by hand is the intended ~1-click chore.

Don't enable PR-required protection while solo — it would just block your own
direct pushes. It's a one-click upgrade when the time comes.
