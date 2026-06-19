"""Per-endpoint context windows for OpenRouter models (endpoint-context-clamp).

OpenRouter's model-level ``context_length`` (in the pricing table) is the
*maximum across all providers* serving a model. A request can be routed to a
floor provider with a smaller window, so clamping ``max_tokens`` against the
model-level max lets a deterministic HTTP-400 through (see
``docs/plans/archive/endpoint-context-clamp.md``). This module fetches each
provider endpoint's own ``context_length`` from OpenRouter and returns the
**minimum** — the window any routing can land on, i.e. the safe clamp ceiling.

Network is confined to this budget-layer module (mirroring ``_pricing.py``).
Fetches are roster-scoped (only the models about to run) and cached on disk so
warm runs cost zero calls; a fetch error degrades to ``None`` (unknown window →
clamp falls back to the model-level value, today's behavior) and never blocks.
"""

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from itemeval._util import atomic_write_bytes, utc_now_iso
from itemeval.budget._pricing import user_pricing_path

OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"
_OPENROUTER_PREFIX = "openrouter/"

# Endpoint windows change rarely; a fixed internal staleness keeps this an
# invisible optimization default (UX-PATTERNS Law 5) — no config knob.
ENDPOINT_MAX_AGE_DAYS = 30.0


class WindowEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Smallest endpoint context window (tokens); None when OpenRouter exposed no
    # endpoint window for the model (kept so we don't refetch a dead lookup
    # every run).
    min_context: int | None = None
    fetched_at: str  # utc_now_iso() stamp


class EndpointWindows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows: dict[str, WindowEntry] = {}


class FetchStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetched: int = 0  # models hit over the network this call
    reused: int = 0  # models served from a fresh cache entry


def user_endpoints_path() -> Path:
    """Cache file next to the pricing cache; ``ITEMEVAL_ENDPOINTS_PATH`` wins.

    Defaults to ``endpoints.json`` beside ``pricing.json`` so the two budget
    caches live together and share an override convention.
    """
    import os

    env = os.environ.get("ITEMEVAL_ENDPOINTS_PATH")
    if env:
        return Path(env)
    return user_pricing_path().parent / "endpoints.json"


def min_window_from_payload(data: dict) -> int | None:
    """Smallest ``context_length`` across an endpoints-API response, or None.

    Pure: no network. ``data`` is the parsed JSON from the endpoints API
    (``{"data": {"endpoints": [{"context_length": ...}, ...]}}``); endpoints
    without a context_length are ignored.
    """
    endpoints = ((data or {}).get("data") or {}).get("endpoints") or []
    ctxs = [e.get("context_length") for e in endpoints if e.get("context_length")]
    return min(ctxs) if ctxs else None


def endpoint_min_context(model_id: str, *, timeout: float = 30.0) -> int | None:
    """Fetch a model's smallest endpoint window from OpenRouter, or None.

    Only ``openrouter/<author>/<slug>`` ids have an endpoints API; any other id
    returns None without a call. Raises on a network/parse error — the caller
    (``load_endpoint_windows``) catches it and records None.
    """
    if not model_id.startswith(_OPENROUTER_PREFIX):
        return None
    slug = model_id[len(_OPENROUTER_PREFIX) :]
    url = OPENROUTER_ENDPOINTS_URL.format(slug=slug)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (https only)
        data = json.loads(resp.read())
    return min_window_from_payload(data)


def _read_cache(path: Path) -> EndpointWindows:
    if not path.is_file():
        return EndpointWindows()
    try:
        return EndpointWindows.model_validate_json(path.read_bytes())
    except (json.JSONDecodeError, ValidationError):
        return EndpointWindows()  # a corrupt cache self-heals on next write


def _age_days(stamp: str, now_iso: str) -> "float | None":
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        then = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        now = datetime.strptime(now_iso, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - then).total_seconds() / 86400.0


def load_endpoint_windows(
    model_ids: "list[str]",
    *,
    max_age_days: float = ENDPOINT_MAX_AGE_DAYS,
    now_iso: "str | None" = None,
    fetch: "Callable[[str], int | None] | None" = None,
    path: "Path | None" = None,
) -> "tuple[dict[str, int | None], FetchStats]":
    """Return ``{model_id: min_endpoint_window}`` for the OpenRouter ids given.

    Fetches only ids missing from the cache or older than ``max_age_days``;
    fresh entries are reused at $0. Non-``openrouter/*`` ids are skipped (no
    endpoints API) and absent from the result, so ``result.get(id)`` is None.
    A fetch that raises is recorded as None (degrade, never block). ``fetch``
    and ``now_iso`` are injectable for tests.
    """
    # Resolve the default at call time (not as a bound default) so tests can
    # monkeypatch `endpoint_min_context` to keep the suite off the network.
    fetch = fetch if fetch is not None else endpoint_min_context
    now_iso = now_iso or utc_now_iso()
    path = path or user_endpoints_path()
    cache = _read_cache(path)
    stats = FetchStats()
    dirty = False
    for mid in dict.fromkeys(model_ids):  # dedup, preserve first-seen order
        if not mid.startswith(_OPENROUTER_PREFIX):
            continue  # no endpoints API for direct ids
        entry = cache.windows.get(mid)
        if entry is not None:
            age = _age_days(entry.fetched_at, now_iso)
            if age is not None and age < max_age_days:
                stats.reused += 1
                continue
        try:
            mc = fetch(mid)
        except Exception:
            mc = None  # degrade: unknown window, fall back to model-level ctx
        cache.windows[mid] = WindowEntry(min_context=mc, fetched_at=now_iso)
        stats.fetched += 1
        dirty = True
    if dirty:
        atomic_write_bytes(path, (cache.model_dump_json(indent=2) + "\n").encode("utf-8"))
    result = {
        mid: cache.windows[mid].min_context
        for mid in dict.fromkeys(model_ids)
        if mid in cache.windows
    }
    return result, stats
