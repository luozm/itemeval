"""recovery-run-identity: semantic digest, experiment_id/attempt, detection, index."""

import pandas as pd
import pytest

from itemeval._config import load_config
from itemeval._errors import StoreError
from itemeval._identity import (
    _count_attempts,
    experiment_id,
    invocation_handle,
    normalized_config_digest,
    resolve_identity,
)
from conftest import TEST_CONFIG_YAML, write_study_files


def _load(tmp_path, yaml_text):
    return load_config(write_study_files(tmp_path, yaml_text))


# --- W1: semantic config digest ------------------------------------------------


def test_digest_invariant_to_comments_whitespace_and_execution_knobs(tmp_path):
    base = _load(tmp_path / "a", TEST_CONFIG_YAML)
    # Same scientific design; only comments + excluded execution/path/cost knobs differ.
    twiddled = TEST_CONFIG_YAML.replace(
        "output_dir: studies", "output_dir: somewhere_else  # a comment"
    ).replace("confirm_above_usd: 100", "confirm_above_usd: 5\n  max_usd: 9.0")
    twiddled = twiddled.replace("solvers:\n", "solvers:\n  cache_prompt: off\n")
    twiddled = twiddled.replace("cache: dev\n", "")  # no-op; just being explicit
    other = _load(tmp_path / "b", "cache: false\n" + twiddled)
    assert base.config_sha256 == other.config_sha256


def test_digest_changes_on_a_real_design_edit(tmp_path):
    base = _load(tmp_path / "a", TEST_CONFIG_YAML)
    edited = _load(
        tmp_path / "b",
        TEST_CONFIG_YAML.replace("mockllm/solver-a", "mockllm/solver-x"),
    )
    assert base.config_sha256 != edited.config_sha256
    # and a digest is a full sha256 hex
    assert len(base.config_sha256) == 64


def test_normalized_digest_drops_provider_routing(tmp_path):
    base = _load(tmp_path / "a", TEST_CONFIG_YAML)
    routed = TEST_CONFIG_YAML.replace(
        "  temperature: 0.3",
        "  temperature: 0.3\n  provider_routing: {order: [anthropic]}",
    )
    assert normalized_config_digest(_load(tmp_path / "b", routed)) == base.config_sha256


def test_experiment_id_stable_and_stage_scoped(tmp_path):
    a = _load(tmp_path / "a", TEST_CONFIG_YAML)
    b = _load(tmp_path / "b", TEST_CONFIG_YAML)
    assert experiment_id(a, "generate") == experiment_id(b, "generate")  # deterministic
    assert experiment_id(a, "generate") != experiment_id(a, "grade")  # stage-scoped
    assert len(experiment_id(a, "generate")) == 12


# --- W2: recovery-vs-new detection + attempt counter ---------------------------


def test_attempt_counter_ignores_experiments_subdir(tmp_path):
    mdir = tmp_path / "manifests"
    (mdir / "experiments").mkdir(parents=True)
    eid = "a1b2c3d4e5f6"
    (mdir / f"{eid}.a1.json").write_text("{}")
    # A W3 index file in the subdir must NOT be counted as an attempt (non-recursive).
    (mdir / "experiments" / f"generate.{eid}.json").write_text("{}")
    assert _count_attempts(mdir, eid) == 1


def test_resolve_identity_new_then_recovery(study):
    _, prep = study
    eid = experiment_id(prep.config, "generate")
    first = resolve_identity(prep.config, prep.paths, "generate")
    assert first == (eid, 1, "new")
    # Seed a manifest for this experiment, then re-resolve.
    prep.paths.manifests_dir.mkdir(parents=True, exist_ok=True)
    (prep.paths.manifests_dir / f"{invocation_handle(eid, 1)}.json").write_text("{}")
    second = resolve_identity(prep.config, prep.paths, "generate")
    assert second.attempt == 2 and second.run_kind == "recovery"
    # --new-run salts a fresh id -> always new, attempt 1.
    fresh = resolve_identity(prep.config, prep.paths, "generate", new_run=True)
    assert fresh.experiment_id != eid and fresh.run_kind == "new" and fresh.attempt == 1


# --- migration guard (DEVELOPMENT.md schema-evolution gate) ---------------------


def test_old_schema_store_raises_migration_briefing(study):
    _, prep = study
    prep.paths.solutions.parent.mkdir(parents=True, exist_ok=True)
    # An old-schema store: has run_id, lacks experiment_id.
    pd.DataFrame(
        [{"study": "t", "run_id": "r", "condition_id": "c", "item_id": "1", "epoch": 1}]
    ).to_parquet(prep.paths.solutions)
    from itemeval.store._solutions import read_solutions

    with pytest.raises(StoreError, match="experiment_id"):
        read_solutions(prep.paths)


# --- W1+W3 end-to-end: convergence, attempt rollup -----------------------------


def test_recovery_converges_and_indexes_attempts(study):
    from itemeval._experiments import read_experiments
    from itemeval.generate._run import run_generate
    from itemeval.store._solutions import read_solutions

    _, prep = study
    r1 = run_generate(prep, display="none")
    assert r1.run_kind == "new" and r1.attempt == 1
    # A second run of the unchanged config recovers the SAME experiment (converges).
    r2 = run_generate(prep, force=True, display="none")
    assert r2.experiment_id == r1.experiment_id
    assert r2.attempt == 2 and r2.run_kind == "recovery"

    sol = read_solutions(prep.paths)
    # Data converged: one experiment_id; the force re-run overwrote rows at attempt 2.
    assert set(sol["experiment_id"]) == {r1.experiment_id}
    assert set(sol["attempt"].astype(int)) == {2}

    # The per-experiment index rolls up both attempts; current points at the latest.
    idxs = [e for e in read_experiments(prep.paths) if e.stage == "generate"]
    assert len(idxs) == 1
    assert len(idxs[0].attempts) == 2 and idxs[0].current_attempt == 2
