"""Pre-flight model probe (preflight-check): one ~1-token call per distinct model
to surface roster health before a paid run.

A dead model (404 EOL, bad auth) otherwise isn't caught until it fails mid-paid-
run and floods the log. This probes each distinct execution model in the grid and
reports `N ok / M dead / K unverified` so the user fixes the roster first. It is a
deliberately-invoked command (like `estimate`): invoking it is the consent to its
sub-cent spend, so it never spends model money a user did not ask for, and it adds
no per-run latency to `generate`/`grade`.

Boundary (DEVELOPMENT.md): an orchestrator-tier module — it calls inspect's
published `Model.generate` directly (wrap, don't fork), writing no `.eval` log, so
it cannot pollute the harvest path.
"""

from typing import TYPE_CHECKING, Callable, Literal

import anyio
from pydantic import BaseModel, ConfigDict

from itemeval._classify import classify_error, http_status

if TYPE_CHECKING:
    from inspect_ai.model import Model

    from itemeval._prepare import PreparedStudy

ProbeStatus = Literal["ok", "dead", "unverified"]

# Bound on concurrent probes — a roster is usually < 50 distinct models; this
# keeps a large sampled roster from opening one connection per model at once.
_MAX_CONCURRENCY = 8

ModelFactory = Callable[[str, str, "dict | None"], "Model"]


class PreflightModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: ProbeStatus
    detail: "str | None" = None  # exception summary when not ok
    http_status: "int | None" = None


class PreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study: str
    models: list[PreflightModel]
    # Counts (Law 6: a fact in the line is a field) — derivable from `models`,
    # surfaced explicitly so an agent reads them without re-aggregating.
    ok: int
    dead: int
    unverified: int

    @property
    def has_dead(self) -> bool:
        return self.dead > 0


def _probe_targets(prep: "PreparedStudy") -> "list[tuple[str, str]]":
    """(execution_id, stage) per distinct model in the grid, in first-seen order.

    Solver models (generate) + judge models (grade); verifiable conditions have
    no model. Native batch routing is applied so the probe hits the id that will
    actually run. The materializer model is out of scope (probe the roster, not
    every auxiliary call)."""
    seen: dict[str, str] = {}
    for c in prep.grid.generate:
        exec_id = prep.native_routes.get(c.model, c.model)
        seen.setdefault(exec_id, "generate")
    for c in prep.grid.grade:
        if c.grader_model:
            exec_id = prep.native_routes.get(c.grader_model, c.grader_model)
            seen.setdefault(exec_id, "grade")
    return list(seen.items())


async def _probe_one(exec_id: str, stage: str, factory: ModelFactory) -> PreflightModel:
    from inspect_ai.model import GenerateConfig

    try:
        model = factory(exec_id, stage, None)
        # max_retries=0: a terminal failure surfaces immediately (no retry storm
        # during a probe); max_tokens=1: cheapest possible liveness call.
        await model.generate("ping", config=GenerateConfig(max_tokens=1, max_retries=0))
        return PreflightModel(id=exec_id, status="ok")
    except Exception as e:  # noqa: BLE001 — any provider/SDK error is a probe result
        cls = classify_error(e)
        detail = f"{type(e).__name__}: {e}".strip()
        return PreflightModel(
            id=exec_id,
            status="dead" if cls == "terminal" else "unverified",
            detail=detail[:300],
            http_status=http_status(e),
        )


async def _probe_all(
    targets: "list[tuple[str, str]]", factory: ModelFactory
) -> list[PreflightModel]:
    results: "list[PreflightModel | None]" = [None] * len(targets)
    limiter = anyio.CapacityLimiter(_MAX_CONCURRENCY)

    async def run(i: int, exec_id: str, stage: str) -> None:
        async with limiter:
            results[i] = await _probe_one(exec_id, stage, factory)

    async with anyio.create_task_group() as tg:
        for i, (exec_id, stage) in enumerate(targets):
            tg.start_soon(run, i, exec_id, stage)
    return [r for r in results if r is not None]


def preflight_study(
    prep: "PreparedStudy", *, model_factory: "ModelFactory | None" = None
) -> PreflightReport:
    """Probe every distinct model in the grid with a ~1-token call and report
    roster health. Never prompts (Law 3 — a library takes consent by being
    called). `model_factory` overrides model resolution (tests inject failures)."""
    from itemeval._mockmodels import resolve_model

    factory = model_factory or resolve_model
    targets = _probe_targets(prep)
    models = anyio.run(_probe_all, targets, factory) if targets else []
    return PreflightReport(
        study=prep.config.study,
        models=models,
        ok=sum(1 for m in models if m.status == "ok"),
        dead=sum(1 for m in models if m.status == "dead"),
        unverified=sum(1 for m in models if m.status == "unverified"),
    )
