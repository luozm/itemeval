"""Expected (calibrated) cost projection alongside the ceiling (expected-cost).

The expected pass swaps each worst-case token assumption for an observed mean
read from the stores; it is informational only — the ceiling figures `usd` /
`remaining_usd` (the gate inputs) must never move. Offline, no API calls.
"""

import json

import pandas as pd
import pytest

from itemeval import cli
from itemeval.budget._estimator import K_CALIBRATION_SAMPLES, estimate_study
from conftest import write_study_files


def _solutions(model: str, n: int, *, output_tokens: int, solution: str) -> pd.DataFrame:
    """n observed solution rows for `model`, under a condition outside the grid
    (so nothing reads as complete — calibration keys on `model`, not condition)."""
    return pd.DataFrame(
        {
            "condition_id": ["seed-gen"] * n,
            "item_id": [f"seed-{i}" for i in range(n)],
            "epoch": [1] * n,
            "model": [model] * n,
            "solution": [solution] * n,
            "output_tokens": [output_tokens] * n,
            "error": [None] * n,
        }
    )


def _gradings(grader_model: str, n: int, *, output_tokens: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "grade_condition_id": ["seed-grade"] * n,
            "gen_condition_id": ["seed-gen"] * n,
            "item_id": [f"seed-{i}" for i in range(n)],
            "epoch": [1] * n,
            "grader_model": [grader_model] * n,
            "output_tokens": [output_tokens] * n,
            "error": [None] * n,
        }
    )


def test_cold_start_expected_equals_ceiling(study):
    """No observations: expected == ceiling, every model uncalibrated."""
    _, prep = study
    est = estimate_study(prep, solutions_df=pd.DataFrame(), gradings_df=pd.DataFrame())
    for st in (est.generate, est.grade):
        assert st.expected_usd == pytest.approx(st.usd)
        assert st.expected_remaining_usd == pytest.approx(st.remaining_usd)
        assert st.calibration.observed_rows == 0
        assert st.calibration.calibrated_models == 0
        assert st.calibration.uncalibrated_models >= 1
        assert st.calibration.mean_output_tokens is None
    # 2 distinct generate models, 1 judge model — all at the ceiling.
    assert est.generate.calibration.uncalibrated_models == 2
    assert est.grade.calibration.uncalibrated_models == 1


def test_calibrated_expected_below_ceiling(study):
    """Short observed solutions + low output tokens pull expected under the
    ceiling on both stages, while the gate figures stay put."""
    _, prep = study
    cold = estimate_study(prep, solutions_df=pd.DataFrame(), gradings_df=pd.DataFrame())
    # solver-a observed at well under the 256-token / 1024-char ceiling.
    sols = _solutions(
        "mockllm/solver-a", K_CALIBRATION_SAMPLES, output_tokens=40, solution="x" * 80
    )
    grds = _gradings("mockllm/judge", K_CALIBRATION_SAMPLES, output_tokens=30)
    est = estimate_study(prep, solutions_df=sols, gradings_df=grds)

    # Ceiling (gate input) is byte-for-byte unchanged by the expected pass.
    assert est.generate.usd == pytest.approx(cold.generate.usd)
    assert est.generate.remaining_usd == pytest.approx(cold.generate.remaining_usd)
    assert est.grade.usd == pytest.approx(cold.grade.usd)
    assert est.grade.remaining_usd == pytest.approx(cold.grade.remaining_usd)

    # Expected sits below the ceiling on both stages.
    assert est.generate.expected_usd < est.generate.usd
    assert est.grade.expected_usd < est.grade.usd
    # Nothing is complete, so expected-remaining tracks expected-full.
    assert est.generate.expected_remaining_usd == pytest.approx(est.generate.expected_usd)


def test_calibration_tiers_own_and_pooled(study):
    """solver-a has its own >=K samples (own); solver-b has none but borrows the
    pooled mean (the stage is non-empty); the judge calibrates from its own."""
    _, prep = study
    sols = _solutions(
        "mockllm/solver-a", K_CALIBRATION_SAMPLES, output_tokens=40, solution="x" * 80
    )
    grds = _gradings("mockllm/judge", K_CALIBRATION_SAMPLES, output_tokens=30)
    est = estimate_study(prep, solutions_df=sols, gradings_df=grds)

    gcal = est.generate.calibration
    assert gcal.calibrated_models == 1  # solver-a, own mean
    assert gcal.pooled_models == 1  # solver-b borrows the pooled mean
    assert gcal.uncalibrated_models == 0
    assert gcal.observed_rows == K_CALIBRATION_SAMPLES
    assert gcal.mean_output_tokens == pytest.approx(40.0)
    assert gcal.mean_solution_chars is None  # solution length is a grade-side fact

    ccal = est.grade.calibration
    assert ccal.calibrated_models == 1  # judge, own mean
    assert ccal.mean_output_tokens == pytest.approx(30.0)
    # the grade input stub is sized from the observed solution length (80 chars)
    assert ccal.mean_solution_chars == pytest.approx(80.0)


def test_below_k_borrows_pooled_not_own(study):
    """A model with fewer than K samples does not use its own mean."""
    _, prep = study
    sols = _solutions(
        "mockllm/solver-a", K_CALIBRATION_SAMPLES - 1, output_tokens=40, solution="x" * 80
    )
    est = estimate_study(prep, solutions_df=sols, gradings_df=pd.DataFrame())
    gcal = est.generate.calibration
    assert gcal.calibrated_models == 0  # below K -> not "own"
    assert gcal.pooled_models == 2  # both models fall back to the pooled mean


# --- CLI surface: rendering, --json parity, gate-stop parity ---


def test_cli_estimate_json_has_expected_and_calibration(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    for stage in ("generate", "grade"):
        assert "expected_usd" in doc[stage]
        assert "expected_remaining_usd" in doc[stage]
        assert "uncalibrated_models" in doc[stage]["calibration"]


def test_cli_estimate_always_states_ceiling_and_renders_expected_after_pilot(
    tmp_path, offline_adapter, capsys
):
    config = write_study_files(tmp_path)
    # cold start: ceiling clause is always present; no expected line yet.
    assert cli.main(["estimate", str(config)]) == 0
    cold = capsys.readouterr()
    assert "ceiling: output at max_tokens" in cold.out
    assert "expected ~$" not in cold.out
    assert "run --policy dev to calibrate" in cold.err  # the estimate-is-ceiling hint

    # a free mock pilot populates real output_tokens -> the expected line appears.
    assert cli.main(["generate", str(config), "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["estimate", str(config)]) == 0
    out = capsys.readouterr().out
    assert "expected ~$" in out
    assert "observed generations" in out


def test_gate_stop_doc_carries_expected(tmp_path, offline_adapter, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    cfg_yaml = (
        write_study_files(tmp_path)
        .read_text()
        .replace("confirm_above_usd: 100", "confirm_above_usd: 0.0")
    )
    config = tmp_path / "gated.yaml"
    config.write_text(cfg_yaml)
    assert cli.main(["generate", str(config), "--json"]) == 3  # gate stop, no --yes
    doc = json.loads(capsys.readouterr().out)
    assert "expected_estimate_usd" in doc
    assert doc["expected_estimate_usd"] == pytest.approx(doc["estimate_usd"])  # cold start
    # the ceiling hint rides the stop document (pre-spend advice at a gate stop)
    assert "estimate-is-ceiling" in {h["code"] for h in doc["hints"]}
