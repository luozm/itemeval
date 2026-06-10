"""Facet grid expansion: config -> generate/grade conditions with stable ids."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from itemeval._config import (
    ExperimentConfig,
    ModelConfigFacet,
    ReasoningEffort,
    SolversConfig,
)
from itemeval._errors import ConfigError
from itemeval._templates import Template, validate_template
from itemeval._util import drop_none
from itemeval.design._ids import make_condition_id, model_short

JUDGE_FORMAT_VERSION = 1  # bump when the packaged judge output-format suffix changes


class GenParams(BaseModel):
    """Resolved sampling params for one generate condition (facet over solver defaults)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    reasoning_tokens: int | None = None


def resolve_gen_params(solvers: SolversConfig, mc: ModelConfigFacet) -> GenParams:
    return GenParams(
        temperature=mc.temperature if mc.temperature is not None else solvers.temperature,
        top_p=mc.top_p if mc.top_p is not None else solvers.top_p,
        max_tokens=mc.max_tokens if mc.max_tokens is not None else solvers.max_tokens,
        seed=solvers.seed,
        reasoning_effort=mc.reasoning_effort,
        reasoning_tokens=mc.reasoning_tokens,
    )


class GenCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    slug: str
    model: str
    prompt_name: str
    prompt_hash: str  # 12 hex
    model_config_name: str
    gen_params: GenParams
    payload: dict


class GradeCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    slug: str
    kind: Literal["judge", "verifiable"]
    grader_name: str | None = None
    grader_model: str | None = None
    grader_max_tokens: int | None = None
    grader_reasoning_effort: ReasoningEffort | None = None
    rubric_name: str | None = None
    rubric_hash: str | None = None
    scorer: str | None = None
    payload: dict


class Grid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replications: int  # from facets (NOT policy-adjusted)
    generate: list[GenCondition]
    grade: list[GradeCondition]


def expand_generate_grid(
    config: ExperimentConfig, solver_templates: "dict[str, Template]"
) -> list[GenCondition]:
    conditions = []
    for model in config.solvers.models:
        for prompt_name in config.facets.prompt:
            template = solver_templates[prompt_name]
            validate_template(template, {"input"})
            for mc in config.facets.model_config_facet:
                params = resolve_gen_params(config.solvers, mc)
                payload = {
                    "kind": "generate",
                    "model": model,
                    "model_config": {
                        "name": mc.name,
                        "params": drop_none(params.model_dump()),
                    },
                    "prompt": {"name": prompt_name, "hash": template.hash12},
                }
                cond_id, slug = make_condition_id(
                    [model_short(model), prompt_name, mc.name], payload
                )
                conditions.append(
                    GenCondition(
                        id=cond_id,
                        slug=slug,
                        model=model,
                        prompt_name=prompt_name,
                        prompt_hash=template.hash12,
                        model_config_name=mc.name,
                        gen_params=params,
                        payload=payload,
                    )
                )
    return conditions


def expand_grade_grid(
    config: ExperimentConfig, rubric_templates: "dict[str, Template]"
) -> list[GradeCondition]:
    conditions = []
    if config.facets.scorer is not None:
        scorer = config.facets.scorer
        payload = {"kind": "grade", "scorer": scorer}
        cond_id, slug = make_condition_id(["scorer", scorer], payload)
        conditions.append(
            GradeCondition(id=cond_id, slug=slug, kind="verifiable", scorer=scorer, payload=payload)
        )
    for grader_name in config.facets.grader:
        spec = config.grader_spec(grader_name)
        for rubric_name in config.facets.rubric:
            template = rubric_templates[rubric_name]
            validate_template(template, {"input", "solution"})
            payload = {
                "kind": "grade",
                "grader": drop_none(
                    {
                        "name": grader_name,
                        "model": spec.model,
                        "temperature": 0.0,  # pinned for v0.1 (ROADMAP M3)
                        "max_tokens": spec.max_tokens,
                        "reasoning_effort": spec.reasoning_effort,
                    }
                ),
                "rubric": {"name": rubric_name, "hash": template.hash12},
                "format": JUDGE_FORMAT_VERSION,
            }
            cond_id, slug = make_condition_id([grader_name, rubric_name], payload)
            conditions.append(
                GradeCondition(
                    id=cond_id,
                    slug=slug,
                    kind="judge",
                    grader_name=grader_name,
                    grader_model=spec.model,
                    grader_max_tokens=spec.max_tokens,
                    grader_reasoning_effort=spec.reasoning_effort,
                    rubric_name=rubric_name,
                    rubric_hash=template.hash12,
                    payload=payload,
                )
            )
    return conditions


def expand_grid(
    config: ExperimentConfig,
    solver_templates: "dict[str, Template]",
    rubric_templates: "dict[str, Template]",
) -> Grid:
    generate = expand_generate_grid(config, solver_templates)
    grade = expand_grade_grid(config, rubric_templates)
    ids = [c.id for c in generate] + [c.id for c in grade]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ConfigError(f"duplicate condition ids after grid expansion: {dupes}")
    return Grid(replications=config.facets.replications, generate=generate, grade=grade)
