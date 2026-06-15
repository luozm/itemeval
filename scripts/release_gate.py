#!/usr/bin/env python3
"""Release-readiness gate — run before tagging and as the first step in release.yml.

Usage:
    python3 scripts/release_gate.py [vX.Y.Z]

With no argument the version is read from pyproject.toml. Exits non-zero, listing
every reason, if the repo is not in a consistent state to release X.Y.Z:
  - the version is final (no dev/pre-release suffix);
  - pyproject.toml is set to exactly that version;
  - CHANGELOG.md has a `## [X.Y.Z] - YYYY-MM-DD` section;
  - CHANGELOG.md `[Unreleased]` is empty (entries were moved into the release);
  - README.md carries `**Status: vX.Y.Z.**`.

Stdlib only, reads files relative to the current directory, so it runs before
`uv sync` and is easy to invoke locally.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path.cwd()
PRE_RELEASE = re.compile(r"(dev|a|b|rc|post)", re.IGNORECASE)


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _unreleased_is_empty(changelog: str) -> bool:
    m = re.search(r"## \[Unreleased\]\n(.*?)\n## \[", changelog, re.DOTALL)
    if not m:
        return False  # a missing Unreleased section is itself a problem
    return m.group(1).strip() == ""


def main(argv: list[str]) -> int:
    version = (argv[1] if len(argv) > 1 else _pyproject_version()).lstrip("v")
    problems: list[str] = []

    if PRE_RELEASE.search(version):
        problems.append(f"version {version!r} is a dev/pre-release — releases must be final")

    pv = _pyproject_version()
    if pv != version:
        problems.append(f"pyproject.toml version is {pv!r}, expected {version!r}")

    changelog = (ROOT / "CHANGELOG.md").read_text()
    if not re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}", changelog, re.MULTILINE
    ):
        problems.append(f"CHANGELOG.md has no `## [{version}] - YYYY-MM-DD` section")
    if not _unreleased_is_empty(changelog):
        problems.append(
            "CHANGELOG.md `[Unreleased]` is not empty — move entries into the release section"
        )

    if f"**Status: v{version}.**" not in (ROOT / "README.md").read_text():
        problems.append(f"README.md is missing `**Status: v{version}.**`")

    if problems:
        print(f"release gate FAILED for {version}:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"release gate OK for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
