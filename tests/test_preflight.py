"""Pre-flight model probe (preflight-check W2). Hermetic: mock ids probe ok with
no network; failures are injected through a model_factory, so no real provider is
ever called."""

import json

from itemeval import cli
from itemeval._mockmodels import resolve_model
from itemeval._preflight import preflight_study


class FakeHTTPError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status_code = status


def _factory(failures: dict):
    """Resolve mock ids normally; raise the injected error for named ids."""

    def factory(model, stage, model_args=None):
        if model in failures:
            raise failures[model]
        return resolve_model(model, stage, model_args)

    return factory


def test_all_mock_models_probe_ok(study):
    _, prep = study
    report = preflight_study(prep)
    # Grid is mockllm/solver-a, mockllm/solver-b (generate) + mockllm/judge (grade).
    assert {m.id for m in report.models} == {
        "mockllm/solver-a",
        "mockllm/solver-b",
        "mockllm/judge",
    }
    assert report.ok == 3 and report.dead == 0 and report.unverified == 0
    assert all(m.status == "ok" for m in report.models)
    assert not report.has_dead


def test_dead_and_unverified_mapping(study):
    _, prep = study
    failures = {
        "mockllm/solver-a": FakeHTTPError(404, "model not found"),
        "mockllm/solver-b": FakeHTTPError(429, "rate limited"),
    }
    report = preflight_study(prep, model_factory=_factory(failures))
    by_id = {m.id: m for m in report.models}
    assert by_id["mockllm/solver-a"].status == "dead"
    assert by_id["mockllm/solver-a"].http_status == 404
    assert by_id["mockllm/solver-b"].status == "unverified"  # transient, not accused
    assert by_id["mockllm/judge"].status == "ok"
    assert report.ok == 1 and report.dead == 1 and report.unverified == 1
    assert report.has_dead


def test_cli_preflight_all_ok_exit_0(tmp_path, offline_adapter, capsys):
    from conftest import write_study_files

    config = write_study_files(tmp_path)
    assert cli.main(["preflight", str(config)]) == 0
    out = capsys.readouterr().out
    assert "preflight:" in out and "3 ok" in out


def test_cli_preflight_json_shape(tmp_path, offline_adapter, capsys):
    from conftest import write_study_files

    config = write_study_files(tmp_path)
    assert cli.main(["preflight", str(config), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] == 3 and doc["dead"] == 0
    assert {m["id"] for m in doc["models"]} == {
        "mockllm/solver-a",
        "mockllm/solver-b",
        "mockllm/judge",
    }
