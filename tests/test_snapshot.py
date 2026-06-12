"""Snapshots + STUDY_CARD.md (growth-ux 2.1 + 2.2)."""

import json

import pytest
import yaml

from itemeval import cli
from itemeval._config import load_config
from itemeval.store._export import export_study
from conftest import write_study_files


@pytest.fixture()
def completed_study(tmp_path, offline_adapter):
    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    assert cli.main(["grade", str(config), "--yes"]) == 0
    return config


def test_snapshot_dir_contents_complete(completed_study, capsys):
    assert cli.main(["export", str(completed_study), "--snapshot", "pub1"]) == 0
    out = capsys.readouterr().out
    assert "snapshot: pub1 written — 8 rows · 2 runs ·" in out
    cfg = load_config(completed_study)
    snap = cfg.study_dir / "export" / "snapshots" / "pub1"
    for name in (
        "gradings_long.parquet",
        "gradings_long.csv",
        "ledger.csv",
        "dataset_locks.json",
        "snapshot.json",
        "STUDY_CARD.md",
    ):
        assert (snap / name).is_file(), name
    meta = json.loads((snap / "snapshot.json").read_text())
    assert meta["name"] == "pub1" and meta["rows"] == 8
    assert len(meta["run_ids"]) == 2
    # one manifest copy per included run
    assert sorted(p.stem for p in (snap / "manifests").glob("*.json")) == meta["run_ids"]


def test_existing_snapshot_name_refused_exit_2(completed_study, capsys):
    assert cli.main(["export", str(completed_study), "--snapshot", "pub1"]) == 0
    assert cli.main(["export", str(completed_study), "--snapshot", "pub1"]) == 2
    assert "snapshot 'pub1' exists — choose a new name" in capsys.readouterr().err
    assert cli.main(["export", str(completed_study), "--snapshot", "pub2"]) == 0


def test_invalid_snapshot_name_exit_2(completed_study, capsys):
    assert cli.main(["export", str(completed_study), "--snapshot", "Pub 1!"]) == 2
    assert "invalid snapshot name" in capsys.readouterr().err


def test_snapshot_immutable_across_later_runs(completed_study):
    cfg = load_config(completed_study)
    export_study(cfg, snapshot="pub1")
    snap = cfg.study_dir / "export" / "snapshots" / "pub1"
    before = {p.name: p.read_bytes() for p in snap.iterdir() if p.is_file()}
    # later generate + export must not touch the frozen copy
    assert cli.main(["generate", str(completed_study), "--yes", "--force"]) == 0
    assert cli.main(["export", str(completed_study)]) == 0
    after = {p.name: p.read_bytes() for p in snap.iterdir() if p.is_file()}
    assert before == after


def test_python_kwarg_returns_path_and_status_lists(completed_study, capsys):
    cfg = load_config(completed_study)
    result = export_study(cfg, snapshot="pub1")
    assert result.snapshot_path == "export/snapshots/pub1"
    assert result.snapshot.card_path.endswith("STUDY_CARD.md")
    assert cli.main(["status", str(completed_study)]) == 0
    assert "snapshots: pub1 (" in capsys.readouterr().out
    assert cli.main(["status", str(completed_study), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["snapshots"][0]["name"] == "pub1" and report["snapshots"][0]["rows"] == 8


def test_study_card_front_matter_and_body(completed_study):
    cfg = load_config(completed_study)
    result = export_study(cfg, snapshot="pub1")
    card = (cfg.study_dir / result.snapshot.card_path).read_text()
    assert card.startswith("---\n")
    front = yaml.safe_load(card.split("---")[1])
    assert front["itemeval_study_card"] == 1
    assert front["study"] == "tstudy" and front["snapshot"] == "pub1"
    assert front["rows"] == 8 and front["replications"] == 2
    assert front["datasets"][0]["id"] == "fake/ds"
    assert front["graders"] == [{"name": "judge", "model": "mockllm/judge"}]
    # body: grid table, every run id, the config, no secrets
    for run_id in result.snapshot.run_ids:
        assert run_id in card
    assert "## Design" in card and "## Reproduce" in card
    assert "mockllm/solver-a" in card
    assert "```yaml" in card and "study: tstudy" in card
    assert "sk-" not in card


def test_study_card_deterministic_given_fixed_stores(completed_study):
    cfg = load_config(completed_study)
    r1 = export_study(cfg, snapshot="a1")
    r2 = export_study(cfg, snapshot="a2")

    def normalized(result):
        text = (cfg.study_dir / result.snapshot.card_path).read_text()
        return (
            text.replace(result.snapshot.name, "NAME")
            .replace(result.snapshot.created_at, "TS")
            .replace(result.snapshot.created_at[:10], "DATE")
        )

    assert normalized(r1) == normalized(r2)
