import builtins

from itemeval import Item
from itemeval._config import BudgetConfig
from itemeval.budget._gate import check_gate
from itemeval.budget._policies import apply_items_limit, effective_plan


def test_dev_policy_limits_items_and_disables_batch():
    plan = effective_plan(BudgetConfig(policy="dev", batch=True), replications=4)
    assert plan.items_limit == 2
    assert plan.replications == 4
    assert plan.batch is None  # dev runs are interactive


def test_dev_replications_cap():
    plan = effective_plan(BudgetConfig(policy="dev", dev_replications=1), replications=4)
    assert plan.replications == 1


def test_full_batch_auto():
    assert effective_plan(BudgetConfig(policy="full-batch"), 2).batch is True
    assert effective_plan(BudgetConfig(policy="full-interactive"), 2).batch is None
    assert effective_plan(BudgetConfig(policy="full-interactive", batch=True), 2).batch is True
    assert effective_plan(BudgetConfig(policy="full-batch", batch=False), 2).batch is None
    assert effective_plan(BudgetConfig(policy="full-batch", batch=100), 2).batch == 100


def test_apply_items_limit():
    items = [Item(id=str(i), input="q") for i in range(5)]
    assert len(apply_items_limit(items, 2)) == 2
    assert len(apply_items_limit(items, None)) == 5


def test_gate_under_threshold_proceeds():
    result = check_gate(
        1.0, BudgetConfig(confirm_above_usd=5.0), assume_yes=False, interactive=False
    )
    assert result.proceed and result.exit_code == 0


def test_gate_max_usd_never_overridable():
    result = check_gate(10.0, BudgetConfig(max_usd=5.0), assume_yes=True, interactive=True)
    assert not result.proceed and result.exit_code == 4


def test_gate_yes_overrides_confirmation():
    result = check_gate(
        10.0, BudgetConfig(confirm_above_usd=5.0), assume_yes=True, interactive=False
    )
    assert result.proceed and "--yes" in result.reason


def test_gate_non_interactive_declines():
    result = check_gate(
        10.0, BudgetConfig(confirm_above_usd=5.0), assume_yes=False, interactive=False
    )
    assert not result.proceed and result.exit_code == 3


def test_gate_interactive_prompt(monkeypatch):
    answers = iter(["y", "nope"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))
    yes = check_gate(10.0, BudgetConfig(confirm_above_usd=5.0), assume_yes=False, interactive=True)
    assert yes.proceed
    no = check_gate(10.0, BudgetConfig(confirm_above_usd=5.0), assume_yes=False, interactive=True)
    assert not no.proceed and no.exit_code == 3
