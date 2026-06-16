---
description: Start a bug fix the itemeval way — failing test first, then fix, CHANGELOG, make check
argument-hint: <short-description>
disable-model-invocation: true
---
Fix a bug following the **Fix a bug** flow in @CONTRIBUTING.md. The bug is: **$ARGUMENTS**

1. **Branch.** `git checkout -b fix/<slug>` — derive a short kebab-case slug from the description; confirm the name with me if it's ambiguous.
2. **Failing test first.** Write a test that reproduces the bug and run it to confirm it's red. Show me the failure before fixing anything.
3. **Fix** the code until that test — and `make check` — goes green. Keep the change minimal; no opportunistic refactors riding along.
4. **CHANGELOG.** Add a `[Unreleased]` entry to @CHANGELOG.md in user-facing wording. Bugs need no backlog key.
5. **Green:** `make check`. Report it.

Stop before pushing — I'll review the diff. If this is a regression in the *latest release*, remind me it ships as a patch release promptly, not batched (DEVELOPMENT.md "When to release").
