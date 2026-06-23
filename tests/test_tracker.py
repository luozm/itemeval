"""Live-tracker heartbeat: pure rendering, the run gate, e2e stderr, banner guard."""

import subprocess
import sys

from itemeval import _tracker
from itemeval.generate._run import run_generate


def test_render_heartbeat_with_total():
    ctx = _tracker._RunContext(
        active=True,
        stage="generate",
        experiment_id="abcd1234",
        attempt=1,
        total=8,
        started=8,
        ended=4,
        errors=1,
        start_monotonic=0.0,
    )
    line = _tracker.render_heartbeat(ctx, now=4.0)  # 4 done in 4s -> 1/s -> 60/min
    assert line.startswith("[itemeval] generate")
    assert "exp abcd1234/a1" in line
    assert "4/8 (50%)" in line
    assert "60/min" in line
    assert "~4s left" in line  # 4 remaining at 1/s
    assert "1 errors" in line
    assert "4 in-flight" in line  # started 8 - ended 4


def test_render_heartbeat_no_total_no_rate():
    # No total -> "N done", no percent/ETA; <3 done -> no rate; no experiment label.
    ctx = _tracker._RunContext(active=True, stage="grade", started=1, ended=1, start_monotonic=0.0)
    line = _tracker.render_heartbeat(ctx, now=0.5)
    assert "[itemeval] grade" in line
    assert "1 done" in line
    assert "exp " not in line
    assert "/min" not in line
    assert "left" not in line
    assert "in-flight" not in line  # started == ended


def test_tracking_disabled_is_noop():
    with _tracker.tracking("generate", "x", 1, 8, enabled=False):
        assert _tracker._CTX.active is False
    assert _tracker._CTX.active is False


def test_tracking_enabled_activates_and_clears():
    with _tracker.tracking("generate", "exp1", 2, 8, enabled=True):
        assert _tracker._CTX.active is True
        assert _tracker._CTX.stage == "generate"
        assert _tracker._CTX.total == 8
        assert _tracker._CTX.attempt == 2
    assert _tracker._CTX.active is False  # always cleared on exit


def test_heartbeat_emits_to_stderr_on_mock_run(study, capsys):
    # conftest pins INSPECT_DISPLAY=none -> resolve_display(None) == "none" -> heartbeat on.
    cfg, prep = study
    run_generate(prep)
    err = capsys.readouterr().err
    assert "[itemeval] generate" in err
    assert "8/8" in err  # final forced line: 2 conditions x 2 dev items x 2 epochs
    assert "0 errors" in err


def test_no_duplicate_final_heartbeat_line(capsys, monkeypatch):
    # When the LAST sample's on_sample_end emits the terminal line (not throttled),
    # tracking()'s closing force-emit must NOT repeat it. Zero the throttle so every
    # on_sample_end emits, making the final sample's line unthrottled.
    import asyncio

    monkeypatch.setattr(_tracker, "_MIN_INTERVAL_S", 0.0)
    tracker = _tracker.LiveTracker()

    class _End:
        sample_id = "s"
        sample = type("S", (), {"error": None})()

    with _tracker.tracking("generate", "exp1", 1, 2, enabled=True):
        asyncio.run(tracker.on_sample_end(_End()))  # 1/2
        asyncio.run(tracker.on_sample_end(_End()))  # 2/2 — emits the terminal line
    err = capsys.readouterr().err
    assert err.count("2/2 (100%)") == 1  # closing line not duplicated


def test_no_heartbeat_when_display_not_silenced(study, capsys):
    cfg, prep = study
    run_generate(prep, display="plain")  # rich/plain carries its own liveness
    err = capsys.readouterr().err
    assert "[itemeval] generate" not in err


