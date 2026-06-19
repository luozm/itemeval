"""Per-endpoint context-window fetch + cache (endpoint-context-clamp W1).

No network: the OpenRouter fetch is injected. Cache lives in a tmp file via
ITEMEVAL_ENDPOINTS_PATH so tests never touch the user cache.
"""

import pytest

from itemeval.budget import _endpoint_windows as ew


def test_min_window_from_payload_picks_smallest():
    data = {
        "data": {
            "endpoints": [
                {"context_length": 131072},
                {"context_length": 32768},
                {"context_length": 65536},
            ]
        }
    }
    assert ew.min_window_from_payload(data) == 32768


def test_min_window_from_payload_ignores_missing_and_empty():
    assert ew.min_window_from_payload({"data": {"endpoints": []}}) is None
    assert ew.min_window_from_payload({}) is None
    data = {"data": {"endpoints": [{"context_length": None}, {"context_length": 32768}]}}
    assert ew.min_window_from_payload(data) == 32768


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ITEMEVAL_ENDPOINTS_PATH", str(tmp_path / "endpoints.json"))
    return tmp_path


def test_load_skips_non_openrouter_without_fetching(cache_env):
    calls: list[str] = []

    def fake(mid):
        calls.append(mid)
        return 32768

    windows, stats = ew.load_endpoint_windows(["mockllm/x", "openai/gpt-5"], fetch=fake)
    assert calls == []  # neither is an openrouter/* id → no endpoints API
    assert stats.fetched == 0 and stats.reused == 0
    assert windows.get("openai/gpt-5") is None


def test_load_fetches_openrouter_then_reuses_warm_cache(cache_env):
    calls: list[str] = []

    def fake(mid):
        calls.append(mid)
        return 32768

    mid = "openrouter/qwen/qwen-2.5-7b-instruct"
    windows, stats = ew.load_endpoint_windows([mid], fetch=fake)
    assert windows[mid] == 32768
    assert stats.fetched == 1 and stats.reused == 0
    assert calls == [mid]

    calls.clear()
    windows2, stats2 = ew.load_endpoint_windows([mid], fetch=fake)
    assert calls == []  # fresh cache → no refetch
    assert stats2.reused == 1 and stats2.fetched == 0
    assert windows2[mid] == 32768


def test_load_refetches_when_stale(cache_env):
    mid = "openrouter/qwen/qwen-2.5-7b-instruct"
    ew.load_endpoint_windows([mid], fetch=lambda m: 32768, now_iso="2026-01-01T00:00:00Z")
    calls: list[str] = []

    def fake(m):
        calls.append(m)
        return 16384

    windows, stats = ew.load_endpoint_windows(
        [mid], fetch=fake, now_iso="2026-06-01T00:00:00Z", max_age_days=30
    )
    assert calls == [mid]
    assert windows[mid] == 16384 and stats.fetched == 1


def test_load_records_none_on_fetch_error_without_raising(cache_env):
    mid = "openrouter/foo/bar"

    def boom(m):
        raise RuntimeError("network down")

    windows, stats = ew.load_endpoint_windows([mid], fetch=boom)
    assert windows[mid] is None  # recorded, not raised
    assert stats.fetched == 1

    # a recorded None is cached too (don't hammer a dead endpoint every run)
    calls: list[str] = []
    windows2, stats2 = ew.load_endpoint_windows([mid], fetch=lambda m: calls.append(m) or 999)
    assert calls == [] and stats2.reused == 1 and windows2[mid] is None


def test_load_dedups_repeated_ids(cache_env):
    calls: list[str] = []
    mid = "openrouter/qwen/qwen-2.5-7b-instruct"
    windows, stats = ew.load_endpoint_windows(
        [mid, mid, mid], fetch=lambda m: calls.append(m) or 32768
    )
    assert calls == [mid]  # fetched once despite three references
    assert stats.fetched == 1
    assert windows[mid] == 32768
