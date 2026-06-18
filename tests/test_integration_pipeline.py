"""M6 exit criterion: the full pipeline, driven only through the CLI."""

import json

import pandas as pd

from itemeval import cli
from conftest import write_study_files


def test_full_pipeline_cli_only(tmp_path, offline_adapter, capsys):
    config = str(write_study_files(tmp_path))
    study_dir = tmp_path / "studies" / "tstudy"

    # status (empty) -> generate -> grade -> export -> status (complete)
    assert cli.main(["status", config]) == 0
    capsys.readouterr()

    assert cli.main(["generate", config, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "rows written: 8" in out
    assert (study_dir / "solutions.parquet").is_file()
    # cost-lever legibility (Issue #3) + coarse ETA (W2) on the dev pre-flight
    assert "cost levers: batch off (dev policy)" in out
    assert "response-cache on" in out
    assert "at concurrency 2" in out and "default latency — rough" in out
    # all conditions ran in one shared log dir (no per-condition subdirs)
    gen_logs = study_dir / "logs" / "generate"
    assert gen_logs.is_dir() and not any(p.is_dir() for p in gen_logs.iterdir())

    # resumability: second generate run skips every condition
    assert cli.main(["generate", config, "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.count("skipped: complete") == 2
    assert "rows written: 0" in out

    assert cli.main(["grade", config, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "rows written: 8" in out and "parse_failures=0" in out
    assert (study_dir / "gradings.parquet").is_file()

    assert cli.main(["export", config]) == 0
    out = capsys.readouterr().out
    assert "internally reconciled (ledger vs row sums): yes" in out
    assert (study_dir / "export" / "gradings_long.parquet").is_file()
    assert (study_dir / "export" / "gradings_long.csv").is_file()
    assert (study_dir / "export" / "ledger.csv").is_file()

    assert cli.main(["status", config, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(c["completed"] == c["expected"] == 4 for c in report["generate"])
    assert report["grade"][0]["completed"] == report["grade"][0]["expected"] == 8
    assert report["grade"][0]["parse_failures"] == 0
    assert report["spend_generate_usd"] > 0 and report["spend_grade_usd"] > 0
    assert len(report["manifests"]) == 3  # 2 generate runs + 1 grade run

    # Export table is item-level and complete.
    df = pd.read_parquet(study_dir / "export" / "gradings_long.parquet")
    assert len(df) == 8
    assert df["score"].notna().all()
    assert df["reasoning"].notna().all()
