# Development Guide

Process documentation for maintaining itemeval. For coding conventions see
`CLAUDE.md`; for direction and the release plan see `ROADMAP.md`; for the UX contract every
feature must follow (output, side effects, consent), see
`docs/UX-PATTERNS.md`.

## Dependency policy

- `pyproject.toml` declares **ranges** (lower bounds, e.g. `inspect-ai>=0.3.239`)
  — this is the contract published to package users.
- `uv.lock` pins **exact versions** of everything — this is what `uv sync`
  reproduces locally and in CI. Always committed.
- Add/remove only via `uv add <pkg>` / `uv remove <pkg>`; never edit dependency
  lists by hand and never call pip.
- Upper-bound pins (`<X`) are allowed only as a temporary response to a known
  breakage, with a linked issue and removal plan.

## inspect_ai boundary

itemeval is a design-and-accounting façade over inspect_ai, the execution
engine. They meet at one narrow waist: build a `Task`, call `eval()`, flatten
the `EvalLog`. Rules for any code touching the boundary:

- **Wrap, don't fork.** If inspect has it (providers, retry, batch, caching,
  logs), call it. Extend only via published extension points, drop-in
  compatible (e.g. `_cachegate.gated_generate` is an `@solver`). Bypass a
  subsystem only when its abstraction conflicts with item-level provenance
  (e.g. `scorer=None`, scores parsed post-hoc) — and say why in code.
- **Pass through, don't rename.** Model id strings, API-key env vars,
  `INSPECT_DISPLAY`/display modes, and `GenerateConfig` knob names surface to
  users unchanged; itemeval docs explain every knob so users never need
  inspect's docs. One documented escape hatch: raw `.eval` logs are kept and
  indexed, openable with `inspect view`.
- **Flatten at the boundary.** inspect types never cross the public API —
  results are itemeval pydantic models and flat parquet/CSV; `EvalLog` types
  appear only under `TYPE_CHECKING` outside the waist.
- **Keep the contact surface small.** inspect imports live only in the task
  builders (`generate/_task`, `grade/_judge`), orchestrators
  (`generate/_run`, `grade/_run`), and extensions (`_cachegate`,
  `_mockmodels`); config/design/store/budget/CLI stay engine-free, and
  `import itemeval` stays lazy so no-API commands never pay inspect's import
  cost. This small waist is exactly the watch list for upgrades below.

## inspect_ai upgrade pipeline

inspect-ai is the load-bearing dependency and releases frequently. Upgrades are
**deliberate, never incidental** — routine `uv sync` keeps using the lockfile
pin, so versions only move when we move them.

Cadence: at the start of each release cycle, and before any large paid run
in a consuming study.

1. **Check what's new**
   ```bash
   uv tree --package inspect-ai --depth 0     # current pinned version
   ```
   Read the release notes: https://github.com/UKGovernmentBEIS/inspect_ai/releases
   Watch for: model-provider changes, batch/caching behavior, log-format (.eval
   schema) changes, dataframe API (`samples_df`/`evals_df`) changes.
2. **Upgrade on a branch**
   ```bash
   git checkout -b chore/bump-inspect-ai
   uv lock --upgrade-package inspect-ai && uv sync
   ```
3. **Unit tests** (no API calls): `./.venv/bin/python -m pytest`
4. **Live smoke test** (manual, costs cents): run the consuming study's pilot
   config at `dev` scope end-to-end (generate → grade → export) and confirm
   the export schema and cost ledger are unchanged.
5. **Commit** the lockfile bump: `chore: bump inspect-ai 0.3.X -> 0.3.Y`, with
   any behavior notes in the body. Merge.
6. **On breakage**: pin a temporary upper bound in `pyproject.toml`
   (`inspect-ai>=0.3.X,<0.3.Y`), open an issue describing the incompatibility,
   and remove the bound when fixed.

Once CI exists (ROADMAP M7): add a scheduled weekly GitHub Actions job (or
Renovate/Dependabot) that opens the upgrade PR automatically; steps 3–5 run in
CI, step 4 stays manual.

All other dependencies: `uv lock --upgrade && uv sync` quarterly, same
branch-test-commit flow, less scrutiny.

## Versioning discipline

**Semantic versioning**, version lives in one place: `pyproject.toml`
(`itemeval.__version__` reads it via package metadata — never duplicate it).

- **Pre-1.0 (now)**: `0.MINOR.PATCH`. Minor bumps may break APIs (expected at
  this stage and noted in the changelog); patch bumps are fixes only.
  Between releases the version carries a `.devN` suffix (e.g. `0.1.0.dev0`).
- **Post-1.0**: MAJOR = breaking, MINOR = backwards-compatible features,
  PATCH = backwards-compatible fixes. Breaking changes are deprecated with a
  runtime warning for at least one minor release before removal.

**CHANGELOG.md** follows [Keep a Changelog](https://keepachangelog.com):
user-visible changes are added to the `[Unreleased]` section in the same PR
that makes them — never reconstructed at release time.

PyPI publishing uses **trusted publishing** (OIDC from GitHub Actions — no API
token stored). One-time setup: on PyPI, add a trusted publisher for the project
(owner `luozm`, repo `itemeval`, workflow `release.yml`, environment blank). The
publish itself runs in `.github/workflows/release.yml`, triggered when a GitHub
release is published; locally you only build/tag.

**Release checklist** (applies from v0.1.0, ROADMAP M7). To run it
hands-off, hand `docs/prompts/release.md` to an agent — it encodes these
steps as runnable commands.

1. Tests and lint green: `./.venv/bin/python -m pytest && ./.venv/bin/python -m ruff check .`
2. Move `[Unreleased]` entries under a new `[X.Y.Z] - YYYY-MM-DD` heading, then
   **consolidate them to one header per change-type** (`Added`/`Changed`/…) —
   entries accrue as many separate `### Added` blocks during development, which
   render as stacked "Added" headers on the release. Preserve every bullet's
   text; add a short summary lead and the `[X.Y.Z]` / `[Unreleased]` footer links.
3. Set `version = "X.Y.Z"` in `pyproject.toml` (drop the `.devN`), and sync the
   hand-maintained docs: the `**Status: vX.Y.Z.**` line in `README.md`; in
   `ROADMAP.md`, move the released version from the **Release plan** to
   **History** (a one-line CHANGELOG pointer). Shipped features should already
   have left `docs/BACKLOG.md` under the same-change rule (each closed by a
   CHANGELOG `Closes: <key>`); confirm none of the release's keys still appear
   there.
4. Optionally verify the build locally: `uv build` (the same command CI runs).
5. Commit `release: vX.Y.Z`; tag and push: `git tag vX.Y.Z && git push origin main --tags`.
6. Create a GitHub release from the tag with **curated user-facing highlights**
   (themed, plain-language — *not* the raw changelog dump), ending with a link to
   the full changelog section. Publishing to PyPI is automatic: the
   `release: published` event triggers `release.yml`, which runs
   `uv build && uv publish` via trusted publishing. Watch the Actions run and
   confirm the new version appears on PyPI.
7. Bump to the next dev version (e.g. `0.2.0.dev0`) in a follow-up commit.

Consuming studies pin itemeval like any dependency: editable path source during
development, exact-version pin from PyPI once published — their `uv.lock` plus
run manifests (which record `itemeval.__version__`) keep results reproducible.
