"""Live-run heartbeat: an inspect SampleEnd/SampleStart hook -> throttled stderr line.

A generate/grade stage runs as one inspect ``eval()`` our orchestrator is blocked
inside; when the rich display is silenced (``--json`` / ``--display none`` / off-TTY)
the run goes dark — no progress, no ETA. This hook fires in-process as each sample
completes and writes a plain-text liveness line to stderr (relay-safe per
UX-PATTERNS Law 8 and the relay rule): it carries **no fact of record** — the final
counts/spend live in the run summary block and the result JSON — only live progress
plus a throughput-based ETA.

Because that per-sample line is completion-driven, a *hung* cell (no ``SampleEnd``)
freezes it. So a wall-clock straggler timer (``_straggler_loop``), launched onto
inspect's event loop in ``on_run_start`` and cancelled in ``on_run_end``, ticks
regardless and — during a stall — names the slowest in-flight cells
(``model · item · elapsed``, with ``try N`` when a cell is retrying). It shares the
per-sample throttle clock, so it only speaks when the per-sample line has gone quiet.

Boundary (DEVELOPMENT.md): an extension module, so the inspect hooks import lives
here. The hook is process-global (``eval()`` has no scoped ``hooks=`` param), so it
is a no-op (``enabled()`` False) outside an itemeval run; the orchestrator turns it
on for the duration of its eval via ``tracking()``. The callback is **awaited inside
``eval()``**, so it does only a counter bump + a throttled ``sys.stderr`` write —
never sized I/O (a slow hook would slow the run).
"""

import asyncio
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

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

from inspect_ai.hooks import (  # noqa: E402
    Hooks,
    RunEnd,
    RunStart,
    SampleAttemptStart,
    SampleEnd,
    SampleStart,
    TaskStart,
    hooks,
)

# Batch mode goes dark differently than a silenced display: on_sample_end fires only
# when a whole provider batch *resolves*, so between resolutions (minutes to hours)
# the per-sample heartbeat freezes. inspect emits an aggregate BatchStatus on every
# ~15s status poll (batch_count + pending/completed/failed request counts) and exposes
# set_batch_status_callback to capture it — but only via this private util path; it is
# re-exported from neither inspect_ai nor inspect_ai.model. We register it for the
# eval's duration so the heartbeat tracks batch churn in between, and so inspect's
# *default* callbacks (which print to STDOUT) do not run — that print would corrupt a
# --json run, the very mode the heartbeat exists for. Same deliberate private-symbol
# tradeoff as init_hooks above.
from inspect_ai.model._providers.util.batch_log import (  # noqa: E402
    BatchStatus,
    set_batch_log_callback,
    set_batch_status_callback,
)

_MIN_INTERVAL_S = 10.0  # throttle: at most one heartbeat line per this many seconds

# Straggler keepalive (timer-driven): when the per-sample line goes quiet (a hung
# cell emits no SampleEnd), a wall-clock timer lists the slowest in-flight cells.
_STRAGGLER_INTERVAL_S = 30.0  # timer tick + min gap since the last line before emitting
_STRAGGLER_THRESHOLD_S = 120.0  # only list cells in-flight at least this long
_STRAGGLER_CAP = 10  # max cells listed; the rest collapse to a "+N more" footer


