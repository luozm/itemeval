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
    split_prompt: bool = False  # render prompt as system(shared head)+user(item)
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
    rubric_hash: str | None = None  # grade-template hash (when materializing, the grade template)
    scorer: str | None = None
    split_rubric: bool = False  # render rubric as system(shared)+user(solution)
    # Two-stage materialization (None unless this rubric materializes per-item):
    materialize_model: str | None = None
    materialize_max_tokens: int | None = None
    materialize_reasoning_effort: ReasoningEffort | None = None
    build_template_hash: str | None = None  # 12 hex; the materialize (build) template
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
                if config.solvers.split_prompt:
                    # Only present when enabled so pre-existing condition ids
                    # are unchanged for the default single-message layout.
                    payload["layout"] = "split"
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
                        split_prompt=config.solvers.split_prompt,
                        payload=payload,
                    )
                )
    return conditions


def expand_grade_grid(
    config: ExperimentConfig,
    rubric_templates: "dict[str, Template]",
    build_templates: "dict[str, Template] | None" = None,
) -> list[GradeCondition]:
    build_templates = build_templates or {}
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
            rspec = config.rubric_spec(rubric_name)
            if rspec is None:
                validate_template(template, {"input", "solution"})
                build = None
            else:
                # Materializing rubric: the grade template receives {rubric}; the
                # build template renders the item's reference only (no {solution}).
                build = build_templates[rubric_name]
                validate_template(template, {"input", "solution", "rubric"})
                validate_template(build, {"input"})
                if "{solution}" in build.text:
                    raise ConfigError(
                        f"materialize template '{build.name}' must not reference "
                        "{solution}: the candidate solution does not exist at "
                        "materialization time"
                    )
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
            if build is not None:
                # Only present when materializing so pre-existing condition ids
                # are unchanged for plain rubrics. The materialized rubric is
                # per-item (recorded in the store), so the id carries the *spec*
                # — materializer model + build-template hash — not the outputs.
                payload["materialize"] = {
                    "model": rspec.materialize.model,
                    "build_hash": build.hash12,
                }
            if spec.split_rubric:
                # Only present when enabled so pre-existing condition ids are
                # unchanged for the default single-message layout.
                payload["layout"] = "split"
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
                    split_rubric=spec.split_rubric,
                    materialize_model=rspec.materialize.model if rspec else None,
                    materialize_max_tokens=rspec.materialize.max_tokens if rspec else None,
                    materialize_reasoning_effort=(
                        rspec.materialize.reasoning_effort if rspec else None
                    ),
                    build_template_hash=build.hash12 if build else None,
                    payload=payload,
                )
            )
    return conditions


def expand_grid(
    config: ExperimentConfig,
    solver_templates: "dict[str, Template]",
    rubric_templates: "dict[str, Template]",
    build_templates: "dict[str, Template] | None" = None,
) -> Grid:
    generate = expand_generate_grid(config, solver_templates)
    grade = expand_grade_grid(config, rubric_templates, build_templates)
    ids = [c.id for c in generate] + [c.id for c in grade]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ConfigError(f"duplicate condition ids after grid expansion: {dupes}")
    return Grid(replications=config.facets.replications, generate=generate, grade=grade)
