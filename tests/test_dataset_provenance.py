"""Dataset provenance announcements (UX-compliance Step 1, Law 1)."""

import json

from itemeval import cli
from itemeval._config import load_config
from itemeval.adapters._base import load_items
from itemeval.adapters._hf import _dataset_cache_dir, _dir_size_bytes
from conftest import FAKE_REVISION, write_study_files


def _load(tmp_path, offline_adapter):
    cfg = load_config(write_study_files(tmp_path))
    from itemeval.store._layout import StudyPaths

    paths = StudyPaths(cfg.study_dir)
    paths.ensure()
    return cfg, paths


def test_revision_precedence_branches(tmp_path, offline_adapter):
    cfg, paths = _load(tmp_path, offline_adapter)
    # first run: nothing pinned -> adapter resolves, lock written
    [ds] = load_items(cfg, paths.dataset_locks)
    assert ds.revision_source == "resolved" and ds.pinned_now is True
    # second run: lock wins, nothing rewritten
    [ds] = load_items(cfg, paths.dataset_locks)
    assert ds.revision_source == "lock" and ds.pinned_now is False
    # explicit config revision wins over the lock; lock updates once
    cfg.benchmark.datasets[0].revision = "deadbeef" + FAKE_REVISION[8:]
    offline_adapter.revision = cfg.benchmark.datasets[0].revision
    [ds] = load_items(cfg, paths.dataset_locks)
    assert ds.revision_source == "config" and ds.pinned_now is True
    [ds] = load_items(cfg, paths.dataset_locks)
    assert ds.revision_source == "config" and ds.pinned_now is False


def test_cli_dataset_line_first_run_then_reused(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config)]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("dataset: ")]
    assert len(lines) == 1  # one line per dataset
    assert "fake/ds (split train) @ fakerev" in lines[0]
    assert "revision pinned in dataset_locks.json" in lines[0]
    # re-run: pinned reuse, no pin clause
    assert cli.main(["estimate", str(config)]) == 0
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("dataset: ")]
    assert "reused from HF cache (pinned)" in line
    assert "revision pinned in dataset_locks.json" not in line


def test_dataset_provenance_json_parity(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config), "--json"]) == 0
    est = json.loads(capsys.readouterr().out)
    [d] = est["datasets"]
    assert d["id"] == "fake/ds" and d["revision_source"] == "resolved"
    assert d["pinned_now"] is True and d["cache"] == "reused"
    assert cli.main(["status", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    [d] = report["datasets"]
    assert d["revision_source"] == "lock" and d["pinned_now"] is False
    assert cli.main(["generate", str(config), "--yes", "--json"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["datasets"][0]["id"] == "fake/ds"


def test_cache_detection_against_tmp_dir(tmp_path):
    root = tmp_path / "hf_cache"
    repo = _dataset_cache_dir(root, "org/name")
    assert repo == root / "org___name"
    assert not (repo.is_dir() and any(repo.iterdir()))  # empty cache -> download
    repo.mkdir(parents=True)
    (repo / "data.arrow").write_bytes(b"x" * 128)
    assert repo.is_dir() and any(repo.iterdir())  # materialized -> reuse
    assert _dir_size_bytes(repo) == 128
