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
   `## [Unreleased]`, leaving `[Unreleased]` empty above it. Do not
   reword or reorder the existing entries.

3. **Version.** Set `version = "X.Y.Z"` in `pyproject.toml` (drop the
   `.devN`), then `uv lock` so `uv.lock` records the release version.

4. **Build sanity.** `rm -rf dist && uv build` — confirm it builds
   `itemeval-X.Y.Z.tar.gz` + the wheel. Then confirm the package resolves:
   `uv run python -c "import itemeval; print(itemeval.__version__)"` prints
   `X.Y.Z`.

5. **Commit + tag + push.** Stage `CHANGELOG.md pyproject.toml uv.lock`, then:
   ```
   git commit -m "release: vX.Y.Z"
   git tag vX.Y.Z
   git push --follow-tags && git push origin vX.Y.Z
   ```
   `--follow-tags` only pushes *annotated* tags; the explicit `git push
   origin vX.Y.Z` covers the lightweight tag created above (harmless if
   already pushed).

6. **GitHub release (this is what triggers PyPI).** Extract the `[X.Y.Z]`
   section body from `CHANGELOG.md` (everything between this version's
   heading and the next `## [` heading, header line excluded) into a temp
   file, then:
   ```
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <body-file>
   ```

7. **Watch + confirm.** Find the run (`gh run list --workflow=release.yml
   --limit 1`), watch it (`gh run watch <id> --exit-status`), then poll
   PyPI until the index shows `X.Y.Z` (it lags the workflow by tens of
   seconds — retry, don't conclude failure on the first miss):
   ```
   curl -s https://pypi.org/pypi/itemeval/json \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   ```

8. **Dev bump (follow-up commit).** Set `version = "A.B.C.devN"` in
   `pyproject.toml`, `uv lock`, commit `chore: bump to A.B.C.devN`, push.

Report the release URL, the confirmed PyPI version, and the dev-bump commit.
