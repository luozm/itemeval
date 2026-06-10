# CLAUDE.md — itemeval (package development)

Publishable Python package: item-level LLM evaluation on inspect_ai.
`README.md` is the spec; `ROADMAP.md` is the milestone plan — keep it updated
as milestones complete. `DEVELOPMENT.md` defines the inspect_ai upgrade
pipeline and the versioning/release process — follow it for any dependency
bump or release; update `CHANGELOG.md` ([Unreleased]) in the same change that
makes a user-visible difference. Any study consuming this package lives in its
own separate repo; never put study-specific content (a particular study's
datasets, rubric texts, or analysis) in this package.

## Python environment

- Managed by `uv`. All Python runs against `./.venv`; system Python is never used.
- Invoke by absolute path, never activate: `./.venv/bin/python -m pytest`.
  Never emit `source .venv/bin/activate`.
- New deps: `uv add <pkg>` (runtime) / `uv add --dev <pkg>` (dev). Never call
  pip directly. `pyproject.toml` carries ranges; `uv.lock` pins exactly and is
  committed.
- Recreate from scratch: `uv sync`. `./.venv` is disposable.
- Supports Python >=3.11 (don't use 3.12+ syntax); develop on 3.12. Floor is
  3.11 because the tested stack pulls pandas 3.x, which requires >=3.11.

## Conventions

- src layout: code in `src/itemeval/`; tests import the installed package.
- Public API is exported from `itemeval/__init__.py`; everything else is
  internal and free to refactor. Prefix private modules with `_`.
- pydantic models for all config/data schemas; YAML configs validated at load.
- Lint/format: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .`
- Tests: `./.venv/bin/python -m pytest`. Unit tests must not call paid APIs;
  anything touching providers is mocked or marked for manual runs.
- No real API keys in tests, fixtures, or examples.
- Conventional commits (feat:/fix:/docs:/test:/refactor:); update CHANGELOG.md
  for user-visible changes once releases start.
