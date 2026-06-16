---
description: Cut an itemeval release to PyPI (executes the DEVELOPMENT.md release checklist via the agent prompt)
argument-hint: [X.Y.Z]
disable-model-invocation: true
---
Read @docs/prompts/release.md and execute it **exactly** — it encodes the `DEVELOPMENT.md` "Release checklist" as runnable steps, including the gotchas that bite in practice (consolidating the changelog to one header per change-type, the three hand-maintained status docs that get forgotten, the release gate before tagging, and trusted-publishing fired by the GitHub release event).

Version to release: **$ARGUMENTS** — if empty, infer `X.Y.Z` from `pyproject.toml` (drop the `.devN`) and the next dev version from there.

First verify the prerequisites the prompt lists: on `main`, clean tree, `gh` authenticated, `[Unreleased]` has real entries. Stop and report if `make check` or `python3 scripts/release_gate.py` fails — never tag a half-prepared release. Tagging, pushing, and `gh release create` are outward-facing and trigger the PyPI publish: confirm with me before those steps.
