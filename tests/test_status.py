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
    grade = report.grade[0]
    assert grade.expected == 8 and grade.completed == 8
    assert grade.parse_failures == 0
    assert report.spend_generate_usd > 0
    assert report.spend_grade_usd > 0
    assert len(report.manifests) == 2