@dataclass
class _Inflight:
    """A cell (one sample execution = item × epoch) that has started but not ended.
    The timer reads these to name the slow ones; ``attempt`` is bumped by inspect's
    per-attempt retry hook so the line can flag a retrying (vs. genuinely hung) cell."""

    model: str
    item: str
    start_monotonic: float
    attempt: int = 1


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
    last_emit_ended: int = -1  # ``ended`` count at the last emitted line (dedups the final line)
    # Straggler tracking: which cells are in-flight (so the timer can name the slow
    # ones), the eval_id→model map to label them, and the monotonic time of the last
    # completion (seeds "no completion for Ns"). ``straggler_task`` is the timer
    # coroutine handle, launched in on_run_start and cancelled in on_run_end.
    inflight: "dict[str, _Inflight]" = field(default_factory=dict)
    task_models: "dict[str, str]" = field(default_factory=dict)
    last_end_monotonic: float = 0.0
    straggler_task: "asyncio.Task | None" = None
    # Batch mode (provider Batch API): the per-sample counters above advance only in
    # chunks (one jump per batch that resolves), so these aggregate batch counts —
    # refreshed by inspect's ~15s status poll — carry liveness in between.
    batch: bool = False
    batch_count: int = 0
    batch_pending: int = 0
    batch_oldest_age: int = 0  # seconds the oldest still-open batch has been running


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
    have completed (the throughput estimate is meaningless before that). In batch
    mode the line is provider-paced: it carries the batch churn and drops the
    samples/sec ETA (a batch drains in minutes–hours on the provider's clock)."""
    parts = [f"[itemeval] {ctx.stage}"]
    if ctx.batch:
        parts.append("batch")
    if ctx.experiment_id:
        parts.append(f"exp {ctx.experiment_id}/a{ctx.attempt}")
    if ctx.total:
        pct = int(round(100 * ctx.ended / ctx.total))
        parts.append(f"{ctx.ended}/{ctx.total} ({pct}%)")
    else:
        parts.append(f"{ctx.ended} done")
    if ctx.batch:
        # ``ended`` jumps a batch at a time; these counts move on each ~15s poll, so
        # the line keeps refreshing while ``ended`` sits still between resolutions.
        parts.append(f"{ctx.batch_count} batches")
        parts.append(f"{ctx.batch_pending} pending")
        if ctx.batch_oldest_age > 0:
            parts.append(f"oldest {_fmt_duration(ctx.batch_oldest_age)}")
        parts.append(f"{ctx.errors} errors")
        return " · ".join(parts)
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


def render_stragglers(
    inflight: "dict[str, _Inflight]",
    now: float,
    *,
    stage: str,
    last_end_monotonic: float,
    threshold_s: float = _STRAGGLER_THRESHOLD_S,
    cap: int = _STRAGGLER_CAP,
) -> "str | None":
    """The stall block, pure for testing. Lists the in-flight cells over
    ``threshold_s``, slowest first, as ``model · item · elapsed`` (with ``try N``
    when retrying); caps at ``cap`` lines with a ``+N more`` footer. Returns None
    when no cell is over threshold (nothing worth printing yet)."""
    over = sorted(
        (c for c in inflight.values() if now - c.start_monotonic >= threshold_s),
        key=lambda c: c.start_monotonic,  # oldest start = longest elapsed = first
    )
    if not over:
        return None
    stalled = _fmt_duration(max(0.0, now - last_end_monotonic))
    lines = [
        f"[itemeval] {stage} · no completion for {stalled} · "
        f"{len(over)} cell(s) in-flight >{_fmt_duration(threshold_s)}:"
    ]
    for c in over[:cap]:
        retry = f" · try {c.attempt}" if c.attempt >= 2 else ""
        lines.append(
            f"[itemeval]   {c.model} · item {c.item} · "
            f"{_fmt_duration(now - c.start_monotonic)}{retry}"
        )
    if len(over) > cap:
        lines.append(f"[itemeval]   +{len(over) - cap} more")
    return "\n".join(lines)


def _safe_stderr(line: str) -> None:
    """A single stderr line, swallowing any write error — liveness must never break
    the run (the callback runs inside ``eval()``)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _emit(ctx: _RunContext, now: float, force: bool = False) -> None:
    # First line (last_emit == 0) always emits, to show life immediately; after
    # that, at most one per _MIN_INTERVAL_S unless forced (the final line).
    if not force and ctx.last_emit > 0 and (now - ctx.last_emit) < _MIN_INTERVAL_S:
        return
    ctx.last_emit = now
    ctx.last_emit_ended = ctx.ended
    _safe_stderr(render_heartbeat(ctx, now))


def _straggler_due(ctx: _RunContext, now: float) -> bool:
    """The timer emits a straggler block only when the per-sample line has gone quiet
    for ``_STRAGGLER_INTERVAL_S`` — i.e. a genuine stall, not a healthy run (whose
    completions emit lines ≤ _MIN_INTERVAL_S apart). Sharing ``last_emit`` with
    ``_emit`` keeps the straggler line from competing with the per-sample one and
    self-throttles it to one block per interval during a long stall."""
    return ctx.active and (now - ctx.last_emit) >= _STRAGGLER_INTERVAL_S


async def _straggler_loop() -> None:
    """Wall-clock keepalive scheduled onto inspect's event loop (launched in
    ``on_run_start``, cancelled in ``on_run_end``). It exists because the per-sample
    heartbeat is completion-driven: a hung cell emits no ``SampleEnd``, so the line
    freezes. This ticks regardless and, during a stall, names the slowest in-flight
    cells. Never raises into the loop — a render slip must not perturb the eval."""
    try:
        while _CTX.active:
            await asyncio.sleep(_STRAGGLER_INTERVAL_S)
            now = time.monotonic()
            if not _straggler_due(_CTX, now):
                continue
            # Snapshot the map: on_sample_end may pop entries between the iterations
            # below (same loop, cooperative — but cheap insurance against mutation).
            line = render_stragglers(
                dict(_CTX.inflight),
                now,
                stage=_CTX.stage,
                last_end_monotonic=_CTX.last_end_monotonic,
            )
            if line is not None:
                _CTX.last_emit = now
                _safe_stderr(line)
    except asyncio.CancelledError:
        pass


def _on_batch_status(status: "BatchStatus") -> None:
    """inspect's ~15s batch-status poll → refresh the batch counters and emit one
    heartbeat. Registered only for the duration of a batch eval (see ``tracking()``);
    reads ``_CTX`` directly and is a no-op once ``active`` is cleared. ``oldest_age``
    is wall-clock (the status timestamps are unix seconds), unlike the monotonic
    throttle clock — it is display-only."""
    if not _CTX.active:
        return
    _CTX.batch_count = status.batch_count
    _CTX.batch_pending = status.pending_requests
    _CTX.batch_oldest_age = (
        max(0, int(time.time() - status.oldest_created_at)) if status.oldest_created_at else 0
    )
    _emit(_CTX, time.monotonic())


@contextmanager
def tracking(
    stage: str,
    experiment_id: str,
    attempt: int,
    total: "int | None",
    *,
    enabled: bool,
    batch: bool = False,
):
    """Turn the heartbeat on for the wrapped ``eval()``. A no-op when ``enabled`` is
    False (the rich display is carrying liveness, or a notebook), so the hook stays
    dormant. Always clears ``active`` on exit, emitting one final line.

    When ``batch`` is set, also route inspect's batch-status poll (and its otherwise
    stdout-bound batch log) through this stderr heartbeat for the eval's duration, and
    print a one-time banner so the operator expects batched — not continuous —
    progress. Both provider callbacks are restored to inspect's default on exit."""
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
    _CTX.last_emit_ended = -1
    _CTX.inflight = {}
    _CTX.task_models = {}
    _CTX.last_end_monotonic = _CTX.start_monotonic
    _CTX.straggler_task = None
    _CTX.batch = batch
    _CTX.batch_count = 0
    _CTX.batch_pending = 0
    _CTX.batch_oldest_age = 0
    if batch:
        set_batch_status_callback(_on_batch_status)
        set_batch_log_callback(lambda msg: _safe_stderr(f"[itemeval] batch · {msg}"))
        _safe_stderr(
            "[itemeval] batch mode: results land in provider batches, not continuously "
            "(each batch runs minutes–hours). The line below refreshes on every ~15s "
            "poll; the done count jumps as each batch resolves."
        )
    try:
        yield
    finally:
        # Force a closing line carrying the terminal counts — unless the last
        # per-sample emit already showed this exact ``ended`` count (an unthrottled
        # final sample), which would otherwise duplicate the last line.
        if _CTX.ended and _CTX.last_emit_ended != _CTX.ended:
            _emit(_CTX, time.monotonic(), force=True)
        if batch:  # hand the provider callbacks back to inspect's default printer
            set_batch_status_callback(None)
            set_batch_log_callback(None)
        _CTX.active = False
        _CTX.batch = False


