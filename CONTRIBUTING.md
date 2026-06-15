# Contributing to itemeval

Thanks for working on itemeval. This is the **playbook** — the steps for the
common jobs. The *rules* live in deeper docs and this file links to them rather
than restate them (one fact, one home — that's the whole philosophy below).

itemeval is a publishable package: item-level LLM evaluation on `inspect_ai`. A
study that *uses* itemeval lives in its own repo; never add study-specific
datasets, rubrics, or analysis here.

## The one idea

Every fact has **one authoritative home**; every other place is derived from it
or checked against it in CI. A hand-maintained copy with no check is a future
drift. The guards (`make check`, the docs-consistency tests, pre-commit, the
release gate) exist so a disagreement becomes a red build, not a surprise later.

## Setup (once)

```bash
uv sync            # create ./.venv from the lockfile (never use system Python)
make hooks         # install the pre-commit + commit-msg git hooks
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
| [CHANGELOG.md](CHANGELOG.md) | what shipped |
| `docs/plans/<key>.md` | a feature's in-flight implementation brief |

A feature's identity is its **key** (kebab-case), threaded the whole way:
BACKLOG section → ROADMAP plan → branch `feat/<key>` → `docs/plans/<key>.md` →
CHANGELOG `Closes: <key>`.

## Add a feature

1. **Find or declare the key.** If it's in [docs/BACKLOG.md](docs/BACKLOG.md),
   use that `**Key:**`. If not, add a backlog section first (motivation, design
   sketch, a new key) so there's something to point at.
2. **Plan it.** Copy [docs/plans/TEMPLATE.md](docs/plans/TEMPLATE.md) to
   `docs/plans/<key>.md`; that file tracks work status (NOT STARTED → IN
   PROGRESS → IMPLEMENTED). Scheduling stays in ROADMAP, not the plan.
3. **Branch:** `git checkout -b feat/<key>`.
4. **Build it.** Keep the simplest thing that satisfies the spec — no
   speculative knobs (CLAUDE.md "don't over-engineer"). Code touching
   `inspect_ai` follows the boundary rules in [DEVELOPMENT.md](DEVELOPMENT.md)
   (wrap don't fork; pass through don't rename; flatten at the public API).
   New config/data schemas are pydantic models.
5. **Pass the UX contract.** Run the development checklist in
   [docs/UX-PATTERNS.md](docs/UX-PATTERNS.md) — no silent side effects, consent
   rules, hint framework, knob buckets. This is binding, not advisory.
6. **Apply the same-change rule** (in the *same commit* as the user-visible
   change): add a `[Unreleased]` entry to [CHANGELOG.md](CHANGELOG.md) with
   `Closes: <key>`; **remove** the shipped section from
   [docs/BACKLOG.md](docs/BACKLOG.md) (its design record lives on in the plan,
   which moves to `docs/plans/archive/`); update the wiki and the UX-PATTERNS
   ledger if the surface changed.
7. **Green before PR:** `make check` (lint + fast tests, what CI runs). If you
   changed the public API or CLI surface, expect
   `tests/test_public_api_snapshot.py` to go red — update the golden set
   deliberately, in the same change.

## Fix a bug

`git checkout -b fix/<short-desc>` → write a failing test first, then fix →
add a `[Unreleased]` CHANGELOG entry (no key needed; bugs aren't backlog
features) → `make check` → PR. A regression in the latest release ships as a
patch release promptly, not batched (see DEVELOPMENT.md "When to release").

## Commits & PRs

- **Conventional Commits:** `feat:` `fix:` `docs:` `test:` `refactor:` `chore:`
  `ci:` `build:` `perf:` (the commit-msg hook enforces this; `release:` is
  reserved for release commits).
- Run `make fmt` before committing if you skipped the hooks.
- The PR template's checklist is the same-change rule in checkbox form — fill it
  in honestly. CI must be green (lint + test matrix + docs consistency).

## Release

Maintainer-only and rare. The full process is the checklist in
[DEVELOPMENT.md](DEVELOPMENT.md#versioning-discipline); to run it hands-off,
hand [docs/prompts/release.md](docs/prompts/release.md) to an agent.
`scripts/release_gate.py` refuses a half-prepared release.

## Reporting issues

Use the issue templates (feature request / bug report). For anything touching
provenance, cost accounting, or the export schema, include the itemeval version
(`uv run python -c "import itemeval; print(itemeval.__version__)"`, also recorded
in every run manifest) and the relevant config — those are the load-bearing
surfaces consuming studies pin against.
