"""Local response-cache announcement (UX-compliance Step 4, Law 1)."""

import json

from itemeval import cli
from conftest import write_study_files


def test_fresh_run_has_no_local_cache_line(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "answered from local cache" not in out


def test_replayed_run_announces_local_cache(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    # --force re-runs everything; identical calls replay from the local cache
    assert cli.main(["generate", str(config), "--yes", "--force"]) == 0
    out = capsys.readouterr().out
    assert "8 calls answered from local cache ($0) — cache dir: " in out


def test_local_cache_fields_in_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes", "--json"]) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["local_cache_rows"] == 0 and fresh["local_cache_dir"] is None
    assert cli.main(["generate", str(config), "--yes", "--json", "--force"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["local_cache_rows"] == 8
    assert replay["local_cache_dir"]
    assert all(c["local_cache_rows"] == 4 for c in replay["conditions"])


def test_grade_replay_announces_local_cache(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert cli.main(["grade", str(config), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["grade", str(config), "--yes", "--force", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["local_cache_rows"] == 8
