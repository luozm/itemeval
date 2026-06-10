import json

import pytest

from itemeval import cli
from conftest import write_study_files


def test_unknown_config_exits_2(tmp_path, capsys):
    assert cli.main(["status", str(tmp_path / "missing.yaml")]) == 2
    assert "error" in capsys.readouterr().err


def test_missing_subcommand_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_status_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["status", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["study"] == "tstudy"
    assert len(report["generate"]) == 2


def test_estimate_plain_and_json(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    assert cli.main(["estimate", str(config)]) == 0
    out = capsys.readouterr().out
    assert "GENERATE" in out and "GRADE" in out and "total projected" in out
    assert cli.main(["estimate", str(config), "--json"]) == 0
    est = json.loads(capsys.readouterr().out)
    assert est["generate"]["calls"] == 8


def test_generate_gate_declines_non_interactively(tmp_path, offline_adapter, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    config_yaml = write_study_files(tmp_path).read_text()
    config = tmp_path / "gated.yaml"
    config.write_text(config_yaml.replace("confirm_above_usd: 100", "confirm_above_usd: 0.0"))
    assert cli.main(["generate", str(config)]) == 3
    assert "confirm" in capsys.readouterr().err
    # --yes overrides
    assert cli.main(["generate", str(config), "--yes"]) == 0


@pytest.mark.parametrize(
    "stage, module, result_kwargs",
    [
        ("generate", "itemeval.generate._run", {"rows_written": 0, "total_usd": 0.0}),
        (
            "grade",
            "itemeval.grade._run",
            {"rows_written": 0, "parse_failures": 0, "total_usd": 0.0},
        ),
    ],
)
def test_display_defaults_to_live_and_forwards_override(
    tmp_path, offline_adapter, monkeypatch, stage, module, result_kwargs
):
    """Omitting --display forwards None (inspect's own live default), not 'none';
    an explicit value is forwarded verbatim. Holds for both generate and grade."""
    import importlib

    run_mod = importlib.import_module(module)
    result_cls = run_mod.GenerateResult if stage == "generate" else run_mod.GradeResult
    fn_name = f"run_{stage}"
    captured = {}

    def fake_run(prep, **kwargs):
        captured["display"] = kwargs.get("display", "MISSING")
        return result_cls(
            run_id="r",
            study=prep.config.study,
            conditions=[],
            manifest_path="m",
            **result_kwargs,
        )

    monkeypatch.setattr(run_mod, fn_name, fake_run)
    config = write_study_files(tmp_path)

    assert cli.main([stage, str(config), "--yes"]) == 0
    assert captured["display"] is None  # -> inspect renders live progress

    assert cli.main([stage, str(config), "--yes", "--display", "plain"]) == 0
    assert captured["display"] == "plain"


def test_missing_template_exits_2(tmp_path, offline_adapter, capsys):
    config = write_study_files(tmp_path)
    (tmp_path / "prompts" / "solver" / "minimal.md").unlink()
    assert cli.main(["status", str(config)]) == 2
    assert "template" in capsys.readouterr().err


def test_init_creates_runnable_study(tmp_path, offline_adapter, capsys):
    target = tmp_path / "mystudy"
    assert cli.main(["init", str(target)]) == 0
    cfg_path = target / "config.yaml"
    assert cfg_path.is_file()
    text = cfg_path.read_text()
    assert "study: mystudy" in text and "builtin:standard" in text
    assert not (target / "prompts").exists()  # no local templates by default
    capsys.readouterr()
    # the scaffolded study resolves built-in templates and runs with zero local files
    assert cli.main(["status", str(cfg_path)]) == 0


def test_init_with_templates_copies_local(tmp_path):
    target = tmp_path / "ej"
    assert cli.main(["init", str(target), "--with-templates"]) == 0
    assert (target / "prompts" / "solver" / "minimal.md").is_file()
    assert (target / "prompts" / "solver" / "standard.md").is_file()
    assert (target / "rubrics" / "standard.md").is_file()
    lines = (target / "config.yaml").read_text().splitlines()
    prompt_line = next(line for line in lines if line.lstrip().startswith("prompt:"))
    rubric_line = next(line for line in lines if line.lstrip().startswith("rubric:"))
    assert "builtin:" not in prompt_line and "[minimal, standard]" in prompt_line
    assert "builtin:" not in rubric_line and "[standard]" in rubric_line


def test_init_refuses_overwrite(tmp_path, capsys):
    target = tmp_path / "x"
    assert cli.main(["init", str(target)]) == 0
    assert cli.main(["init", str(target)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["init", str(target), "--force"]) == 0
