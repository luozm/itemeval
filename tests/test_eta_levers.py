"""Coarse ETA helpers (W2) + cost-lever pre-flight line (Issue #3)."""

import pandas as pd

from itemeval.budget._estimator import (
    DEFAULT_CALL_LATENCY_S,
    estimate_study,
    eta_seconds,
    median_latency_s,
)
from itemeval.cli import _cost_levers_line, _fmt_duration


def test_eta_seconds_math():
    assert eta_seconds(0, 4, 5.0) is None  # nothing to run
    assert eta_seconds(8, 2, 4.0) == 16.0  # (8/2) * 4
    assert eta_seconds(10, 1, None) == 10 * DEFAULT_CALL_LATENCY_S  # default prior
    assert eta_seconds(10, 0, 3.0) == 30.0  # concurrency floored to 1
    assert eta_seconds(6, 3, -1.0) == 2 * DEFAULT_CALL_LATENCY_S  # bad latency -> default


def test_median_latency_s():
    assert median_latency_s(None) is None
    assert median_latency_s(pd.DataFrame()) is None
    assert median_latency_s(pd.DataFrame({"x": [1]})) is None  # no latency column
    df = pd.DataFrame({"latency_s": [2.0, None, 0.0, 4.0]})  # drop NaN + non-positive
    assert median_latency_s(df) == 3.0


def test_fmt_duration():
    assert _fmt_duration(32) == "32s"
    assert _fmt_duration(90) == "1m"
    assert _fmt_duration(3 * 3600 + 5 * 60) == "3h 5m"


def test_estimate_carries_eta_and_concurrency(study):
    _, prep = study  # dev: 2 distinct mock models, 2 items, 2 epochs
    est = estimate_study(prep)
    gen = est.generate
    assert gen.concurrency == 2  # two distinct solver models -> parallel
    assert gen.remaining_calls == 8
    assert gen.eta_latency_basis == "default"  # cold study, no observed latency
    assert gen.eta_seconds == (8 / 2) * DEFAULT_CALL_LATENCY_S


def test_cost_levers_line_dev(study):
    _, prep = study  # policy dev, reps=2, mock models, cache default on
    line = _cost_levers_line(prep, "generate")
    assert "batch off (dev policy)" in line
    assert "native-routing off (needs batch plan)" in line
    assert "prompt-cache on (auto, reps>1)" in line
    assert "response-cache on" in line
