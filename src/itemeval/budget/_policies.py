"""Run policies: dev / full-interactive / full-batch -> effective execution plan."""

from pydantic import BaseModel, ConfigDict

from itemeval._config import BudgetConfig
from itemeval._item import Item


class EffectivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: str
    items_limit: int | None  # dev: budget.dev_items; else None
    replications: int  # dev: min(reps, dev_replications or reps); else reps
    batch: bool | int | None  # GenerateConfig.batch value; None = caching/batch off


def effective_plan(
    budget: BudgetConfig, replications: int, policy: "str | None" = None
) -> EffectivePlan:
    """Resolve the execution plan; `policy` (e.g. CLI --policy) overrides config."""
    policy = policy or budget.policy
    if budget.batch is False:
        batch: "bool | int | None" = None
    elif budget.batch == "auto":
        batch = True if policy == "full-batch" else None
    else:
        batch = budget.batch
    if policy == "dev":
        batch = None  # dev runs are interactive
        return EffectivePlan(
            policy="dev",
            items_limit=budget.dev_items,
            replications=min(replications, budget.dev_replications or replications),
            batch=batch,
        )
    return EffectivePlan(policy=policy, items_limit=None, replications=replications, batch=batch)


def apply_items_limit(items: "list[Item]", limit: "int | None") -> "list[Item]":
    """First N of the concatenated item list (datasets in config order)."""
    return list(items) if limit is None else list(items)[:limit]
