# CLAUDE.md — itemeval (package development)

Publishable Python package: item-level LLM evaluation on inspect_ai. Studies
consuming it live in their own repos; never put study-specific content
(datasets, rubric texts, analysis) here.

## Doc map

- Spec & planning: `README.md` (spec) · `ROADMAP.md` (direction — vision,
  themes, near-term release plan; not a feature ledger) · `docs/BACKLOG.md`
  (candidate features not yet built — why/how, one keyed section each) ·
  `docs/plans/` (active briefs from `plans/TEMPLATE.md`; done → `plans/archive/`).
- `docs/UX-PATTERNS.md` — **binding** UX contract (no silent side effects,
  hint framework, consent rules, knob buckets); every feature, new or touched,
  must pass its checklist.
- `DEVELOPMENT.md` — inspect_ai boundary rules (wrap don't fork; pass through
  don't rename; flatten at the public API; inspect imports confined to the
  task-builder/orchestrator/extension modules), the upgrade pipeline, and the
  versioning/release process — follow for any code touching inspect_ai, a
  dependency bump, or a release.
- `CHANGELOG.md` — update `[Unreleased]` in the same change that makes a
  user-visible difference.
- `docs/COST-OPTIMIZATION.md` — maintainer reference for cost-saving mechanisms
  (user-facing: `docs/wiki/Cost-Savings.md`).
- User docs: `docs/wiki/` (published wiki + tutorials).
- Local-only, gitignored, never published: `local/` (STRATEGY.md,
  WORKING_MEM.md) · `local/archive/DESIGN.md` (retired M1–M6 design contract;
  its amendments record decisions that won during implementation).

## Python environment

- Managed by `uv`; all Python runs against `./.venv`, never system Python.
- Run commands via the Makefile (`make help` lists targets) or `uv run <cmd>`;
  both use `./.venv` without activating it. Never emit `source .venv/bin/activate`.
- New deps: `uv add <pkg>` (runtime) / `uv add --dev <pkg>` (dev). Never use
  pip. `pyproject.toml` carries ranges; `uv.lock` pins exactly and is committed.
- Recreate from scratch: `uv sync`. `./.venv` is disposable.
- Python >=3.11 (don't use 3.12+ syntax); develop on 3.12. Floor is 3.11
  because the tested stack pulls pandas 3.x.

## Conventions

- Don't over-engineer: simplest thing that satisfies the spec; no speculative
  abstractions, config knobs, or generality beyond current needs.
- src layout: code in `src/itemeval/`; tests import the installed package.
- Public API exported from `itemeval/__init__.py`; everything else is internal
  and free to refactor. Prefix private modules with `_`.
- pydantic models for all config/data schemas; YAML configs validated at load.
- Lint/format/test via the Makefile (the single command home — `make help`):
  `make lint` / `make fmt` / `make test` (fast, no network) / `make check`
  (lint + tests, what CI runs). Unit tests must not call paid APIs; provider
  code is mocked or marked for manual runs.
- No real API keys in tests, fixtures, or examples.
- Conventional commits (feat:/fix:/docs:/test:/refactor:).

## Planning & docs workflow

Three docs split the work by time — never duplicate a fact across them:
- `docs/BACKLOG.md` — candidate features **not yet built** (the only backlog).
- `ROADMAP.md` — direction + near-term release plan; references BACKLOG keys,
  never enumerates features or restates shipped detail.
- `CHANGELOG.md` — what shipped.

**Keys.** Every backlog feature has a stable kebab-case key, declared in its
BACKLOG section as `**Key:** \`slug\``. The key is its identity everywhere:
branch `feat/<slug>`, plan `docs/plans/<slug>.md`, CHANGELOG `Closes: <slug>`.
Scheduling (which keys land in which release) lives only in ROADMAP. Work
progress lives only in the plan file (NOT STARTED → IN PROGRESS → IMPLEMENTED).
A feature leaves BACKLOG when it ships.

**Same-change rule.** Any user-visible change, in the *same commit*:
1. add a `[Unreleased]` entry to `CHANGELOG.md`;
2. if it ships a backlog feature — **remove** that section from `docs/BACKLOG.md`
   (the design record stays in `docs/plans/archive/<slug>.md`) and add
   `Closes: <slug>` to the changelog entry;
3. update the wiki if user-facing, and the UX-PATTERNS ledger/hint rows if the
   surface changed.

**SSOT formats** (tooling parses these — keep them exact): the version lives
only in `pyproject.toml`; `README.md` carries one `**Status: vX.Y.Z.**` line
tracking the latest *released* CHANGELOG heading; CHANGELOG headings are
`## [X.Y.Z] - YYYY-MM-DD`. A non-runnable example YAML block starts with a
`# sketch` comment so config-validation tooling skips it.
