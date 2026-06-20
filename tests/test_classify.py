"""Terminal-vs-transient error classifier (preflight-check W1) — pure, no network."""

import pytest

from itemeval._classify import classify_error, classify_message, http_status, labeled


class FakeHTTPError(Exception):
    def __init__(self, status: int, msg: str = ""):
        super().__init__(msg)
        self.status_code = status


class FakeResponseError(Exception):
    """Mimics an SDK error exposing the status under `.response.status_code`."""

    def __init__(self, status: int, msg: str = ""):
        super().__init__(msg)
        self.response = type("R", (), {"status_code": status})()


class PrerequisiteError(Exception):  # name-matched by the classifier (engine-free)
    pass


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (404, "", "terminal"),
        (401, "", "terminal"),
        (403, "", "terminal"),
        (429, "", "transient"),
        (500, "", "transient"),
        (503, "", "transient"),
        (None, "model not found", "terminal"),
        (None, "This model is deprecated", "terminal"),
        (None, "no endpoints found for x/y", "terminal"),
        (None, "Rate limit exceeded, try again", "transient"),
        (None, "Connection reset by peer", "transient"),
        (None, "request timed out", "transient"),
        (None, "something weird happened", "unknown"),
        (400, "", "unknown"),  # bare 400 is ambiguous → not accused
        (400, "model does not exist", "terminal"),  # 400 + message → terminal
        (None, "", "unknown"),
    ],
)
def test_classify_message(status, message, expected):
    assert classify_message(status, message) == expected


def test_http_status_reads_status_code_and_response():
    assert http_status(FakeHTTPError(404)) == 404
    assert http_status(FakeResponseError(503)) == 503
    assert http_status(RuntimeError("no status")) is None
    assert http_status(None) is None


def test_classify_error_prefers_status_then_message():
    assert classify_error(FakeHTTPError(404, "gone")) == "terminal"
    assert classify_error(FakeResponseError(429, "slow down")) == "transient"
    assert classify_error(RuntimeError("model not found")) == "terminal"
    assert classify_error(RuntimeError("kaboom")) == "unknown"


def test_classify_error_treats_prerequisite_as_terminal():
    # A missing provider SDK / API key is a roster problem the user must fix.
    assert classify_error(PrerequisiteError("install anthropic")) == "terminal"


def test_labeled_prefixes_only_when_classified():
    assert labeled("404 model not found").startswith("terminal: ")
    assert labeled("request timed out").startswith("transient: ")
    # Unclassifiable → unchanged (never a misleading label).
    assert labeled("eval status: error — mystery") == "eval status: error — mystery"
    # exc= path routes through classify_error (status code wins).
    assert labeled("boom", exc=FakeHTTPError(404)).startswith("terminal: ")
