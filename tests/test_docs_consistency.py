"""Docs stay consistent with reality.

Four guards, all pure-local and offline (no network, no API) so they run in
`make docs-check` / CI:

1. Version SSOT — the README status line tracks the latest *released* CHANGELOG
   heading, and pyproject is at or ahead of it.
2. Example configs validate — every shipped `configs/*.yaml` and every runnable
   ```yaml block in the docs (top-level ``study:``, not marked ``# sketch``)
   loads through the real ``itemeval.load_config`` schema validator.
3. Key disjointness — a `docs/BACKLOG.md` `**Key:**` (a feature *not yet built*)
   must never appear in a CHANGELOG `Closes:` (a feature that *shipped*). A key
   in both means a shipped feature was left in the backlog — the exact drift the
   same-change rule forbids.
4. ROADMAP doesn't strand a shipped key — a CHANGELOG `Closes:` key (a feature
   that *shipped*) must not still be named as a future candidate in `ROADMAP.md`;
   the `**Already landed**` bridge line is the one sanctioned place an
   in-`[Unreleased]` key may be named. The ROADMAP-side mirror of guard 3 — only
   `key`-token membership is parsed, never ROADMAP's human-curated prose.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

import itemeval

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Version consistency
# --------------------------------------------------------------------------- #
def _ver_tuple(v: str) -> tuple[int, ...]:
    base = re.split(r"[.+]?(?:dev|post|a|b|rc)", v)[0].rstrip(".")
    return tuple(int(x) for x in base.split("."))


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _changelog_latest_release() -> str:
    for m in re.finditer(r"^## \[(\d+\.\d+\.\d+)\] - ", (ROOT / "CHANGELOG.md").read_text(), re.M):
        return m.group(1)
    raise AssertionError("no released `## [X.Y.Z] - DATE` heading in CHANGELOG.md")


def _readme_status() -> str:
    m = re.search(r"\*\*Status: v(\d+\.\d+\.\d+)\.\*\*", (ROOT / "README.md").read_text())
    assert m, "README.md is missing a `**Status: vX.Y.Z.**` line"
    return m.group(1)


def test_readme_status_matches_latest_release():
    assert _readme_status() == _changelog_latest_release(), (
        "README `**Status:**` is out of sync with the latest CHANGELOG release"
    )


def test_pyproject_version_at_or_ahead_of_release():
    assert _ver_tuple(_pyproject_version()) >= _ver_tuple(_changelog_latest_release()), (
        f"pyproject version {_pyproject_version()} is behind the latest "
        f"CHANGELOG release {_changelog_latest_release()}"
    )


# --------------------------------------------------------------------------- #
# Example configs validate against the schema
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _config_examples() -> list[tuple[str, str]]:
    """(label, yaml-text) for every config that should validate."""
    cases = [
        (str(p.relative_to(ROOT)), p.read_text()) for p in sorted((ROOT / "configs").glob("*.yaml"))
    ]
    for md in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        for i, body in enumerate(_FENCE.findall(md.read_text())):
            first = next((ln for ln in body.splitlines() if ln.strip()), "")
            if first.lstrip().startswith("# sketch"):
                continue  # explicitly-flagged fragment
            try:
                data = yaml.safe_load(body)
            except yaml.YAMLError:
                continue  # not standalone YAML -> not a config example
            if isinstance(data, dict) and "study" in data:  # a full config, not a snippet
                cases.append((f"{md.relative_to(ROOT)}#yaml{i}", body))
    return cases


_CASES = _config_examples()


def test_found_config_examples():
    # Guard against the extractor silently matching nothing (e.g. fence syntax drift).
    assert _CASES, "no example configs discovered — check the YAML-fence extractor"


@pytest.mark.parametrize("label,body", _CASES, ids=[c[0] for c in _CASES])
def test_example_config_validates(label, body, tmp_path):
    (tmp_path / "config.yaml").write_text(body)
    itemeval.load_config(tmp_path / "config.yaml")  # raises ConfigError on schema drift


# --------------------------------------------------------------------------- #
# Backlog keys and shipped keys are disjoint
# --------------------------------------------------------------------------- #
def _backlog_keys() -> set[str]:
    text = (ROOT / "docs" / "BACKLOG.md").read_text()
    return set(re.findall(r"^\*\*Key:\*\* `([a-z0-9-]+)`", text, re.M))


def _changelog_closed_keys() -> set[str]:
    # `Closes: slug` or `Closes: slug-a, slug-b` (comma-separated).
    text = (ROOT / "CHANGELOG.md").read_text()
    keys: set[str] = set()
    for group in re.findall(r"^Closes:\s*(.+)$", text, re.M):
        keys.update(k.strip() for k in group.split(",") if k.strip())
    return keys


def test_found_backlog_keys():
    # Guard against the extractor silently matching nothing (e.g. marker drift).
    assert _backlog_keys(), "no `**Key:**` markers found in docs/BACKLOG.md — check the extractor"


def test_found_shipped_keys():
    # Guard 4 below is vacuous if this set is empty (e.g. `Closes:` marker drift).
    assert _changelog_closed_keys(), "no `Closes:` keys found in CHANGELOG.md — check the extractor"


def test_backlog_and_shipped_keys_disjoint():
    leaked = _backlog_keys() & _changelog_closed_keys()
    assert not leaked, (
        f"keys are both in docs/BACKLOG.md and a CHANGELOG `Closes:`: {sorted(leaked)} — "
        "a shipped feature must leave the backlog (same-change rule, CLAUDE.md)"
    )


# --------------------------------------------------------------------------- #
# ROADMAP doesn't strand a shipped key as a future candidate
# --------------------------------------------------------------------------- #
# A shipped feature has left BACKLOG (guard 3). ROADMAP cites BACKLOG keys to
# *schedule* them; once a key ships it must move from a planning section to the
# sanctioned "Already landed" bridge — the transient line naming keys that sit in
# [Unreleased] until the release cuts. A shipped key lingering in any other
# ROADMAP block is the drift this guard catches. ROADMAP stays human-curated:
# only `key`-token membership is parsed (against the shipped set), never prose.
_ROADMAP_LANDED_MARKER = "already landed"


def _roadmap_blocks() -> list[str]:
    return re.split(r"\n\s*\n", (ROOT / "ROADMAP.md").read_text())


def test_roadmap_does_not_strand_shipped_keys():
    shipped = _changelog_closed_keys()
    stranded: set[str] = set()
    for block in _roadmap_blocks():
        if _ROADMAP_LANDED_MARKER in block.lower():
            continue  # the sanctioned bridge may name a key still in [Unreleased]
        stranded |= {k for k in re.findall(r"`([a-z0-9-]+)`", block) if k in shipped}
    assert not stranded, (
        f"shipped keys named in a forward-looking ROADMAP.md section: {sorted(stranded)} — a "
        "shipped feature has left BACKLOG, so it must move to the `**Already landed**` line (or "
        "out of ROADMAP); it is no longer a future candidate (same-change rule, CLAUDE.md)"
    )