@hooks(name="itemeval/live-tracker", description="itemeval live-run stderr heartbeat")
class LiveTracker(Hooks):
    def enabled(self) -> bool:
        return _CTX.active

    async def on_run_start(self, data: RunStart) -> None:
        # Schedule the straggler timer onto inspect's event loop (we are inside it
        # here — eval() owns the loop, so this is the only place a task created now
        # will actually tick). Skipped in batch mode (liveness is already provider-
        # paced) and degrades to the completion-driven heartbeat if not on asyncio.
        if not _CTX.active or _CTX.batch:
            return
        try:
            _CTX.straggler_task = asyncio.create_task(_straggler_loop())
        except RuntimeError:
            _CTX.straggler_task = None

    async def on_run_end(self, data: RunEnd) -> None:
        task = _CTX.straggler_task
        _CTX.straggler_task = None
        if task is not None:
            task.cancel()
            try:
                await task  # in-loop teardown before eval() returns
            except (asyncio.CancelledError, Exception):
                pass

    async def on_task_start(self, data: TaskStart) -> None:
        # One task == one condition == one model; the per-sample events carry only
        # eval_id, so map it to the model here for the straggler line's label.
        _CTX.task_models[data.eval_id] = data.spec.model

    async def on_sample_start(self, data: SampleStart) -> None:
        _CTX.started += 1
        _CTX.inflight[data.sample_id] = _Inflight(
            model=_CTX.task_models.get(data.eval_id, "?"),
            item=str(data.summary.id),
            start_monotonic=time.monotonic(),
        )

    async def on_sample_attempt_start(self, data: SampleAttemptStart) -> None:
        # Fires on every attempt incl. the first (1-based); bump the in-flight cell so
        # a straggler line can flag a retrying cell (`try N`) vs. a genuinely hung one.
        cell = _CTX.inflight.get(data.sample_id)
        if cell is not None:
            cell.attempt = data.attempt

    async def on_sample_end(self, data: SampleEnd) -> None:
        _CTX.ended += 1
        _CTX.inflight.pop(data.sample_id, None)
        _CTX.last_end_monotonic = time.monotonic()
        if data.sample.error is not None:
            _CTX.errors += 1
        _emit(_CTX, time.monotonic())
