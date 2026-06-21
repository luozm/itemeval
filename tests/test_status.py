from itemeval._status import build_status
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade


def test_status_before_any_run(study):
    cfg, prep = study
    report = build_status(cfg, prep)
    assert report.study == "tstudy"
    assert report.policy == "dev"
    assert report.n_items_total == 3
    assert report.n_items_effective == 2
    assert len(report.generate) == 2
    assert all(c.expected == 4 and c.completed == 0 for c in report.generate)
    assert len(report.grade) == 1
    assert report.grade[0].expected == 0  # no gradable solutions yet
    assert report.spend_generate_usd == 0.0
    assert report.manifests == []


def test_status_after_full_pipeline(study):
    cfg, prep = study
    run_generate(prep)
    run_grade(prep)
    report = build_status(cfg, prep)
    assert all(c.completed == 4 and c.errors == 0 for c in report.generate)
    assert all(c.incomplete == 0 for c in report.generate)
    grade = report.grade[0]
    assert grade.expected == 8 and grade.completed == 8
    assert grade.parse_failures == 0
    assert report.spend_generate_usd > 0
    assert report.spend_grade_usd > 0
    assert len(report.manifests) == 2


def _seed_one_empty_condition(prep):
    """Seed one gen condition with item 1 blank (no error), item 2 gradable."""
    from conftest import force_write_solutions

    cond = prep.grid.generate[0]
    rows = []
    for it in prep.items_effective:
        for epoch in (1, 2):
            empty = it.id == "1"
            rows.append(
                {
                    "study": prep.config.study,
                    "experiment_id": "r",
                    "attempt": 1,
                    "condition_id": cond.id,
                    "condition_slug": cond.slug,
                    "item_id": it.id,
                    "dataset_id": "d",
                    "dataset_revision": "v",
                    "epoch": epoch,
                    "model": cond.model,
                    "prompt_name": cond.prompt_name,
                    "prompt_hash": "h",
                    "model_config_name": cond.model_config_name,
                    "solution": None if empty else "ANSWER: 4",
                    "stop_reason": "max_tokens" if empty else "stop",
                    "error": None,
                    "log_file": "lf",
                    "created_at": "t0",
                }
            )
    force_write_solutions(prep, rows)
    return cond


def test_status_reports_incomplete_empties(study):
    cfg, prep = study  # default policy: skip
    cond = _seed_one_empty_condition(prep)
    report = build_status(cfg, prep)
    seeded = next(c for c in report.generate if c.condition_id == cond.id)
    # skip policy: empties still count as produced (done), but are surfaced
    assert seeded.completed == 4 and seeded.incomplete == 2
    # gradable excludes empties -> grade expects only item 2's 2 solutions
    assert report.grade[0].expected == 2


def _seed_truncation_mix(prep):
    """Seed one gen condition (2 items x 2 epochs) with a mix of stop reasons:
    a non-empty max_tokens (truncated), a non-empty model_length (truncated), an
    empty max_tokens (empty/incomplete — NOT truncated), and a clean stop."""
    from conftest import force_write_solutions

    cond = prep.grid.generate[0]
    cases = {
        ("1", 1): ("Long partial answer", "max_tokens"),  # truncated
        ("1", 2): (None, "max_tokens"),  # empty (incomplete), not truncated
        ("2", 1): ("ANSWER: 4", "stop"),  # clean
        ("2", 2): ("Another cut-off answer", "model_length"),  # truncated
    }
    rows = []
    for it in prep.items_effective:
        for epoch in (1, 2):
            solution, stop = cases[(it.id, epoch)]
            rows.append(
                {
                    "study": prep.config.study,
                    "experiment_id": "r",
                    "attempt": 1,
                    "condition_id": cond.id,
                    "condition_slug": cond.slug,
                    "item_id": it.id,
                    "dataset_id": "d",
                    "dataset_revision": "v",
                    "epoch": epoch,
                    "model": cond.model,
                    "prompt_name": cond.prompt_name,
                    "prompt_hash": "h",
                    "model_config_name": cond.model_config_name,
                    "solution": solution,
                    "stop_reason": stop,
                    "error": None,
                    "log_file": "lf",
                    "created_at": "t0",
                }
            )
    force_write_solutions(prep, rows)
    return cond


def test_status_reports_truncated_disjoint_from_empty(study):
    cfg, prep = study  # items 1 and 2 are effective under dev policy
    cond = _seed_truncation_mix(prep)
    report = build_status(cfg, prep)
    seeded = next(c for c in report.generate if c.condition_id == cond.id)
    # two non-empty length-cap stops -> truncated; the empty max_tokens -> incomplete
    assert seeded.truncated == 2
    assert seeded.incomplete == 1
    # truncated is a sub-count of completed, never a reclassification (skip policy:
    # all 4 no-error rows count as produced/done).
    assert seeded.completed == 4
    import yaml

    from itemeval import ExperimentConfig
    from itemeval._prepare import prepare_study

    cfg, _ = study
    data = yaml.safe_load(cfg.config_path.read_text())
    data["solvers"]["on_empty"] = "rerun"
    cfg2 = ExperimentConfig.model_validate(data)
    cfg2._config_dir = cfg.config_dir
    cfg2._work_dir = cfg.work_dir
    prep2 = prepare_study(cfg2)

    cond = _seed_one_empty_condition(prep2)
    report = build_status(cfg2, prep2)
    seeded = next(c for c in report.generate if c.condition_id == cond.id)
    # rerun policy: empties are not done -> they'll be re-attempted
    assert seeded.completed == 2 and seeded.incomplete == 2
