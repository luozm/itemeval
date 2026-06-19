import yaml

from itemeval.budget._estimator import (
    DEFAULT_OUTPUT_TOKENS_GENERATE,
    DEFAULT_OUTPUT_TOKENS_JUDGE,
    estimate_study,
)


def _prepared(tmp_path, yaml_text, prompt=None):
    from conftest import write_study_files
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    config = write_study_files(tmp_path, yaml_text)
    if prompt is not None:
        (tmp_path / "prompts" / "solver" / "minimal.md").write_text(prompt)
    cfg = load_config(config)
    return cfg, prepare_study(cfg)


def _split_yaml(model="anthropic/claude-haiku-4-5", split_prompt=True, split_rubric=True):
    from conftest import TEST_CONFIG_YAML

    text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]",
        f"  models: [{model}]" + ("\n  split_prompt: true" if split_prompt else ""),
    )
    if split_rubric:
        text = text.replace(
            "    model: mockllm/judge",
            "    model: anthropic/claude-haiku-4-5\n    split_rubric: true",
        )
    return text


def test_estimate_demo_arithmetic(study):
    _, prep = study
    est = estimate_study(prep)
    # 2 gen conditions x (2 dev items x 2 reps) calls
    assert est.generate.calls == 2 * 4
    assert all(c.calls == 4 for c in est.generate.conditions)
    # configured max_tokens=256 caps output per call
    assert est.generate.output_tokens == est.generate.calls * 256
    assert est.generate.usd > 0  # mockllm/* priced via seed
    assert not est.warnings  # max_tokens set -> no uncapped warning
    # judge: one call per (gen condition x item x epoch)
    assert est.grade.calls == 2 * 2 * 2
    assert est.grade.output_tokens == est.grade.calls * 256
    assert est.total_usd == est.generate.usd + est.grade.usd
    assert est.generate.unpriced_models == []
    # Provenance travels with the estimate (the demo uses the bundled seed).
    assert est.pricing.source == "seed" and est.pricing.refreshed is False


def test_auto_refresh_marks_provenance(study, monkeypatch):
    import io as _io
    import json as _json
    import urllib.request as _ur

    from itemeval._prepare import prepare_study

    cfg, _ = study
    cfg.budget.pricing_max_age_days = 0  # treat any table as stale -> refresh
    payload = {
        "data": [{"id": "x/y", "pricing": {"prompt": "0.0000005", "completion": "0.000001"}}]
    }
    monkeypatch.setattr(
        _ur, "urlopen", lambda url, timeout: _io.BytesIO(_json.dumps(payload).encode())
    )
    prep = prepare_study(cfg)
    assert prep.pricing_refreshed is True
    assert estimate_study(prep).pricing.refreshed is True


def test_estimate_uncapped_warns(study, tmp_path):
    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, prep = study
    data = yaml.safe_load(cfg.config_path.read_text())
    del data["solvers"]["max_tokens"]
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    prep2 = prepare_study(cfg2)
    est = estimate_study(prep2)
    assert any("uncapped-generation" in w for w in est.warnings)
    assert est.generate.output_tokens == est.generate.calls * DEFAULT_OUTPUT_TOKENS_GENERATE


def test_estimate_unpriced_model_flagged(study):
    from itemeval.budget._pricing import PricingTable

    _, prep = study
    prep.pricing = PricingTable(updated_at="t", source="file", models={})
    est = estimate_study(prep)
    assert est.generate.usd == 0.0
    assert "mockllm/solver-a" in est.generate.unpriced_models
    assert all(not c.priced for c in est.generate.conditions)


def test_estimate_uses_stored_solutions_for_judge_input(study):
    import pandas as pd

    _, prep = study
    est_placeholder = estimate_study(prep, None)
    gen_cond = prep.grid.generate[0]
    rows = []
    for it in prep.items_effective:
        for epoch in (1, 2):
            rows.append(
                {
                    "condition_id": gen_cond.id,
                    "item_id": it.id,
                    "epoch": epoch,
                    "solution": "tiny",
                }
            )
    est_stored = estimate_study(prep, pd.DataFrame(rows))
    # Real (tiny) solutions shrink the judge input estimate vs 4*max_tokens placeholders.
    assert est_stored.grade.input_tokens < est_placeholder.grade.input_tokens


