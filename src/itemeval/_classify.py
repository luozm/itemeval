"""Terminal-vs-transient classification of a model-call failure (preflight-check).

The reusable primitive: label why a model call failed so callers can tell a
*roster* problem (a dead/EOL model, bad auth — the user must edit the config)
apart from a *retryable* one (timeout, rate limit, 5xx — a retry or reroute may
succeed). Used by the preflight probe to mark a model "dead" vs "unverified", and
by the run-time condition error reporting to label a failure in one concise line.
The shipped `request-timeout` feature's "don't retry a terminal timeout"
refinement will consume this too.

Pure and engine-free: it reads a duck-typed status code plus the message text, so
it unit-tests without inspect_ai or a network. Bias is deliberately conservative
toward `transient`/`unknown` — mislabeling a merely rate-limited model as
`terminal` would tell a user to delete a model that is actually fine, the more
harmful error.
"""

from typing import Literal

Classification = Literal["terminal", "transient", "unknown"]

# HTTP statuses a ~1-token liveness probe can see. Only unambiguously-fatal codes
# are terminal; 400 (often a param/content quibble, not a dead model) falls
# through to the message check rather than accusing the roster.
_TERMINAL_STATUS = frozenset({401, 403, 404, 405})
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

_TERMINAL_SUBSTRINGS = (
    "model not found",
    "does not exist",
    "no endpoints found",
    "not a valid model",
    "no allowed providers",
    "is not supported",
    "deprecated",
    "decommission",
    "end-of-life",
    "end of life",
    "unauthorized",
    "invalid api key",
    "no api key",
    "permission",
)
_TRANSIENT_SUBSTRINGS = (
    "timeout",
    "timed out",
    "rate limit",
    "overloaded",
    "temporarily",
    "try again",
    "connection",
    "reset by peer",
    "service unavailable",
)


def http_status(exc: "BaseException | None") -> "int | None":
    """Best-effort HTTP status off a provider/SDK/httpx exception."""
    if exc is None:
        return None
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


def classify_message(status: "int | None", message: "str | None") -> Classification:
    """Classify from a status code (most reliable) then a message substring."""
    if status is not None:
        if status in _TERMINAL_STATUS:
            return "terminal"
        if status in _TRANSIENT_STATUS:
            return "transient"
    text = (message or "").lower()
    if any(s in text for s in _TERMINAL_SUBSTRINGS):
        return "terminal"
    if any(s in text for s in _TRANSIENT_SUBSTRINGS):
        return "transient"
    return "unknown"


def classify_error(exc: BaseException) -> Classification:
    """Classify a caught exception from a model call."""
    # inspect raises PrerequisiteError for a missing provider SDK / API key — a
    # roster problem the user fixes, so terminal. Matched by name to keep this
    # module engine-free (no inspect import).
    if type(exc).__name__ == "PrerequisiteError":
        return "terminal"
    return classify_message(http_status(exc), str(exc))


def labeled(message: str, *, exc: "BaseException | None" = None) -> str:
    """Prefix a one-line failure detail with its classification, e.g.
    ``terminal: 404 model not found``. Unchanged when unclassifiable, so the line
    never gains a misleading label."""
    cls = classify_error(exc) if exc is not None else classify_message(None, message)
    return f"{cls}: {message}" if cls in ("terminal", "transient") else message
