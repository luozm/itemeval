"""Export rewrote-wording and batch announcement (UX-compliance Step 6)."""

import json

from itemeval import cli
from conftest import write_study_files


def _run_pipeline(config):
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert cli.main(["grade", str(config), "--yes"]) == 0


def test_export_says_rewrote_disposable_view(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    _run_pipeline(config)
    capsys.readouterr()
    assert cli.main(["export", str(config)]) == 0
    out = capsys.readouterr().out
    assert "export: rewrote export/" in out
    assert "(disposable view)" in out


def test_batch_line_printed_when_batch_providers_ran(
    tmp_path, offline_adapter, monkeypatch, capsys
):
    from itemeval.generate import _run as run_mod

    def fake_run(prep, **kwargs):
        return run_mod.GenerateResult(
            run_id="r",
            study=prep.config.study,
            conditions=[],
            rows_written=0,
            total_usd=0.0,
            manifest_path="m",
            batch=True,
            batch_providers=["anthropic"],
        )

    monkeypatch.setattr(run_mod, "run_generate", fake_run)
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "batch: enabled (anthropic) — provider-side jobs created" in out
    assert "resume with the same command" in out


def test_batch_fields_default_off_in_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["batch"] is False and doc["batch_providers"] == []
