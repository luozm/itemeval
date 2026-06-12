"""--policy override (growth-ux 1.1) and the pilot-available hint (1.2)."""

import json

import pytest

from itemeval import cli
from itemeval._config import load_config
from itemeval._errors import ConfigError
from itemeval._prepare import prepare_study
from conftest import write_study_files


def test_policy_override_beats_config(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    assert cfg.budget.policy == "dev"
    prep = prepare_study(cfg, policy="full-interactive")
    assert prep.plan.policy == "full-interactive"
    assert prep.plan.items_limit is None
    assert prep.policy_source == "override"
    assert len(prep.items_effective) == 3  # dev would cap at 2
    # no override -> config policy, source "config"
    prep = prepare_study(cfg)
    assert prep.plan.policy == "dev" and prep.policy_source == "config"


def test_invalid_policy_raises_config_error(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    with pytest.raises(ConfigError, match="invalid policy"):
        prepare_study(cfg, policy="warp-speed")


def test_manifest_records_policy_source(tmp_path, offline_adapter):
    from itemeval.generate._run import run_generate

    cfg = load_config(write_study_files(tmp_path))
    prep = prepare_study(cfg, policy="full-interactive")
    result = run_generate(prep, display="none")
    manifest = json.loads((cfg.study_dir / result.manifest_path).read_text())
    assert manifest["policy"] == "full-interactive"
    assert manifest["policy_source"] == "override"


def test_estimate_policy_flag_changes_scope(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config), "--json"]) == 0
    dev = json.loads(capsys.readouterr().out)
    assert cli.main(["estimate", str(config), "--policy", "full-interactive", "--json"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert dev["policy"] == "dev" and dev["policy_source"] == "config"
    assert full["policy"] == "full-interactive" and full["policy_source"] == "override"
    assert full["generate"]["calls"] > dev["generate"]["calls"]  # 3 items vs dev's 2


def test_invalid_policy_flag_argparse_exit_2(tmp_path, offline_adapter):
    config = write_study_files(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["estimate", str(config), "--policy", "bogus"])
    assert exc.value.code == 2


@pytest.fixture()
def gated_study(tmp_path, offline_adapter):
    """Fresh study whose gate always engages (confirm_above_usd: 0)."""
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "gated.yaml"
    config.write_text(config_yaml.replace("confirm_above_usd: 100", "confirm_above_usd: 0.0"))
    return config


def test_pilot_hint_fires_at_gate_on_fresh_study(gated_study, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    assert cli.main(["generate", str(gated_study)]) == 3
    err = capsys.readouterr().err
    assert "hint: " in err and "--policy dev" in err
    assert "Cost-Savings#never-pay-twice" in err


def test_pilot_hint_in_json_gate_stop(gated_study, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    assert cli.main(["generate", str(gated_study), "--json"]) == 3
    doc = json.loads(capsys.readouterr().out)
    assert any(h["code"] == "pilot-available" for h in doc["hints"])


def test_pilot_hint_absent_on_rerun(gated_study, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    assert cli.main(["generate", str(gated_study), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["generate", str(gated_study)]) == 3  # gate engages again
    err = capsys.readouterr().err
    assert "pilot" not in err and "--policy dev" not in err  # store no longer empty


def test_pilot_hint_absent_when_gate_silent(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)  # confirm_above_usd: 100 — gate never engages
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert "--policy dev" not in capsys.readouterr().err


def test_pilot_hint_suppressed_by_env(gated_study, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    monkeypatch.setenv("ITEMEVAL_HINTS", "off")
    assert cli.main(["generate", str(gated_study)]) == 3
    assert "hint: " not in capsys.readouterr().err
