"""PreparedStudy: everything a command needs, computed once per invocation."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from itemeval._config import ExperimentConfig
from itemeval._item import Item
from itemeval._templates import Template, rubric_registry, solver_registry
from itemeval.adapters._base import LoadedDataset, load_items
from itemeval.budget._policies import EffectivePlan, apply_items_limit, effective_plan
from itemeval.budget._pricing import PricingTable, load_pricing, refresh_pricing
from itemeval.design._grid import Grid, expand_grid
from itemeval.store._layout import StudyPaths


class DatasetOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    revision: str


@dataclass
class PreparedStudy:
    config: ExperimentConfig
    paths: StudyPaths
    datasets: list[LoadedDataset]
    items_all: list[Item]  # concatenated, config order
    items_effective: list[Item]  # after plan.items_limit
    items_by_id: dict[str, Item]
    origins: dict[str, DatasetOrigin]  # item_id -> origin
    solver_templates: dict[str, Template]
    rubric_templates: dict[str, Template]  # {} when no graders configured
    grid: Grid
    plan: EffectivePlan
    pricing: PricingTable


def prepare_study(
    config: ExperimentConfig, *, refresh_pricing_table: bool = False
) -> PreparedStudy:
    paths = StudyPaths(config.study_dir)
    paths.ensure()
    datasets = load_items(config, paths.dataset_locks)

    items_all: list[Item] = []
    origins: dict[str, DatasetOrigin] = {}
    for ds in datasets:
        for item in ds.items:
            items_all.append(item)
            origins[item.id] = DatasetOrigin(dataset_id=ds.dataset_id, revision=ds.revision)

    plan = effective_plan(config.budget, config.facets.replications)
    items_effective = apply_items_limit(items_all, plan.items_limit)

    solvers = solver_registry(config)
    solver_templates = {name: solvers.get(name) for name in config.facets.prompt}
    rubric_templates: dict[str, Template] = {}
    if config.facets.grader:
        rubrics = rubric_registry(config)
        rubric_templates = {name: rubrics.get(name) for name in config.facets.rubric}

    grid = expand_grid(config, solver_templates, rubric_templates)
    pricing = (
        refresh_pricing()
        if refresh_pricing_table
        else load_pricing(config.budget.pricing_path, config.base_dir)
    )
    return PreparedStudy(
        config=config,
        paths=paths,
        datasets=datasets,
        items_all=items_all,
        items_effective=items_effective,
        items_by_id={it.id: it for it in items_all},
        origins=origins,
        solver_templates=solver_templates,
        rubric_templates=rubric_templates,
        grid=grid,
        plan=plan,
        pricing=pricing,
    )
