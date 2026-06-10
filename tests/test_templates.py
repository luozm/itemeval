import pytest

from itemeval._errors import TemplateError
from itemeval._templates import (
    Template,
    TemplateRegistry,
    load_template,
    render_template,
    validate_template,
)
from itemeval._util import sha256_hex


def test_load_template_normalizes_crlf(tmp_path):
    p = tmp_path / "t.md"
    p.write_bytes(b"line1\r\nline2 {input}\r\n")
    t = load_template(p, "t")
    assert t.text == "line1\nline2 {input}\n"
    assert t.sha256 == sha256_hex(t.text.encode("utf-8"))
    assert t.hash12 == t.sha256[:12]


def test_load_template_missing(tmp_path):
    with pytest.raises(TemplateError, match="nope"):
        load_template(tmp_path / "nope.md", "nope")


def test_render_is_brace_safe():
    text = r"Latex \frac{a}{b} and {\"json\": 1} stay; {input} goes"
    assert render_template(text, {"input": "HERE"}) == (
        r"Latex \frac{a}{b} and {\"json\": 1} stay; HERE goes"
    )


def test_render_multiple_and_repeated():
    assert render_template("{a}{b}{a}", {"a": "1", "b": "2"}) == "121"


def test_render_empty_values_noop():
    assert render_template("{x}", {}) == "{x}"


def test_validate_template_missing_placeholder():
    t = Template(name="r", path="/x", text="no placeholders", sha256=sha256_hex(b"no placeholders"))
    with pytest.raises(TemplateError, match=r"\{input\}"):
        validate_template(t, {"input"})


def test_registry_lists_available(tmp_path):
    (tmp_path / "a.md").write_text("{input}")
    (tmp_path / "b.md").write_text("{input}")
    reg = TemplateRegistry(tmp_path, "solver")
    assert reg.names() == ["a", "b"]
    assert reg.get("a").name == "a"
    with pytest.raises(TemplateError, match="available: a, b"):
        reg.get("missing")


def test_registry_missing_root(tmp_path):
    reg = TemplateRegistry(tmp_path / "absent", "rubric")
    assert reg.names() == []
