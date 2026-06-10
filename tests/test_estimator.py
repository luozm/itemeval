import yaml

from itemeval.budget._estimator import (
    DEFAULT_OUTPUT_TOKENS_GENERATE,
    DEFAULT_OUTPUT_TOKENS_JUDGE,
    estimate_study,
)


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


def test_estimate_uncapped_warns(study, tmp_path):
    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, prep = study
    data = yaml.safe_load(cfg.config_path.read_text())
    del data["solvers"]["max_tokens"]
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._base_dir = cfg.base_dir
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


def test_judge_default_output_tokens(study):
    import yaml as _yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, _ = study
    data = _yaml.safe_load(cfg.config_path.read_text())
    data["graders"]["judge"].pop("max_tokens")
    data["graders"]["judge"]["max_tokens"] = None
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._base_dir = cfg.base_dir
    prep2 = prepare_study(cfg2)
    est = estimate_study(prep2)
    assert est.grade.output_tokens == est.grade.calls * DEFAULT_OUTPUT_TOKENS_JUDGE
