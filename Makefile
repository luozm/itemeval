# itemeval developer commands — run `make` (or `make help`) to list them.
# Every target runs inside the uv-managed ./.venv via `uv run`; never activate
# the venv by hand. CI and (later) pre-commit call these same targets, so
# "passes locally" means "passes in CI".

.DEFAULT_GOAL := help
.PHONY: help sync lint fmt test test-all docs-check check build hooks precommit

help:  ## list available targets
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

sync:  ## install deps from the lockfile (uv sync --locked)
	uv sync --locked

lint:  ## ruff lint + format check (writes nothing)
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test:  ## fast unit tests (no network)
	uv run pytest -m "not network"

test-all:  ## full suite, including network (HF Hub) tests
	uv run pytest

docs-check:  ## doc/version/config-example consistency tests only
	uv run pytest tests/test_docs_consistency.py -q

check: lint test  ## what CI runs: lint + fast tests (incl. docs-check)

hooks:  ## install the git hooks: pre-commit, commit-msg, pre-push (run once)
	uv run pre-commit install --install-hooks

precommit:  ## run all pre-commit hooks against every file
	uv run pre-commit run --all-files

build:  ## build sdist + wheel into dist/
	rm -rf dist && uv build
