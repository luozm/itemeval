import json

from itemeval._manifest import build_manifest, finalize_manifest, write_manifest


def test_build_manifest_contents(study):
    cfg, prep = study
    manifest = build_manifest(prep, "generate", "exp123", 1, ["c1"], 0.5)
    assert manifest.experiment_id == "exp123"
    assert manifest.attempt == 1
    assert manifest.stage == "generate"
    assert manifest.study == "tstudy"
    assert manifest.itemeval_version != "unknown"
    assert manifest.packages["inspect-ai"] != "unknown"
    assert manifest.config_sha256 == cfg.config_sha256
    assert manifest.config["facets"]["model_config"]  # YAML alias preserved
    ds = manifest.datasets[0]
    assert ds.id == "fake/ds" and ds.n_items == 3 and len(ds.items_hash) == 12
    assert [t.name for t in manifest.solver_templates] == ["minimal"]
    assert manifest.solver_templates[0].path == "prompts/solver/minimal.md"
    assert len(manifest.solver_templates[0].sha256) == 64
    assert manifest.graders["judge"]["model"] == "mockllm/judge"
    assert "models" not in manifest.sampling_requested
    assert manifest.sampling_requested["temperature"] == 0.3
    assert manifest.replications_requested == 2
    assert manifest.items_limit == 2  # dev policy
    assert [c.id for c in manifest.grid_generate] == [c.id for c in prep.grid.generate]
    assert manifest.grid_generate[0].payload["kind"] == "generate"
    assert manifest.conditions_run == ["c1"]
    assert manifest.estimate_usd == 0.5
    assert manifest.sampling_effective is None


def test_write_and_finalize_manifest(study):
    _, prep = study
    manifest = build_manifest(prep, "grade", "exp456", 2, [], None)
    path = write_manifest(manifest, prep.paths)
    assert path.name == "exp456.a2.json"  # filename is the invocation handle
    data = json.loads(path.read_text())
    assert data["stage"] == "grade"

    finalize_manifest(path, {"c1": {"temperature": 0.3}})
    data = json.loads(path.read_text())
    assert data["sampling_effective"] == {"c1": {"temperature": 0.3}}
    assert data["experiment_id"] == "exp456" and data["attempt"] == 2  # rest untouched
