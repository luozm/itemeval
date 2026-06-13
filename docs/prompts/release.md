# Release automation prompt

Hand this file to an agent to cut a release. It encodes the
`DEVELOPMENT.md` "Release checklist" as runnable steps, including the
gotchas that bite in practice. Fill in `X.Y.Z` (the version being
released) and `A.B.C.devN` (the next dev version) before running, or let
the agent infer them from `pyproject.toml` + the `[Unreleased]` changelog.

---

**Task: release itemeval `vX.Y.Z` to PyPI, then bump to the next dev version.**

Prerequisites (verify, don't assume): you are on `main` with a clean tree,
`gh` is authenticated, and the `[Unreleased]` changelog section has real
entries to ship. PyPI publishing is trusted-publishing (OIDC) via
`.github/workflows/release.yml` on the `release: published` event — there is
no token to manage and nothing to `uv publish` locally.

1. **Green check.** `./.venv/bin/python -m pytest && ./.venv/bin/python -m ruff check .`
   Stop and report if either fails.

2. **Changelog.** In `CHANGELOG.md`, insert `## [X.Y.Z] - YYYY-MM-DD` (today,
   from the session's current date — do not invent one) directly under
   `## [Unreleased]`, leaving `[Unreleased]` empty above it. Then **tidy the
   new section** — entries accumulate during development as many separate
   `### Added` / `### Changed` blocks (one per change), which renders as
   ugly stacked "Added" headers on the GitHub release. Fix that here:
   - **Consolidate to one header per change-type** (`Added`, `Changed`,
     `Documentation`, …) for the release, in that conventional order. Move
     each bullet under the right single header; **preserve every bullet's
     text verbatim** — only the duplicate headers go away, nothing is
     reworded or dropped. Lead the most-important bullets first within a
     section. After editing, verify the bullet count is unchanged (e.g.
     `awk '/^## \[X.Y.Z\]/{f=1}/^## \[/{if(f&&!/X.Y.Z/)f=0}f&&/^- /' | wc -l`
     before vs after).
   - Add a 1–3 sentence **summary lead** under the version heading saying
     what the release is *about* (themes, not a feature list).
   - **Footer links:** add `[X.Y.Z]: …/releases/tag/vX.Y.Z` and point
     `[Unreleased]: …/compare/vX.Y.Z...HEAD` (was the previous tag).

3. **README status line.** Bump `**Status: vOLD …**` in `README.md` to
   `**Status: vX.Y.Z.**` — it is hand-maintained and easy to forget.

4. **Version.** Set `version = "X.Y.Z"` in `pyproject.toml` (drop the
   `.devN`), then `uv lock` so `uv.lock` records the release version.

5. **Build sanity.** `rm -rf dist && uv build` — confirm it builds
   `itemeval-X.Y.Z.tar.gz` + the wheel. Then confirm the package resolves:
   `uv run python -c "import itemeval; print(itemeval.__version__)"` prints
   `X.Y.Z`.

6. **Commit + tag + push.** Stage `README.md CHANGELOG.md pyproject.toml
   uv.lock`, then:
   ```
   git commit -m "release: vX.Y.Z"
   git tag vX.Y.Z
   git push --follow-tags && git push origin vX.Y.Z
   ```
   `--follow-tags` only pushes *annotated* tags; the explicit `git push
   origin vX.Y.Z` covers the lightweight tag created above (harmless if
   already pushed).

7. **GitHub release (this is what triggers PyPI).** Do **not** paste the raw
   changelog as the release body — the changelog is the exhaustive
   dev-facing record; release notes are for users skimming "what's new."
   Write **curated highlights** into a temp file instead:
   - Open with a 1–2 sentence summary of what the release is about.
   - Group the notable changes under a few themed `##` headings (e.g. by
     what the user gets, not by Added/Changed), 3–6 bullets each, plain
     language — fold the many small changelog bullets into the few that
     matter, drop internal-only detail.
   - End with a link to the full section,
     `…/blob/vX.Y.Z/CHANGELOG.md#xyz---yyyy-mm-dd`, and a
     `pip install --upgrade itemeval` line.
   Then publish (this fires `release.yml` → PyPI):
   ```
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <highlights-file>
   ```
   (To revise notes on an existing release: `gh release edit vX.Y.Z
   --notes-file <file>`.)

8. **Watch + confirm.** Find the run (`gh run list --workflow=release.yml
   --limit 1`), watch it (`gh run watch <id> --exit-status`), then poll
   PyPI until the index shows `X.Y.Z` (it lags the workflow by tens of
   seconds — retry, don't conclude failure on the first miss):
   ```
   curl -s https://pypi.org/pypi/itemeval/json \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   ```

9. **Dev bump (follow-up commit).** Set `version = "A.B.C.devN"` in
   `pyproject.toml`, `uv lock`, commit `chore: bump to A.B.C.devN`, push.

Report the release URL, the confirmed PyPI version, and the dev-bump commit.
