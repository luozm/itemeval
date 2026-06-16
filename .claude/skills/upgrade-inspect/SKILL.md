---
description: Run the inspect-ai upgrade pipeline (deliberate lockfile bump on a branch + full test suite)
disable-model-invocation: true
---
Run the **inspect_ai upgrade pipeline** in @DEVELOPMENT.md. inspect-ai is the load-bearing engine — upgrades are deliberate, never incidental, so routine `uv sync` keeps the pin until we move it here.

1. **What's new.** `uv tree --package inspect-ai --depth 0` for the current pin, then summarize the release notes between it and latest (https://github.com/UKGovernmentBEIS/inspect_ai/releases). Flag specifically: model-provider changes, batch/caching behavior, `.eval` log-schema changes, and dataframe API (`samples_df` / `evals_df`) changes — those four are the boundary watch list.
2. **Branch + bump.** `git checkout -b chore/bump-inspect-ai`, then `uv lock --upgrade-package inspect-ai && uv sync`.
3. **Full suite** (no API calls): `make test-all` — an engine bump should exercise everything, including the HF-adapter test, not just the fast set.
4. **Live smoke** is manual and gates *paid runs*, not the merge. Remind me to run the consuming study's pilot config at `dev` scope end-to-end (generate → grade → export) and confirm the export schema + cost ledger are unchanged. Don't attempt it from here — it needs the separate study repo.
5. **Commit** the lockfile bump: `chore: bump inspect-ai 0.3.X -> 0.3.Y`, with any behavior notes in the body.
6. **On breakage:** pin a temporary `inspect-ai>=0.3.X,<0.3.Y` upper bound in `pyproject.toml`, open an issue describing the incompatibility, and note the removal plan — then stop and report.

Report the version delta, the notable release-note items, and the `make test-all` result. Don't merge or push without my go-ahead.
