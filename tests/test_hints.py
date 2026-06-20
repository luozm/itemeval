"""Hint framework: detectors, budget/priority, ITEMEVAL_HINTS, JSON parity."""

import io
import json

import pytest

from itemeval import cli
from itemeval._hints import (
    Hint,
    detect_cache_zero_reads,
    detect_empty_solutions,
    detect_openrouter_unpinned_cache,
    detect_truncated_completions,
    detect_unpriced_models,
    emit_hints,
)
from conftest import write_study_files


def _hint(code: str) -> Hint:
    return Hint(code=code, message=f"m-{code}", learn_more="Page#anchor")


# --- detectors ---


def test_detect_cache_zero_reads_fires_only_when_scheduled_and_real():
    kwargs = dict(scheduled=True, repeated_prefix_calls=6, cache_read_tokens=0, real_provider=True)
    h = detect_cache_zero_reads(**kwargs)
    assert h is not None and h.code == "cache-zero-reads" and "6 calls" in h.message
    assert detect_cache_zero_reads(**{**kwargs, "cache_read_tokens": 100}) is None
    assert detect_cache_zero_reads(**{**kwargs, "scheduled": False}) is None
    assert detect_cache_zero_reads(**{**kwargs, "repeated_prefix_calls": 0}) is None
    assert detect_cache_zero_reads(**{**kwargs, "real_provider": False}) is None


def test_detect_empty_solutions():
    h = detect_empty_solutions(21, 21, "skip", {"model_length": 21})
    assert h is not None and h.code == "empty-solutions"
    assert "21 solutions are empty" in h.message and "model_length×21" in h.message
    assert h.learn_more == "Error-Handling#empty-completions"
    assert detect_empty_solutions(0, 0, "skip", {}) is None


def test_detect_truncated_completions():
    h = detect_truncated_completions(21)
    assert h is not None and h.code == "truncated-completions"
    assert "21 completion(s) stopped at a length cap" in h.message
    assert "solvers.max_tokens" in h.message
    assert h.learn_more == "Error-Handling#truncation"
    assert detect_truncated_completions(0) is None


def test_detect_reroute_residue():
    from itemeval._hints import detect_reroute_residue

    h = detect_reroute_residue(2, 3, ["GMICloud", "Phala"])
    assert h is not None and h.code == "reroute-residue"
    assert "2 cell(s) still soft-failed after 3 reroute(s)" in h.message
    assert "GMICloud, Phala" in h.message
    assert h.learn_more == "Error-Handling#soft-failures-and-reroute"
    assert detect_reroute_residue(0, 3, []) is None


def test_detect_split_head_below_min_single_static_head():
    from itemeval._hints import detect_split_head_below_min

    h = detect_split_head_below_min(
        stage="generate",
        heads_below=1,
        heads_total=1,
        min_tokens=4096,
        model="anthropic/claude-haiku-4-5",
        head_tokens=900,
    )
    assert h is not None and h.code == "split-head-below-min"
    assert "split_prompt" in h.message and "~900 tokens" in h.message
    assert "silently do nothing" in h.message
    assert h.learn_more == "Cost-Savings#two-gotchas"


def test_detect_split_head_below_min_per_item_counts():
    from itemeval._hints import detect_split_head_below_min

    h = detect_split_head_below_min(
        stage="grade",
        heads_below=7,
        heads_total=40,
        min_tokens=4096,
        model="anthropic/claude-haiku-4-5",
    )
    assert h is not None and "split_rubric" in h.message
    assert "7/40 judge heads" in h.message and "4096" in h.message


def test_detect_split_head_below_min_none_when_all_clear():
    from itemeval._hints import detect_split_head_below_min

    assert (
        detect_split_head_below_min(
            stage="grade",
            heads_below=0,
            heads_total=40,
            min_tokens=4096,
            model="anthropic/claude-haiku-4-5",
        )
        is None
    )


def test_detect_anthropic_openrouter_no_split():
    from itemeval._hints import detect_anthropic_openrouter_no_split

    h = detect_anthropic_openrouter_no_split(
        stage="generate", models=["openrouter/anthropic/claude-haiku-4.5"]
    )
    assert h is not None and h.code == "anthropic-openrouter-no-split"
    assert "openrouter/anthropic/claude-haiku-4.5" in h.message
    assert "split_prompt" in h.message
    assert h.learn_more == "Cost-Savings#prompt-packaging"
    g = detect_anthropic_openrouter_no_split(
        stage="grade", models=["openrouter/anthropic/claude-haiku-4.5"]
    )
    assert g is not None and "split_rubric" in g.message
    assert detect_anthropic_openrouter_no_split(stage="generate", models=[]) is None


def test_detect_openrouter_unpinned_cache():
    h = detect_openrouter_unpinned_cache(["openrouter/anthropic/claude-haiku-4.5"])
    assert h is not None and h.code == "openrouter-unpinned-cache"
    assert "openrouter/anthropic/claude-haiku-4.5" in h.message
    assert "provider_routing" in h.message
    assert h.learn_more == "Cost-Savings#openrouter-or-direct"
    assert detect_openrouter_unpinned_cache([]) is None


