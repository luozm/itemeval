import json

import pytest

from itemeval import cli
from conftest import write_study_files


def test_unknown_config_exits_2(tmp_path, capsys):
    assert cli.main(["status", str(tmp_path / "missing.yaml")]) == 2
    assert "error" in capsys.readouterr().err


def test_missing_subcommand_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_status_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["status", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["study"] == "tstudy"
    assert len(report["generate"]) == 2


def test_estimate_plain_and_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config)]) == 0
    out = capsys.readouterr().out
    assert "GENERATE" in out and "GRADE" in out and "total projected" in out
    assert cli.main(["estimate", str(config), "--json"]) == 0
    est = json.loads(capsys.readouterr().out)
    assert est["generate"]["calls"] == 8


def test_generate_gate_declines_non_interactively(tmp_path, offline_adapter, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "gated.yaml"
    config.write_text(config_yaml.replace("confirm_above_usd: 100", "confirm_above_usd: 0.0"))
    assert cli.main(["generate", str(config)]) == 3
    assert "confirm" in capsys.readouterr().err
    # --yes overrides
    assert cli.main(["generate", str(config), "--yes"]) == 0


def test_missing_template_exits_2(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    (tmp_path / "prompts" / "solver" / "minimal.md").unlink()
    assert cli.main(["status", str(config)]) == 2
    assert "template" in capsys.readouterr().err
