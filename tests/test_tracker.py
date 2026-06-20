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
