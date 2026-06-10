import itemeval


def test_public_api_is_exactly_the_promised_surface():
    assert sorted(itemeval.__all__) == [
        "ExperimentConfig",
        "Item",
        "__version__",
        "load_config",
    ]


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