def test_grade_estimate_scopes_remaining_to_current_grid(study):
    """The grade *remaining* projection must count only solutions whose gen-
    condition is in the current grid — like the ceiling already does. Otherwise
    an orphaned old roster in the store inflates `remaining_usd` past the full-
    grid ceiling `usd` (a logically impossible, ungate-able figure)."""
    from itemeval.generate._run import run_generate
    from itemeval.store._solutions import upsert_solutions

    _, prep = study
    run_generate(prep)  # current grid: 2 gen conds x 2 items x 2 epochs
    grid_gen_ids = {c.id for c in prep.grid.generate}
    upsert_solutions(
        prep.paths,
        [
            {
                "study": prep.config.study,
                "run_id": "old",
                "condition_id": "orphan-roster-cond",  # not in the current grid
                "condition_slug": "orphan",
                "item_id": it.id,
                "dataset_id": "d",
                "dataset_revision": "v",
                "epoch": epoch,
                "model": "mockllm/old-model",
                "prompt_name": "minimal",
                "prompt_hash": "h",
                "model_config_name": "default",
                "solution": "ANSWER: 4",
                "stop_reason": "stop",
                "error": None,
                "log_file": "lf",
                "created_at": "t0",
            }
            for it in prep.items_effective
            for epoch in (1, 2)
        ],
    )
    assert "orphan-roster-cond" not in grid_gen_ids

    est = estimate_study(prep)
    # Remaining can never exceed the full-grid ceiling (pre-fix the orphans push
    # it above), and the grade scope stays the current grid: 2x2x2 judge calls.
    assert est.grade.remaining_usd <= est.grade.usd
    assert est.grade.calls == 2 * 2 * 2


def test_judge_default_output_tokens(study):
    import yaml as _yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, _ = study
    data = _yaml.safe_load(cfg.config_path.read_text())
    data["graders"]["judge"].pop("max_tokens")
    data["graders"]["judge"]["max_tokens"] = None
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    prep2 = prepare_study(cfg2)
    est = estimate_study(prep2)
    assert est.grade.output_tokens == est.grade.calls * DEFAULT_OUTPUT_TOKENS_JUDGE


# --- split-head-below-min (W4): estimate-time detection, per stage ---


def test_split_head_below_min_fires_per_stage(tmp_path, offline_adapter):
    _, prep = _prepared(tmp_path, _split_yaml())
    est = estimate_study(prep)
    gen_hint = next(h for h in est.generate.hints if h.code == "split-head-below-min")
    assert "split_prompt" in gen_hint.message and "silently do nothing" in gen_hint.message
    grade_hint = next(h for h in est.grade.hints if h.code == "split-head-below-min")
    # per-item judge heads: 2 dev items, both far below the 4096 minimum
    assert "2/2 judge heads" in grade_hint.message and "split_rubric" in grade_hint.message
    assert {h.code for h in est.hints} >= {"split-head-below-min"}


def test_split_head_no_hint_when_split_off(tmp_path, offline_adapter):
    _, prep = _prepared(tmp_path, _split_yaml(split_prompt=False, split_rubric=False))
    est = estimate_study(prep)
    assert not any(h.code == "split-head-below-min" for h in est.hints)


def test_split_head_no_hint_for_provider_without_known_minimum(tmp_path, offline_adapter):
    # grok documents no minimum; mock judge has no caching entry — never guess
    _, prep = _prepared(tmp_path, _split_yaml(model="grok/grok-4", split_rubric=False))
    est = estimate_study(prep)
    assert not any(h.code == "split-head-below-min" for h in est.hints)


def test_split_head_boundary_at_minimum(tmp_path, offline_adapter):
    # static head of exactly 4096 estimated tokens (chars/4): not below -> no hint
    yaml_text = _split_yaml(split_rubric=False)
    _, prep = _prepared(tmp_path, yaml_text, prompt=("y" * 4 * 4096) + "{input}")
    est = estimate_study(prep)
    assert not any(h.code == "split-head-below-min" for h in est.generate.hints)
    # one estimated token below the minimum -> fires
    _, prep = _prepared(tmp_path, yaml_text, prompt=("y" * (4 * 4096 - 4)) + "{input}")
    est = estimate_study(prep)
    assert any(h.code == "split-head-below-min" for h in est.generate.hints)


def test_split_head_openrouter_anthropic_obeys_anthropic_minimum(tmp_path, offline_adapter):
    _, prep = _prepared(
        tmp_path,
        _split_yaml(model="openrouter/anthropic/claude-haiku-4.5", split_rubric=False),
    )
    est = estimate_study(prep)
    hint = next(h for h in est.generate.hints if h.code == "split-head-below-min")
    assert "4096" in hint.message
