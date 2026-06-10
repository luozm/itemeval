"""Prompt/rubric template registry with content hashing and brace-safe rendering."""

import re
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict

from itemeval._errors import TemplateError
from itemeval._util import sha256_hex


class Template(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str  # absolute path
    text: str  # newline-normalized content
    sha256: str  # full 64-hex content hash

    @property
    def hash12(self) -> str:
        return self.sha256[:12]


def load_template(path: Path, name: str) -> Template:
    if not path.is_file():
        raise TemplateError(f"template '{name}' not found: {path}")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return Template(name=name, path=str(path), text=text, sha256=sha256_hex(text.encode("utf-8")))


class TemplateRegistry:
    def __init__(self, root: Path, kind: str) -> None:
        self.root = root
        self.kind = kind  # "solver" | "rubric" (error messages only)
        self._cache: dict[str, Template] = {}

    def get(self, name: str) -> Template:
        if name not in self._cache:
            path = self.root / f"{name}.md"
            if not path.is_file():
                available = ", ".join(self.names()) or "(none)"
                raise TemplateError(
                    f"{self.kind} template '{name}' not found in {self.root} "
                    f"(available: {available})"
                )
            self._cache[name] = load_template(path, name)
        return self._cache[name]

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.md"))


def solver_registry(config) -> TemplateRegistry:
    return TemplateRegistry(config.base_dir / config.prompts_dir / "solver", "solver")


def rubric_registry(config) -> TemplateRegistry:
    return TemplateRegistry(config.base_dir / config.rubrics_dir, "rubric")


def render_template(text: str, values: Mapping[str, str]) -> str:
    """Replace known {placeholder}s only — str.format would break on LaTeX/JSON braces."""
    if not values:
        return text
    pattern = re.compile("|".join(r"\{" + re.escape(k) + r"\}" for k in sorted(values)))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], text)


def validate_template(template: Template, required: "set[str]") -> None:
    missing = sorted(name for name in required if ("{" + name + "}") not in template.text)
    if missing:
        raise TemplateError(
            f"template '{template.name}' ({template.path}) is missing required "
            f"placeholder(s): {', '.join('{' + m + '}' for m in missing)}"
        )
