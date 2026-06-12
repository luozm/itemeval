"""Delta-aware estimate + gate on remaining (growth-ux 1.3) and the
replacement statement (1.5)."""

import json

import pytest

from itemeval import cli
from itemeval._config import load_config
from itemeval._prepare import prepare_study
from itemeval.budget._estimator import estimate_study
from itemeval.budget._gate import check_gate
from itemeval.generate._run import run_generate
from conftest import write_study_files


def test_fresh_study_remaining_equals_full(study):
    _, prep = study
    est = estimate_study(prep)
    g = est.generate
    assert g.remaining_usd == pytest.approx(g.usd)
    assert g.full_usd == g.usd
    assert g.completed_cells == 0 and g.rows_replaced == 0
    assert g.remaining_calls == g.calls
    # no solutions yet -> a grade run has nothing it could spend
    assert est.grade.remaining_usd == 0.0 and est.grade.usd > 0


def test_half_complete_remaining_below_full(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    run_generate(prepare_study(cfg), display="none")  # dev scope: 2 items x 2 reps
    prep_full = prepare_study(cfg, policy="full-interactive")  # 3 items
    est = estimate_study(prep_full)
    g = est.generate
    assert 0 < g.remaining_usd < g.usd
    assert g.completed_cells == 8  # 2 conds x 2 items x 2 reps already done
    assert g.total_cells == 12  # 2 conds x 3 items x 2 reps
    assert g.rows_replaced == 0  # purely additive growth replaces nothing
    assert g.remaining_calls == 4  # 2 conds x 1 new item x 2 reps


def test_complete_study_remaining_zero_and_gate_passes(
    tmp_path, offline_adapter, capsys, monkeypatch
):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "gated.yaml"
    config.write_text(config_yaml.replace("confirm_above_usd: 100", "confirm_above_usd: 0.0"))
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    # a $X-full / $0-remaining study passes the always-engaged gate without --yes
    assert cli.main(["generate", str(config)]) == 0
    out = capsys.readouterr().out
    assert "$0.00 remaining" in out or "remaining" in out
    assert "skipped: complete" in out


def test_force_restores_full_and_counts_replacements(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg)
    run_generate(prep, display="none")
    est = estimate_study(prep, force=True)
    g = est.generate
    assert g.remaining_usd == pytest.approx(g.usd)
    assert g.rows_replaced == 8  # every existing row re-runs under --force


def test_epoch_extension_counts_replacements(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    run_generate(prepare_study(cfg), display="none")
    bumped = write_study_files(
        tmp_path,
        config_yaml=(tmp_path / "config.yaml")
        .read_text()
        .replace("replications: 2", "replications: 4"),
    )
    prep = prepare_study(load_config(bumped))
    est = estimate_study(prep)
    g = est.generate
    # every item re-runs all 4 epochs; its 2 existing epochs get rewritten
    assert g.rows_replaced == 8  # 2 conds x 2 items x 2 existing epochs
    assert g.remaining_calls == 16  # 2 conds x 2 items x 4 epochs


def test_replacement_line_printed_at_gate(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["generate", str(config), "--yes", "--force"]) == 0
    out = capsys.readouterr().out
    assert "this run replaces 8 existing rows" in out


def test_replacement_line_absent_on_fresh_run(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert "this run replaces" not in capsys.readouterr().out


def test_manifest_records_remaining_and_full(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    cfg = load_config(config)
    manifest = json.loads((cfg.study_dir / doc["manifest_path"]).read_text())
    assert manifest["estimate_usd"] == pytest.approx(doc["estimate_usd"])
    assert manifest["estimate_full_usd"] >= manifest["estimate_usd"]
    assert doc["rows_replaced"] == 0


def test_gate_never_prompts_under_machine_mode():
    from itemeval._config import BudgetConfig

    # over threshold, no --yes, on a TTY: machine mode must not prompt (exit 3)
    result = check_gate(
        10.0,
        BudgetConfig(confirm_above_usd=5.0),
        assume_yes=False,
        interactive=True,
        machine=True,
    )
    assert result.proceed is False and result.exit_code == 3
    # --yes still proceeds; under-threshold still proceeds
    assert check_gate(10.0, BudgetConfig(confirm_above_usd=5.0), True, True, True).proceed
    assert check_gate(1.0, BudgetConfig(confirm_above_usd=5.0), False, True, True).proceed


def test_json_gate_never_prompts_via_cli(tmp_path, offline_adapter, capsys, monkeypatch):
    """--json on a TTY over threshold: no input() call, exit 3 with the document."""

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("gate prompted under --json"))
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "gated.yaml"
    config.write_text(config_yaml.replace("confirm_above_usd: 100", "confirm_above_usd: 0.0"))
    assert cli.main(["generate", str(config), "--json"]) == 3
    doc = json.loads(capsys.readouterr().out)
    assert doc["gate"]["exit_code"] == 3 and doc["estimate_full_usd"] > 0
