"""Study output directory layout."""

from pathlib import Path


class StudyPaths:
    def __init__(self, study_dir: Path) -> None:
        self.study_dir = study_dir

    @property
    def items(self) -> Path:
        return self.study_dir / "items.parquet"

    @property
    def solutions(self) -> Path:
        return self.study_dir / "solutions.parquet"

    @property
    def gradings(self) -> Path:
        return self.study_dir / "gradings.parquet"

    @property
    def log_index(self) -> Path:
        return self.study_dir / "log_index.parquet"

    @property
    def ledger(self) -> Path:
        return self.study_dir / "ledger.parquet"

    @property
    def dataset_locks(self) -> Path:
        return self.study_dir / "dataset_locks.json"

    @property
    def model_locks(self) -> Path:
        return self.study_dir / "model_locks.json"

    @property
    def manifests_dir(self) -> Path:
        return self.study_dir / "manifests"

    @property
    def export_dir(self) -> Path:
        return self.study_dir / "export"

    def logs_stage_dir(self, stage: str) -> Path:
        # One dir per stage: all conditions of a stage run in a single inspect
        # eval (cross-condition parallelism), so they share a log_dir. inspect
        # names each task's log uniquely; readback is by condition_id columns.
        return self.study_dir / "logs" / stage

    def ensure(self) -> None:
        for d in (self.study_dir, self.manifests_dir, self.export_dir, self.study_dir / "logs"):
            d.mkdir(parents=True, exist_ok=True)
