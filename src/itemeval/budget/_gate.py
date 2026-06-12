"""Estimate-before-run confirmation gate."""

import sys

from pydantic import BaseModel, ConfigDict

from itemeval._config import BudgetConfig


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proceed: bool
    exit_code: int  # 0 | 3 (declined / confirmation required) | 4 (max_usd)
    reason: str


def check_gate(
    estimate_usd: float,
    budget: BudgetConfig,
    assume_yes: bool,
    interactive: "bool | None" = None,
    machine: bool = False,
) -> GateResult:
    if interactive is None:
        interactive = sys.stdin.isatty()
    if machine:
        # --json declares a machine consumer: never prompt, even on a TTY —
        # proceed under threshold or with --yes, otherwise exit 3.
        interactive = False
    if budget.max_usd is not None and estimate_usd > budget.max_usd:
        return GateResult(
            proceed=False,
            exit_code=4,
            reason=(
                f"estimated ${estimate_usd:.2f} exceeds budget.max_usd "
                f"(${budget.max_usd:.2f}) — hard cap, not overridable"
            ),
        )
    if estimate_usd <= budget.confirm_above_usd:
        return GateResult(
            proceed=True,
            exit_code=0,
            reason=(
                f"estimated ${estimate_usd:.2f} within confirm_above_usd "
                f"(${budget.confirm_above_usd:.2f})"
            ),
        )
    if assume_yes:
        return GateResult(
            proceed=True,
            exit_code=0,
            reason=f"estimated ${estimate_usd:.2f} confirmed via --yes",
        )
    if interactive:
        answer = input(
            f"Estimated cost ${estimate_usd:.2f} exceeds confirm_above_usd "
            f"(${budget.confirm_above_usd:.2f}). Proceed? [y/N] "
        )
        if answer.strip().lower() in {"y", "yes"}:
            return GateResult(
                proceed=True,
                exit_code=0,
                reason=f"estimated ${estimate_usd:.2f} confirmed interactively",
            )
        return GateResult(proceed=False, exit_code=3, reason="declined at prompt")
    return GateResult(
        proceed=False,
        exit_code=3,
        reason=(
            f"estimated ${estimate_usd:.2f} exceeds confirm_above_usd "
            f"(${budget.confirm_above_usd:.2f}); re-run with --yes to confirm"
        ),
    )
