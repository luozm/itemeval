"""Python-surface consent: max_usd= raises before any API call (UXC Step 5)."""

import pytest

from itemeval import BudgetExceededError
from itemeval._config import load_config
from itemeval._prepare import prepare_study
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade
from conftest import write_study_files


def _forbidden_factory(model: str, stage: str, model_args=None):
    pytest.fail("model factory called — budget cap must stop the run before any API call")


def test_max_usd_raises_before_any_call(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    with pytest.raises(BudgetExceededError, match="max_usd argument") as exc:
        run_generate(prep, max_usd=1e-12, model_factory=_forbidden_factory, display="none")
    assert "no API calls were made" in str(exc.value)


def test_max_usd_under_threshold_proceeds(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    result = run_generate(prep, max_usd=100.0, display="none")
    assert result.rows_written == 8


def test_config_max_usd_enforced_in_python_path(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    cfg.budget.max_usd = 1e-12
    prep = prepare_study(cfg)
    with pytest.raises(BudgetExceededError, match="budget.max_usd"):
        run_generate(prep, model_factory=_forbidden_factory, display="none")


def test_grade_max_usd_raises_before_any_call(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    with pytest.raises(BudgetExceededError):
        run_grade(prep, max_usd=1e-12, model_factory=_forbidden_factory, display="none")


def test_cap_compares_against_remaining(tmp_path, offline_adapter):
    """A completed study has $0 remaining: a tiny cap no longer blocks re-runs."""
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    result = run_generate(prep, max_usd=1e-12, display="none")  # remaining = 0
    assert all(r.status == "skipped" for r in result.conditions)
