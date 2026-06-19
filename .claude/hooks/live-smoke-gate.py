#!/usr/bin/env python3
"""PreToolUse(Bash) gate: before Claude Code runs ``git push`` on a feat/* branch,
run the live API smoke (``make test-live``) and block the push if it fails.

By design (maintainer's request): triggered by CC, never manually, never in CI.
It self-disables when there is no ``OPENAI_API_KEY`` (so keyless shells — and CI,
which runs no CC hooks anyway — are never blocked) and when
``ITEMEVAL_SKIP_LIVE_SMOKE`` is set (a deliberate one-off escape hatch).

Scope: only ``git push`` from a gated branch (feat/* or fix/*). Other commands,
other branches, and the user's own manual pushes pass straight through. A
successful smoke costs a fraction of a cent.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

ALLOW = 0
BLOCK = 2  # PreToolUse: a non-zero (2) exit blocks the tool call; stderr goes to Claude.

# Branch prefixes whose CC pushes run the smoke first. feat/* and fix/* both ship
# code that can regress the real-model path. main and pure-docs branches are left
# ungated; chore/* (inspect bumps) is a candidate — extend this tuple to cover it.
GATED_PREFIXES = ("feat/", "fix/")


def allow(msg: str | None = None) -> None:
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(ALLOW)


def is_git_push(cmd: str) -> bool:
    """True iff cmd invokes ``git push`` as a subcommand (not 'push' in a message)."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    for i, tok in enumerate(tokens):
        if tok != "git":
            continue
        for nxt in tokens[i + 1 :]:  # the subcommand is the first non-flag token
            if nxt.startswith("-"):
                continue
            if nxt == "push":
                return True
            break
    return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()  # never break tool use on a parse hiccup
    cmd = (data.get("tool_input") or {}).get("command", "") or ""

    if not is_git_push(cmd):
        allow()
    if os.environ.get("ITEMEVAL_SKIP_LIVE_SMOKE"):
        allow("live smoke skipped (ITEMEVAL_SKIP_LIVE_SMOKE set) — push allowed")

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not branch.startswith(GATED_PREFIXES):
        allow()  # only gated branches (GATED_PREFIXES) run the smoke
    if not os.environ.get("OPENAI_API_KEY"):
        allow("live smoke skipped (no OPENAI_API_KEY) — push allowed")

    proc = subprocess.run(["make", "test-live"], capture_output=True, text=True)
    if proc.returncode == 0:
        allow(f"live smoke passed — {branch} push allowed")

    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
    print(
        f"BLOCKED: live API smoke (make test-live) failed before pushing {branch}.\n"
        "This catches real-model / inspect-integration breakage that mock tests\n"
        "cannot. Fix it, or set ITEMEVAL_SKIP_LIVE_SMOKE=1 to push past deliberately.\n"
        "--- smoke log tail ---\n"
        f"{tail}",
        file=sys.stderr,
    )
    sys.exit(BLOCK)


if __name__ == "__main__":
    main()
