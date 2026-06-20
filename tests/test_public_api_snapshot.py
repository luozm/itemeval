"""Public-surface snapshot — the pre-1.0 SemVer tripwire.

Golden sets of the public Python API (`itemeval.__all__`) and the CLI
subcommands. Adding/removing/renaming either fails this test, forcing an
*intentional* update here plus (per the same-change rule) a CHANGELOG entry —
so the public contract can never change by accident. Offline, no API calls.
"""

from __future__ import annotations

import argparse

import itemeval
from itemeval.cli import _build_parser

# Bump these golden sets deliberately, in the same commit as the API/CLI change
# and its CHANGELOG entry.
PUBLIC_API = {
    "BudgetExceededError",
    "ExperimentConfig",
    "Item",
    "ItemevalError",
    "__version__",
    "build_status",
    "estimate_study",
    "export_study",
    "load_config",
    "prepare_study",
    "run_generate",
    "run_grade",
}

CLI_COMMANDS = {"init", "estimate", "generate", "grade", "export", "status", "rebless"}


def test_public_api_surface_unchanged():
    assert set(itemeval.__all__) == PUBLIC_API, (
        "itemeval.__all__ changed — update PUBLIC_API and add a CHANGELOG entry.\n"
        f"  added:   {sorted(set(itemeval.__all__) - PUBLIC_API)}\n"
        f"  removed: {sorted(PUBLIC_API - set(itemeval.__all__))}"
    )


def test_every_public_name_resolves():
    # Catches a __all__ / lazy-export entry that doesn't actually load.
    for name in itemeval.__all__:
        assert getattr(itemeval, name) is not None


def test_cli_subcommands_unchanged():
    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    names = set(sub.choices)
    assert names == CLI_COMMANDS, (
        "CLI subcommands changed — update CLI_COMMANDS and add a CHANGELOG entry.\n"
        f"  added:   {sorted(names - CLI_COMMANDS)}\n"
        f"  removed: {sorted(CLI_COMMANDS - names)}"
    )
