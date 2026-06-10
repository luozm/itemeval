"""Stable, content-derived condition ids."""

import re

from itemeval._util import canonical_json, sha256_hex


def slugify(text: str, max_len: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].strip("-")) or "x"


def model_short(model_id: str) -> str:
    """'openrouter/deepseek/deepseek-v3.2' -> 'deepseek-v3.2'."""
    return model_id.split("/")[-1]


def condition_digest(payload: dict) -> str:
    return sha256_hex(canonical_json(payload).encode("utf-8"))[:12]


def make_condition_id(slug_parts: "list[str]", payload: dict) -> "tuple[str, str]":
    """Returns (condition_id, slug) where condition_id = '<slug>--<digest12>'."""
    slug = "_".join(slugify(p) for p in slug_parts)
    return f"{slug}--{condition_digest(payload)}", slug
