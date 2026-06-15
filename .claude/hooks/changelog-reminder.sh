#!/bin/sh
# Stop-hook reminder for the same-change rule (CLAUDE.md): if the working tree
# has changes under src/itemeval/ but CHANGELOG.md is untouched, surface a
# one-line nudge. Silent when there's nothing to report; never blocks the stop
# (always exits 0, no decision field), so it can't loop.

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

changes=$(git status --porcelain 2>/dev/null) || exit 0
echo "$changes" | grep -q 'src/itemeval/' || exit 0          # no source change -> silent
echo "$changes" | grep -q 'CHANGELOG\.md' && exit 0          # rule already satisfied

printf '%s\n' '{"systemMessage": "same-change rule: src/ changed but CHANGELOG.md is untouched — if this is user-visible, add an [Unreleased] entry (and Closes: <key> if it ships a BACKLOG feature), per CLAUDE.md."}'
exit 0
