---
description: Audit the working tree against the itemeval same-change rule before you commit
when_to_use: Run before committing a user-visible change — especially to catch the judgment parts a hook can't (wiki / UX-PATTERNS prose that should have moved with the surface). The machine invariants also run in the pre-push hook and CI.
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(make docs-check:*), Read, Grep
---
Audit the uncommitted changes against the **same-change rule** (CLAUDE.md / @CONTRIBUTING.md "The one idea") and give me a ✓/✗ checklist verdict. Don't commit — just report.

Read `git status` + `git diff`, then check:

1. **CHANGELOG.** If anything under `src/` changed, is there a matching `[Unreleased]` entry in @CHANGELOG.md? (Same signal as the Stop-hook reminder — but this catches it before the commit.)
2. **BACKLOG / ROADMAP disjointness.** Does any key that now appears in a CHANGELOG `Closes:` still have a `**Key:**` section in @docs/BACKLOG.md, or still sit as a future candidate in @ROADMAP.md (anywhere but the `**Already landed**` line)? A shipped feature must have *left* the backlog and moved to "Already landed" in ROADMAP. Both are invariants `tests/test_docs_consistency.py` enforces — run `make docs-check` to confirm machine-side.
3. **Wiki / UX-PATTERNS.** Did the user-facing surface, or a knob / hint / ledger row, change without a matching `docs/wiki/` or `docs/UX-PATTERNS.md` update?
4. **SSOT formats.** Version lives only in `pyproject.toml`; README carries one `**Status: vX.Y.Z.**` line tracking the latest *released* CHANGELOG heading; non-runnable example YAML starts with `# sketch`.
5. **Public surface.** If `itemeval.__all__` or the CLI subcommands changed, is `tests/test_public_api_snapshot.py` updated deliberately (the pre-1.0 SemVer tripwire)?
6. **KNOWN-ISSUES disjointness.** If this change fixes a bug that was listed in @docs/KNOWN-ISSUES.md, is that entry **removed** in the same change (alongside its CHANGELOG `Fixed` entry)? A fixed bug must have *left* the tracker — the bug mirror of the BACKLOG-disjointness rule.

For each ✗, give the exact file + line to fix. End with the one-line bottom line: ready to commit, or not.
