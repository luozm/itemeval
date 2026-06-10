from itemeval.design._ids import (
    condition_digest,
    make_condition_id,
    model_short,
    slugify,
)


def test_slugify():
    assert slugify("OpenAI/gpt-5 Mini!") == "openai-gpt-5-mini"
    assert slugify("___") == "x"
    assert slugify("a" * 50) == "a" * 24


def test_model_short():
    assert model_short("openrouter/deepseek/deepseek-v3.2") == "deepseek-v3.2"
    assert model_short("mockllm/solver-a") == "solver-a"


def test_digest_stable_and_order_insensitive():
    a = condition_digest({"x": 1, "y": [1, 2]})
    b = condition_digest({"y": [1, 2], "x": 1})
    assert a == b
    assert len(a) == 12
    assert int(a, 16) >= 0


def test_digest_changes_with_content():
    assert condition_digest({"x": 1}) != condition_digest({"x": 2})


def test_make_condition_id_shape():
    cid, slug = make_condition_id(["GPT-5 mini", "Minimal"], {"k": "v"})
    assert slug == "gpt-5-mini_minimal"
    assert cid == f"{slug}--{condition_digest({'k': 'v'})}"
