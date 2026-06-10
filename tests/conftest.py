"""Shared fixtures: hermetic inspect env + offline study factory (no network)."""

from pathlib import Path

import pytest

from itemeval.adapters._base import LoadedDataset
from itemeval.adapters._hf import _record_to_item

FAKE_REVISION = "fakerev0000000000000000000000000000000000"

FAKE_RECORDS = [
    {
        "problem_idx": str(i),
        "problem": f"Compute {i} + {i}. Explain briefly.",
        "points": 7,
        "grading_scheme": [{"desc": "full credit", "points": 7}],
        "sample_solution": f"ANSWER: {2 * i}",
    }
    for i in (1, 2, 3)
]

TEST_CONFIG_YAML = """\
study: tstudy
output_dir: studies
prompts_dir: prompts
rubrics_dir: rubrics
benchmark:
  adapter: hf
  datasets:
    - id: fake/ds
  mapping:
    id: problem_idx
    input: problem
    target: sample_solution
    grading_scheme: grading_scheme
    metadata: [points]
solvers:
  models: [mockllm/solver-a, mockllm/solver-b]
  temperature: 0.3
  max_tokens: 256
facets:
  prompt: [minimal]
  grader: [judge]
  rubric: [standard]
  replications: 2
graders:
  judge:
    model: mockllm/judge
    max_tokens: 256
budget:
  policy: dev
  confirm_above_usd: 100
"""

MINIMAL_PROMPT = 'Solve:\n\n{input}\n\nEnd with "ANSWER: <answer>".\n'
STANDARD_RUBRIC = (
    "Grade this.\n\nProblem:\n{input}\n\nScheme:\n{grading_scheme}\n\n"
    "Reference:\n{target}\n\nCandidate:\n{solution}\n"
)


@pytest.fixture(autouse=True)
def _inspect_hermetic_env(tmp_path, monkeypatch):
    monkeypatch.setenv("INSPECT_CACHE_DIR", str(tmp_path / "inspect_cache"))
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "inspect_logs"))
    monkeypatch.delenv("INSPECT_EVAL_MODEL", raising=False)
    monkeypatch.setenv("ITEMEVAL_PRICING_PATH", str(tmp_path / "no_user_pricing.json"))
    # Outputs anchor to work_dir (CWD); chdir into tmp_path so every test's study
    # tree lands in its own sandbox, never the repo root.
    monkeypatch.chdir(tmp_path)


class FakeHFAdapter:
    """Offline stand-in for HFAdapter; reuses the real record->Item mapping."""

    def __init__(self, records=None, revision=FAKE_REVISION):
        self.records = FAKE_RECORDS if records is None else records
        self.revision = revision

    def resolve_revision(self, spec) -> str:
        return self.revision

    def load(self, spec, mapping, revision) -> LoadedDataset:
        records = self.records
        if spec.limit is not None:
            records = records[: spec.limit]
        items = [_record_to_item(rec, idx, mapping, spec.id) for idx, rec in enumerate(records)]
        return LoadedDataset(
            dataset_id=spec.id,
            adapter="hf",
            split=spec.split,
            name=spec.name,
            revision_requested=spec.revision,
            revision=revision,
            items=items,
        )


def write_study_files(root: Path, config_yaml: str = TEST_CONFIG_YAML) -> Path:
    """Write a config + templates under root; returns the config path."""
    (root / "prompts" / "solver").mkdir(parents=True, exist_ok=True)
    (root / "rubrics").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "solver" / "minimal.md").write_text(MINIMAL_PROMPT)
    (root / "rubrics" / "standard.md").write_text(STANDARD_RUBRIC)
    config_path = root / "config.yaml"
    config_path.write_text(config_yaml)
    return config_path


@pytest.fixture()
def offline_adapter(monkeypatch):
    """Route all 'hf' adapter use to the offline fake; lock-file logic stays real."""
    from itemeval.adapters import _base

    adapter = FakeHFAdapter()
    monkeypatch.setattr(_base, "get_adapter", lambda name: adapter)
    return adapter


@pytest.fixture()
def study(tmp_path, offline_adapter):
    """Loaded config + PreparedStudy over the offline fake dataset."""
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    config_path = write_study_files(tmp_path)
    cfg = load_config(config_path)
    return cfg, prepare_study(cfg)
