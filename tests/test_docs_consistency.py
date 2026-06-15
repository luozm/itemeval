"""Docs stay consistent with reality.

Two guards, both pure-local and offline (no network, no API) so they run in
`make docs-check` / CI:

1. Version SSOT — the README status line tracks the latest *released* CHANGELOG
   heading, and pyproject is at or ahead of it.
2. Example configs validate — every shipped `configs/*.yaml` and every runnable
   ```yaml block in the docs (top-level ``study:``, not marked ``# sketch``)
   loads through the real ``itemeval.load_config`` schema validator.
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
    cases = [(str(p.relative_to(ROOT)), p.read_text()) for p in sorted((ROOT / "configs").glob("*.yaml"))]
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