def test_no_hooks_banner_on_stdout_in_a_fresh_process():
    # The real regression net for the pre-latch: in a fresh process, importing the
    # tracker must NOT cause inspect's init_hooks() to print its "hooks enabled"
    # banner to STDOUT (it would corrupt a --json run). Without the pre-latch, the
    # explicit init_hooks() below would be the first call and would print the banner.
    code = (
        "import itemeval._tracker;"
        "from inspect_ai.hooks._startup import init_hooks; init_hooks();"
        "print('DONE')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "DONE" in r.stdout
    assert "hooks enabled" not in r.stdout
    assert "inspect_ai v" not in r.stdout


def test_render_heartbeat_batch_mode():
    # Batch mode: line is provider-paced — carries batch churn, drops the samples/sec ETA.
    ctx = _tracker._RunContext(
        active=True,
        stage="grade",
        experiment_id="abcd1234",
        attempt=1,
        total=40,
        ended=8,
        errors=0,
        start_monotonic=0.0,
        batch=True,
        batch_count=3,
        batch_pending=20,
        batch_oldest_age=125,
    )
    line = _tracker.render_heartbeat(ctx, now=30.0)
    assert line.startswith("[itemeval] grade · batch")
    assert "8/40 (20%)" in line  # committed sample progress, monotonic
    assert "3 batches" in line
    assert "20 pending" in line
    assert "oldest 2m" in line
    assert "/min" not in line and "left" not in line  # no throughput ETA in batch mode


def test_on_batch_status_updates_ctx_and_emits(capsys):
    import time
    from inspect_ai.model._providers.util.batch_log import BatchStatus

    with _tracker.tracking("grade", "exp1", 1, 40, enabled=True, batch=True):
        status = BatchStatus(
            batch_count=2,
            pending_requests=15,
            completed_requests=5,
            failed_requests=0,
            oldest_created_at=int(time.time()) - 60,
        )
        _tracker._on_batch_status(status)
        assert _tracker._CTX.batch_count == 2
        assert _tracker._CTX.batch_pending == 15
        assert _tracker._CTX.batch_oldest_age >= 55
    err = capsys.readouterr().err
    assert "[itemeval] grade · batch" in err
    assert "2 batches" in err and "15 pending" in err


def test_tracking_batch_registers_banner_and_restores_callbacks(capsys):
    import inspect_ai.model._providers.util.batch_log as bl

    with _tracker.tracking("generate", "exp1", 1, 8, enabled=True, batch=True):
        assert bl._batch_status_callback is _tracker._on_batch_status
        assert _tracker._CTX.batch is True
    assert bl._batch_status_callback is None  # handed back to inspect's default
    assert _tracker._CTX.batch is False
    err = capsys.readouterr().err
    assert "batch mode:" in err  # one-time expectation-setting banner


def test_no_batch_callback_registered_when_batch_off():
    import inspect_ai.model._providers.util.batch_log as bl

    with _tracker.tracking("generate", "exp1", 1, 8, enabled=True, batch=False):
        assert bl._batch_status_callback is None  # untouched on the non-batch path
        assert _tracker._CTX.batch is False


# --- straggler keepalive (timer-driven slow-cell liveness) ---


def test_render_stragglers_lists_over_threshold_slowest_first():
    now = 1000.0
    inflight = {
        "a": _tracker._Inflight("openrouter/anthropic/opus", "aime-17", now - 800),  # 13m
        "b": _tracker._Inflight("openrouter/openai/gpt", "aime-03", now - 240),  # 4m
        "c": _tracker._Inflight("openrouter/google/gemini", "aime-99", now - 30),  # under
    }
    block = _tracker.render_stragglers(
        inflight, now, stage="generate", last_end_monotonic=now - 180
    )
    assert block is not None
    lines = block.splitlines()
    assert lines[0].startswith("[itemeval] generate · no completion for 3m")
    assert "2 cell(s) in-flight >2m" in lines[0]  # the under-threshold cell excluded
    assert "openrouter/anthropic/opus · item aime-17 · 13m" in lines[1]  # slowest first
    assert "openrouter/openai/gpt · item aime-03 · 4m" in lines[2]
    assert "aime-99" not in block


def test_render_stragglers_none_when_all_under_threshold():
    now = 1000.0
    inflight = {"a": _tracker._Inflight("m", "i", now - 10)}
    assert _tracker.render_stragglers(inflight, now, stage="grade", last_end_monotonic=now) is None


def test_render_stragglers_caps_with_more_footer():
    now = 1000.0
    inflight = {str(i): _tracker._Inflight("m", f"i{i}", now - 300 - i) for i in range(13)}
    block = _tracker.render_stragglers(
        inflight, now, stage="generate", last_end_monotonic=now - 300, cap=10
    )
    lines = block.splitlines()
    assert len(lines) == 12  # 1 header + 10 cells + 1 footer
    assert lines[-1].endswith("+3 more")  # the 3 cells past the cap collapse to a footer


def test_render_stragglers_retry_annotation():
    now = 1000.0
    inflight = {
        "a": _tracker._Inflight("m", "i1", now - 300, attempt=2),  # retrying
        "b": _tracker._Inflight("m", "i2", now - 200, attempt=1),  # first attempt
    }
    block = _tracker.render_stragglers(
        inflight, now, stage="generate", last_end_monotonic=now - 300
    )
    lines = block.splitlines()
    assert "item i1" in lines[1] and "try 2" in lines[1]  # i1 older -> listed first
    assert "item i2" in lines[2] and "try" not in lines[2]


def test_straggler_due_only_when_active_and_quiet():
    ctx = _tracker._RunContext(active=True, last_emit=100.0)
    assert _tracker._straggler_due(ctx, 100.0 + _tracker._STRAGGLER_INTERVAL_S) is True
    assert _tracker._straggler_due(ctx, 100.0 + 5) is False  # a per-sample line just emitted
    ctx.active = False
    assert _tracker._straggler_due(ctx, 100.0 + 999) is False


def test_inflight_map_populated_and_cleared_by_hooks():
    import asyncio

    tracker = _tracker.LiveTracker()
    task_start = type("T", (), {"eval_id": "e1", "spec": type("Sp", (), {"model": "m/opus"})()})()
    start = type(
        "S", (), {"sample_id": "s1", "eval_id": "e1", "summary": type("Q", (), {"id": "aime-7"})()}
    )()
    attempt = type("A", (), {"sample_id": "s1", "attempt": 2})()
    end = type("E", (), {"sample_id": "s1", "sample": type("M", (), {"error": None})()})()

    with _tracker.tracking("generate", "exp1", 1, 4, enabled=True):
        asyncio.run(tracker.on_task_start(task_start))
        asyncio.run(tracker.on_sample_start(start))
        cell = _tracker._CTX.inflight["s1"]
        assert cell.model == "m/opus" and cell.item == "aime-7" and cell.attempt == 1
        asyncio.run(tracker.on_sample_attempt_start(attempt))
        assert _tracker._CTX.inflight["s1"].attempt == 2  # retry bumped
        asyncio.run(tracker.on_sample_end(end))
        assert "s1" not in _tracker._CTX.inflight  # cleared on completion
        assert _tracker._CTX.last_end_monotonic > 0  # advanced for the stall clock


def test_straggler_timer_skipped_in_batch_mode():
    import asyncio

    tracker = _tracker.LiveTracker()
    with _tracker.tracking("generate", "exp1", 1, 4, enabled=True, batch=True):
        asyncio.run(tracker.on_run_start(type("R", (), {})()))
        assert _tracker._CTX.straggler_task is None  # batch liveness is provider-paced
