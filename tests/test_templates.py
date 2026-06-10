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
    t = Template(
        name="r",
        source="local",
        path="/x",
        text="no placeholders",
        sha256=sha256_hex(b"no placeholders"),
    )
    with pytest.raises(TemplateError, match=r"\{input\}"):
        validate_template(t, {"input"})


def test_registry_resolves_local_bare_name(tmp_path):
    (tmp_path / "a.md").write_text("{input}")
    (tmp_path / "b.md").write_text("{input}")
    reg = TemplateRegistry(tmp_path, "prompts/solver", "solver")
    assert reg.local_names() == ["a", "b"]
    t = reg.get("a")
    assert t.name == "a" and t.source == "local"
    with pytest.raises(TemplateError, match="available local: a, b"):
        reg.get("missing")


def test_registry_resolves_builtin_prefix(tmp_path):
    reg = TemplateRegistry(tmp_path, "prompts/solver", "solver")
    t = reg.get("builtin:standard")
    assert t.source == "builtin"
    assert t.name == "builtin:standard"
    assert t.path == "builtin:prompts/solver/standard.md"
    assert "{input}" in t.text
    assert "standard" in reg.builtin_names() and "minimal" in reg.builtin_names()


def test_registry_local_and_builtin_same_name_are_distinct(tmp_path):
    """A local 'standard' and 'builtin:standard' are separate refs, not silently merged."""
    (tmp_path / "standard.md").write_text("LOCAL custom rubric {input} {solution}")
    reg = TemplateRegistry(tmp_path, "rubrics", "rubric")
    local = reg.get("standard")
    builtin = reg.get("builtin:standard")
    assert local.source == "local" and builtin.source == "builtin"
    assert local.sha256 != builtin.sha256  # different content, recorded distinctly


def test_registry_missing_builtin_lists_available(tmp_path):
    reg = TemplateRegistry(tmp_path, "rubrics", "rubric")
    with pytest.raises(TemplateError, match="built-in rubric template 'nope' not found"):
        reg.get("builtin:nope")


def test_registry_bare_name_hints_builtin(tmp_path):
    reg = TemplateRegistry(tmp_path, "prompts/solver", "solver")
    with pytest.raises(TemplateError, match="reference it as 'builtin:standard'"):
        reg.get("standard")  # not local, but exists as a built-in


def test_registry_missing_local_root(tmp_path):
    reg = TemplateRegistry(tmp_path / "absent", "rubrics", "rubric")
    assert reg.local_names() == []
