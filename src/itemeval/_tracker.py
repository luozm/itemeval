"""Live-run heartbeat: an inspect SampleEnd/SampleStart hook -> throttled stderr line.

A generate/grade stage runs as one inspect ``eval()`` our orchestrator is blocked
inside; when the rich display is silenced (``--json`` / ``--display none`` / off-TTY)
the run goes dark — no progress, no ETA. This hook fires in-process as each sample
completes and writes a plain-text liveness line to stderr (relay-safe per
UX-PATTERNS Law 8 and the relay rule): it carries **no fact of record** — the final
counts/spend live in the run summary block and the result JSON — only live progress
plus a throughput-based ETA.

Boundary (DEVELOPMENT.md): an extension module, so the inspect hooks import lives
here. The hook is process-global (``eval()`` has no scoped ``hooks=`` param), so it
is a no-op (``enabled()`` False) outside an itemeval run; the orchestrator turns it
on for the duration of its eval via ``tracking()``. The callback is **awaited inside
``eval()``**, so it does only a counter bump + a throttled ``sys.stderr`` write —
never sized I/O (a slow hook would slow the run).
"""

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

# Pre-latch inspect's hook startup BEFORE we register our hook. init_hooks()
# (latched once per process) rich.print()s a "hooks enabled: N" banner to STDOUT the
# first time it runs for ANY registered hook — which would corrupt a --json run's
# one-JSON-doc contract (it uses `from rich import print`, not the display manager,
# so display=none does not silence it). Latching it here, while nothing of type
# "hooks" is yet registered, makes that banner empty; our hook below is still
# discovered for emission by get_all_hooks() (a separate cache that re-scans the
# registry on change). This reaches one private startup symbol deliberately — the
# published @hooks decorator gives us no other way to keep stdout clean.
from inspect_ai.hooks._startup import init_hooks as _init_hooks

_init_hooks()

from inspect_ai.hooks import Hooks, SampleEnd, SampleStart, hooks  # noqa: E402

_MIN_INTERVAL_S = 10.0  # throttle: at most one heartbeat line per this many seconds


@dataclass
class _RunContext:
    """Mutable run-scoped counters the hook updates and the orchestrator frames.

    One persistent instance (`_CTX`); `tracking()` resets it per run rather than
    reassigning, so the hook always reads the same object."""

    active: bool = False
    stage: str = ""
    experiment_id: str = ""
    attempt: int = 0
    total: "int | None" = None  # expected sample_end count; None -> no ETA/percent
    started: int = 0
    ended: int = 0
    errors: int = 0
    start_monotonic: float = 0.0
    last_emit: float = 0.0


_CTX = _RunContext()


def _fmt_duration(seconds: float) -> str:
    """Coarse human duration: <1m as 'Ns', else 'Hh Mm' / 'Mm' (mirrors cli)."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def render_heartbeat(ctx: _RunContext, now: float) -> str:
    """The one stderr line, pure for testing. Rate/ETA appear only once ≥3 samples
    have completed (the throughput estimate is meaningless before that)."""
    parts = [f"[itemeval] {ctx.stage}"]
    if ctx.experiment_id:
        parts.append(f"exp {ctx.experiment_id}/a{ctx.attempt}")
    if ctx.total:
        pct = int(round(100 * ctx.ended / ctx.total))
        parts.append(f"{ctx.ended}/{ctx.total} ({pct}%)")
    else:
        parts.append(f"{ctx.ended} done")
    elapsed = now - ctx.start_monotonic
    if ctx.ended >= 3 and elapsed > 0:
        rate = ctx.ended / elapsed  # samples/sec
        parts.append(f"{rate * 60:.0f}/min")
        if ctx.total and rate > 0:
            parts.append(f"~{_fmt_duration(max(0, ctx.total - ctx.ended) / rate)} left")
    parts.append(f"{ctx.errors} errors")
    inflight = ctx.started - ctx.ended
    if inflight > 0:
        parts.append(f"{inflight} in-flight")
    return " · ".join(parts)


def _emit(ctx: _RunContext, now: float, force: bool = False) -> None:
    # First line (last_emit == 0) always emits, to show life immediately; after
    # that, at most one per _MIN_INTERVAL_S unless forced (the final line).
    if not force and ctx.last_emit > 0 and (now - ctx.last_emit) < _MIN_INTERVAL_S:
        return
    ctx.last_emit = now
    try:
        print(render_heartbeat(ctx, now), file=sys.stderr, flush=True)
    except Exception:
        pass  # liveness must never break the run


@contextmanager
def tracking(
    stage: str,
    experiment_id: str,
    attempt: int,
    total: "int | None",
    *,
    enabled: bool,
):
    """Turn the heartbeat on for the wrapped ``eval()``. A no-op when ``enabled`` is
    False (the rich display is carrying liveness, or a notebook), so the hook stays
    dormant. Always clears ``active`` on exit, emitting one final line."""
    if not enabled:
        yield
        return
    _CTX.active = True
    _CTX.stage = stage
    _CTX.experiment_id = experiment_id or ""
    _CTX.attempt = int(attempt)
    _CTX.total = total
    _CTX.started = 0
    _CTX.ended = 0
    _CTX.errors = 0
    _CTX.start_monotonic = time.monotonic()
    _CTX.last_emit = 0.0
    try:
        yield
    finally:
        if _CTX.ended:
            _emit(_CTX, time.monotonic(), force=True)
        _CTX.active = False


@hooks(name="itemeval/live-tracker", description="itemeval live-run stderr heartbeat")
class LiveTracker(Hooks):
    def enabled(self) -> bool:
        return _CTX.active

    async def on_sample_start(self, data: SampleStart) -> None:
        _CTX.started += 1

    async def on_sample_end(self, data: SampleEnd) -> None:
        _CTX.ended += 1
        if data.sample.error is not None:
            _CTX.errors += 1
        _emit(_CTX, time.monotonic())
