import pytest
from conftest import write_study_files

import itemeval

PROMISED = [
    "BudgetExceededError",
    "ExperimentConfig",
    "Item",
    "ItemevalError",
    "__version__",
    "build_status",
    "estimate_study",
    "export_study",
    "harvest_study",
    "load_config",
    "prepare_study",
    "run_generate",
    "run_grade",
]


def test_public_api_is_exactly_the_promised_surface():
    assert sorted(itemeval.__all__) == PROMISED
    assert sorted(dir(itemeval)) == PROMISED


def test_lazy_exports_resolve_to_the_real_functions():
    from itemeval._prepare import prepare_study
    from itemeval._status import build_status
    from itemeval.budget._estimator import estimate_study
    from itemeval.generate._run import run_generate
    from itemeval.grade._run import run_grade
    from itemeval.store._export import export_study

    assert itemeval.prepare_study is prepare_study
    assert itemeval.estimate_study is estimate_study
    assert itemeval.run_generate is run_generate
    assert itemeval.run_grade is run_grade
    assert itemeval.export_study is export_study
    assert itemeval.build_status is build_status


def test_run_functions_default_to_live_progress_display():
    """display defaults to None, the sentinel that resolves to inspect's `rich`
    live progress (or INSPECT_DISPLAY); "none" would silence it."""
    import inspect

    for fn in (itemeval.run_generate, itemeval.run_grade):
        assert inspect.signature(fn).parameters["display"].default is None


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError, match="no attribute 'bogus'"):
        itemeval.bogus


def test_import_itemeval_does_not_pull_heavy_deps():
    import subprocess
    import sys

    code = (
        "import sys; import itemeval; "
        "heavy = [m for m in ('inspect_ai', 'pandas', 'pyarrow') if m in sys.modules]; "
        "print(','.join(heavy) or 'none')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "none"


def test_version_is_pep440ish():
    assert itemeval.__version__
    assert itemeval.__version__[0].isdigit()


def test_subpackages_export_nothing():
    import itemeval.adapters
    import itemeval.budget
    import itemeval.design
    import itemeval.generate
    import itemeval.grade
    import itemeval.store

    for pkg in (
        itemeval.adapters,
        itemeval.budget,
        itemeval.design,
        itemeval.generate,
        itemeval.grade,
        itemeval.store,
    ):
        public = [n for n in vars(pkg) if not n.startswith("_")]
        assert public == [], f"{pkg.__name__} leaks public names: {public}"


def test_full_pipeline_via_public_api_only(tmp_path, offline_adapter):
    """The CLI-equivalent flow, driven exclusively through `import itemeval`."""
    config_path = write_study_files(tmp_path)

    cfg = itemeval.load_config(config_path)
    prep = itemeval.prepare_study(cfg)

    est = itemeval.estimate_study(prep)
    assert est.total_usd > 0

    gen = itemeval.run_generate(prep)
    assert gen.rows_written == 8
    # auto-read: stored (tiny) solutions now shrink the judge input estimate
    assert itemeval.estimate_study(prep).grade.input_tokens < est.grade.input_tokens

    graded = itemeval.run_grade(prep)
    assert graded.rows_written == 8
    assert graded.parse_failures == 0

    exported = itemeval.export_study(cfg)
    assert exported.rows == 8
    assert exported.internally_reconciled

    report = itemeval.build_status(cfg, prep)
    assert all(c.completed == c.expected == 4 for c in report.generate)
    assert report.grade[0].completed == 8
