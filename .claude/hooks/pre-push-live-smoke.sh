#!/bin/sh
# PreToolUse(Bash) fast prefilter. The matcher fires on every Bash call, so the
# common case must be cheap: if the command doesn't even mention "push", return
# instantly (no python startup). Only the rare "push"-mentioning command is
# handed to the live-smoke gate, which makes the precise decision (is it really
# `git push` on a feat/* branch? run make test-live, block on failure). See
# live-smoke-gate.py. The pipeline's exit status is python's, so a block (2)
# propagates and stops the push.
input=$(cat)
case "$input" in
  *push*) ;;
  *) exit 0 ;;
esac
dir=$(dirname "$0")
printf '%s' "$input" | python3 "$dir/live-smoke-gate.py"
