"""Experiment identity: a deterministic, content-derived run identity.

`experiment_id` is the stable successor to the per-invocation `run_id`. It is
derived from the **semantic** config — identity-bearing fields only, re-parsed
through the validated pydantic model (never raw bytes) — so comments, whitespace,
key order, and pure execution/cost knobs never change identity. Two consequences:

- a **recovery re-run** of an unchanged config gets the **same** `experiment_id`,
  so its attempts are linkable and the data converges (content keys already do);
- a genuine design edit (datasets, models, facets, seeds, templates, scorer)
  changes the digest, so it **forks** into a new experiment (Choice A).

`attempt` counts prior top-level manifests for an `experiment_id`; the
`invocation_handle` `f"{experiment_id}.a{attempt}"` is the unique string a
manifest filename and `.eval` metadata need (the successor to the old `run_id`
string), while the stores carry `experiment_id` + `attempt` as separate columns.

Shares the *technique* (normalize-through-pydantic, never raw dict/bytes) with
`_modelsample`'s lock-spec check, but a different *scope* (the whole config vs the
`solvers.sample` spec) — so the two apply it independently; nothing is lifted.
"""

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from itemeval._util import canonical_json, sha256_hex

if TYPE_CHECKING:
    from itemeval._config import ExperimentConfig
    from itemeval.store._layout import StudyPaths

# Top-level config fields excluded from the identity digest: pure path / cache /
# cost-and-policy knobs that don't change the scientific design. Excluding
# `budget` is what makes a `dev`→`full` policy change *growth*, not a fork
# (Choice A): a bigger run of the same experiment, recovered into the same id.
_NON_IDENTITY_TOP = ("output_dir", "prompts_dir", "rubrics_dir", "cache", "budget")
# Nested pure-optimization / robustness pass-throughs — they never enter condition
# ids either, so a routing/cache-marker pin or a request timeout added between runs
# is not a new experiment.
_NON_IDENTITY_SOLVERS = ("provider_routing", "cache_prompt", "attempt_timeout")
_NON_IDENTITY_GRADER = ("provider_routing", "attempt_timeout")


def normalized_config_digest(config: "ExperimentConfig") -> str:
    """SHA-256 (hex) over the identity-bearing config, validated-model-dumped to
    canonical JSON. Invariant to comments / whitespace / key order and the
    execution knobs enumerated above; changes on any real design edit."""
    payload = config.model_dump(mode="json", by_alias=True)
    for key in _NON_IDENTITY_TOP:
        payload.pop(key, None)
    solvers = payload.get("solvers")
    if isinstance(solvers, dict):
        for key in _NON_IDENTITY_SOLVERS:
            solvers.pop(key, None)
    graders = payload.get("graders")
    if isinstance(graders, dict):
        for spec in graders.values():
            if isinstance(spec, dict):
                for key in _NON_IDENTITY_GRADER:
                    spec.pop(key, None)
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def experiment_id(config: "ExperimentConfig", stage: str, *, salt: "str | None" = None) -> str:
    """`sha256(config_digest : study : stage)[:12]` — deterministic, no
    wall-clock/uuid. Stage-scoped (generate and grade get distinct ids, like the
    old per-stage run_id). `salt` forces a fresh id (the `--new-run` escape)."""
    digest = config.config_sha256 or normalized_config_digest(config)
    base = f"{digest}:{config.study}:{stage}"
    if salt:
        base = f"{base}:{salt}"
    return sha256_hex(base.encode("utf-8"))[:12]


def invocation_handle(experiment_id: str, attempt: int) -> str:
    """The unique per-attempt string (manifest basename, `.eval` `itemeval_run_id`
    metadata). `experiment_id` is hex (never contains `.a`), so a handle splits
    back unambiguously where needed."""
    return f"{experiment_id}.a{attempt}"


def _count_attempts(manifests_dir: Path, experiment_id: str) -> int:
    """Top-level manifests already recorded for this experiment. **Non-recursive**
    (`glob`, not `rglob`, and the pattern has no `/`) so W3's `experiments/`
    rollup subdir is never miscounted as an attempt."""
    if not manifests_dir.is_dir():
        return 0
    return len(list(manifests_dir.glob(f"{experiment_id}.a*.json")))


class RunIdentity(NamedTuple):
    experiment_id: str
    attempt: int
    run_kind: str  # "recovery" (a prior manifest exists) | "new"

    @property
    def handle(self) -> str:
        return invocation_handle(self.experiment_id, self.attempt)


def resolve_identity(
    config: "ExperimentConfig",
    paths: "StudyPaths",
    stage: str,
    *,
    new_run: bool = False,
) -> RunIdentity:
    """Identity for a run: `experiment_id` + the next `attempt`, plus whether this
    is a **recovery** of an existing experiment (a manifest already exists for the
    id) or a **new** one. `--new-run` salts a fresh id (always new, attempt 1)."""
    eid = experiment_id(config, stage, salt=uuid.uuid4().hex[:8] if new_run else None)
    prior = _count_attempts(paths.manifests_dir, eid)
    return RunIdentity(
        experiment_id=eid,
        attempt=prior + 1,
        run_kind="recovery" if prior else "new",
    )
