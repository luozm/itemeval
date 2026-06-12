"""Drift warnings (growth-ux 1.4): config drift and endpoint drift."""

import json

from itemeval import cli
from itemeval._config import load_config
from itemeval._driftcheck import endpoint_drift_warnings
from itemeval._prepare import prepare_study
from itemeval.generate._run import run_generate
from conftest import MINIMAL_PROMPT, write_study_files


def test_untouched_study_has_no_warnings(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    run_generate(prepare_study(cfg), display="none")
    result = run_generate(prepare_study(cfg), display="none")
    assert result.warnings == []


def test_edited_prompt_warns_once_with_facet_and_count(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    run_generate(prepare_study(cfg), display="none")  # 8 rows under the old hash
    (tmp_path / "prompts" / "solver" / "minimal.md").write_text(MINIMAL_PROMPT + "\nEDITED.\n")
    result = run_generate(prepare_study(load_config(tmp_path / "config.yaml")), display="none")
    drift = [w for w in result.warnings if "prompt 'minimal' changed" in w]
    assert len(drift) == 1
    assert "8 existing rows" in drift[0] and "fresh condition" in drift[0]
    # the slug-level check must not double-report the same template edit
    assert not any("condition '" in w for w in result.warnings)


def test_sampling_change_warns_on_unchanged_slug(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    run_generate(prepare_study(cfg), display="none")
    edited = (tmp_path / "config.yaml").read_text().replace("temperature: 0.3", "temperature: 0.9")
    (tmp_path / "config.yaml").write_text(edited)
    result = run_generate(prepare_study(load_config(tmp_path / "config.yaml")), display="none")
    assert any("changed since last run (id " in w for w in result.warnings)


def test_endpoint_drift_from_divergent_manifests(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for i, served in enumerate(["gpt-5-mini-2026-01-15", "gpt-5-mini-2026-05-01"]):
        manifests.joinpath(f"r{i}.json").write_text(
            json.dumps(
                {
                    "created_at": f"2026-06-0{i + 1}T00:00:00Z",
                    "grid_generate": [
                        {"id": "c1", "slug": "s", "payload": {"model": "openai/gpt-5-mini"}}
                    ],
                    "grid_grade": [],
                    "endpoints_effective": {"c1": {"served_model": served}},
                }
            )
        )
    warnings = endpoint_drift_warnings(["openai/gpt-5-mini"], manifests, gap_days=10_000)
    assert len(warnings) == 1
    assert "openai/gpt-5-mini" in warnings[0]
    assert "gpt-5-mini-2026-01-15" in warnings[0] and "gpt-5-mini-2026-05-01" in warnings[0]
    # a model with consistent history stays silent
    assert endpoint_drift_warnings(["other/model"], manifests, gap_days=10_000) == []


def test_endpoint_gap_warning(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifests.joinpath("r0.json").write_text(
        json.dumps(
            {
                "created_at": "2020-01-01T00:00:00Z",
                "grid_generate": [],
                "grid_grade": [],
                "endpoints_effective": {},
            }
        )
    )
    [warning] = endpoint_drift_warnings(["m/x"], manifests)
    assert "days ago" in warning and ">30d" in warning


def test_warnings_in_cli_text_and_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    (tmp_path / "prompts" / "solver" / "minimal.md").write_text(MINIMAL_PROMPT + "\nEDITED.\n")
    capsys.readouterr()
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert "warning: prompt 'minimal' changed" in capsys.readouterr().out
    assert cli.main(["generate", str(config), "--yes", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any("prompt 'minimal' changed" in w for w in doc["warnings"])
