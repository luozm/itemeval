"""Small shared helpers (leaf module: imports nothing from itemeval)."""

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a sibling tmp file then os.replace() into place. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def estimate_tokens(text: str) -> int:
    """Token heuristic used by the estimator and the mock models: ceil(chars/4)."""
    return max(1, math.ceil(len(text) / 4))


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow: remove keys whose value is None (used for condition payloads)."""
    return {k: v for k, v in d.items() if v is not None}
