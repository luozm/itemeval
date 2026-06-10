"""Prompt/rubric template registry with content hashing and brace-safe rendering.

A facet references a template either by a **bare name** (`standard`), resolved
from the user's local `prompts_dir`/`rubrics_dir`, or with the **`builtin:`
prefix** (`builtin:standard`), resolved from the templates packaged inside
itemeval. The two namespaces never collide and never silently shadow each
other: a local `standard` and `builtin:standard` are distinct references with
their own content hash and `source`, recorded separately for reproducibility.
"""

import re
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict

from itemeval._errors import TemplateError
from itemeval._util import sha256_hex

BUILTIN_PREFIX = "builtin:"


class Template(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # the reference as written, e.g. "standard" or "builtin:standard"
    source: Literal["local", "builtin"]
    path: str  # local absolute path, or "builtin:<subdir>/<name>.md"
    text: str  # newline-normalized content
    sha256: str  # full 64-hex content hash

    @property
    def hash12(self) -> str:
        return self.sha256[:12]


def _make_template(name: str, source: str, path: str, raw: str) -> Template:
    text = raw.replace("\r\n", "\n")
    return Template(
        name=name,
        source=source,  # type: ignore[arg-type]
        path=path,
        text=text,
        sha256=sha256_hex(text.encode("utf-8")),
    )


def load_template(path: Path, name: str) -> Template:
    """Load a local template file as the bare reference `name`."""
    if not path.is_file():
        raise TemplateError(f"template '{name}' not found: {path}")
    return _make_template(name, "local", str(path), path.read_text(encoding="utf-8"))


def _builtin_root() -> Traversable:
    return files("itemeval._builtin")


def builtin_names(subdir: str) -> list[str]:
    """Names of packaged templates under `subdir` (e.g. 'prompts/solver', 'rubrics')."""
    root: Traversable = _builtin_root()
    for part in subdir.split("/"):
        root = root.joinpath(part)
    if not root.is_dir():
        return []
    return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))


def read_builtin(subdir: str, name: str) -> "str | None":
    """Raw text of a packaged template, or None if it does not exist."""
    # One segment per joinpath: importlib.resources' MultiplexedPath.joinpath only
    # accepts multiple descendants on Python 3.12+; 3.11 takes a single arg.
    res = _builtin_root()
    for part in (*subdir.split("/"), f"{name}.md"):
        res = res.joinpath(part)
    if not res.is_file():
        return None
    return res.read_text(encoding="utf-8")


class TemplateRegistry:
    """Resolves a facet reference to a Template: bare -> local dir, `builtin:` -> package."""

    def __init__(self, local_root: Path, builtin_subdir: str, kind: str) -> None:
        self.local_root = (
            local_root  # <input_base>/<prompts_dir>/solver or <input_base>/<rubrics_dir>
        )
        self.builtin_subdir = builtin_subdir  # "prompts/solver" | "rubrics"
        self.kind = kind  # "solver" | "rubric" (error messages only)
        self._cache: dict[str, Template] = {}

    def get(self, ref: str) -> Template:
        if ref not in self._cache:
            if ref.startswith(BUILTIN_PREFIX):
                self._cache[ref] = self._get_builtin(ref)
            else:
                self._cache[ref] = self._get_local(ref)
        return self._cache[ref]

    def _get_builtin(self, ref: str) -> Template:
        name = ref[len(BUILTIN_PREFIX) :]
        raw = read_builtin(self.builtin_subdir, name)
        if raw is None:
            available = ", ".join(self.builtin_names()) or "(none)"
            raise TemplateError(
                f"built-in {self.kind} template '{name}' not found "
                f"(available built-in: {available})"
            )
        return _make_template(
            ref, "builtin", f"{BUILTIN_PREFIX}{self.builtin_subdir}/{name}.md", raw
        )

    def _get_local(self, ref: str) -> Template:
        path = self.local_root / f"{ref}.md"
        if not path.is_file():
            local = ", ".join(self.local_names()) or "(none)"
            hint = ""
            if read_builtin(self.builtin_subdir, ref) is not None:
                hint = f"; a built-in exists — reference it as '{BUILTIN_PREFIX}{ref}'"
            raise TemplateError(
                f"local {self.kind} template '{ref}' not found in {self.local_root} "
                f"(available local: {local}{hint})"
            )
        return load_template(path, ref)

    def local_names(self) -> list[str]:
        if not self.local_root.is_dir():
            return []
        return sorted(p.stem for p in self.local_root.glob("*.md"))

    def builtin_names(self) -> list[str]:
        return builtin_names(self.builtin_subdir)


def solver_registry(config) -> TemplateRegistry:
    return TemplateRegistry(
        config.resolve_input_dir(config.prompts_dir) / "solver", "prompts/solver", "solver"
    )


def rubric_registry(config) -> TemplateRegistry:
    return TemplateRegistry(config.resolve_input_dir(config.rubrics_dir), "rubrics", "rubric")


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