def test_detect_unpriced_models():
    h = detect_unpriced_models(["x/y"])
    assert h is not None and h.code == "unpriced-models" and "x/y" in h.message
    assert detect_unpriced_models([]) is None


# --- rendering: budget of 2, priority = catalog order, env switch ---


def test_emit_hints_budget_and_priority():
    stream = io.StringIO()
    hints = [_hint("unpriced-models"), _hint("empty-solutions"), _hint("cache-zero-reads")]
    emit_hints(hints, stream=stream)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2  # budget of 2
    assert "m-cache-zero-reads" in lines[0]  # catalog order, not input order
    assert "m-empty-solutions" in lines[1]
    assert all(line.startswith("hint: ") and "learn more:" in line for line in lines)


def test_emit_hints_off_switch(monkeypatch):
    monkeypatch.setenv("ITEMEVAL_HINTS", "off")
    stream = io.StringIO()
    emit_hints([_hint("unpriced-models")], stream=stream)
    assert stream.getvalue() == ""


# --- CLI integration ---


@pytest.fixture()
def unpriced_study(tmp_path, offline_adapter):
    """Study with one model missing from the pricing table."""
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "unpriced.yaml"
    config.write_text(config_yaml.replace("mockllm/solver-b", "nopricing/model-b"))
    return config


def test_estimate_unpriced_hint_on_stderr_and_in_json(unpriced_study, capsys):
    assert cli.main(["estimate", str(unpriced_study)]) == 0
    captured = capsys.readouterr()
    assert "hint: " in captured.err and "nopricing/model-b" in captured.err
    assert "hint" not in captured.out  # hints are stderr commentary, not stdout facts
    assert cli.main(["estimate", str(unpriced_study), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    # cold-start estimate also carries estimate-is-ceiling; unpriced is present too
    assert "unpriced-models" in {h["code"] for h in doc["hints"]}


def test_hints_off_silences_text_but_never_json(unpriced_study, capsys, monkeypatch):
    monkeypatch.setenv("ITEMEVAL_HINTS", "off")
    assert cli.main(["estimate", str(unpriced_study)]) == 0
    assert "hint: " not in capsys.readouterr().err
    assert cli.main(["estimate", str(unpriced_study), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "unpriced-models" in {h["code"] for h in doc["hints"]}  # JSON never suppressed


def test_grade_empty_solutions_line_is_fact_only(tmp_path, offline_adapter, capsys, monkeypatch):
    """V7: the summary line carries the fact; the advice moved to the hint/doc."""
    import pandas as pd

    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    # blank out one solution to create an empty (no-error) completion
    from itemeval._config import load_config
    from itemeval.store._layout import StudyPaths

    paths = StudyPaths(load_config(config).study_dir)
    df = pd.read_parquet(paths.solutions)
    df.loc[df.index[0], "solution"] = ""
    df.loc[df.index[0], "stop_reason"] = "model_length"
    df.to_parquet(paths.solutions)
    capsys.readouterr()
    assert cli.main(["grade", str(config), "--yes"]) == 0
    captured = capsys.readouterr()
    line = next(ln for ln in captured.out.splitlines() if ln.startswith("empty solutions:"))
    assert "excluded from grading" in line and "on_empty=skip" in line
    assert "raise max_tokens" not in line  # advice no longer embedded
    assert "hint: " in captured.err and "Error-Handling#empty-completions" in captured.err


def test_grade_json_carries_empty_solutions_hint(tmp_path, offline_adapter, capsys):
    import pandas as pd

    config = write_study_files(tmp_path)
    assert cli.main(["generate", str(config), "--yes"]) == 0
    from itemeval._config import load_config
    from itemeval.store._layout import StudyPaths

    paths = StudyPaths(load_config(config).study_dir)
    df = pd.read_parquet(paths.solutions)
    df.loc[df.index[0], "solution"] = ""
    df.to_parquet(paths.solutions)
    capsys.readouterr()
    assert cli.main(["grade", str(config), "--yes", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any(h["code"] == "empty-solutions" for h in doc["hints"])


def test_gate_stop_json_carries_estimate_time_hints(tmp_path, offline_adapter, capsys):
    """W4 wiring: split-head-below-min surfaces on generate, even on a gate stop."""
    from conftest import TEST_CONFIG_YAML, write_study_files

    yaml_text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]",
        "  models: [anthropic/claude-haiku-4-5]\n  split_prompt: true",
    ).replace("  confirm_above_usd: 100", "  confirm_above_usd: 0")
    config = write_study_files(tmp_path, yaml_text)
    rc = cli.main(["generate", str(config), "--json"])  # gate stops: no API call
    assert rc == 3
    doc = json.loads(capsys.readouterr().out)
    assert any(h["code"] == "split-head-below-min" for h in doc["hints"])
