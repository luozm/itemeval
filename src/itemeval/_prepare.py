"""PreparedStudy: everything a command needs, computed once per invocation."""

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from itemeval._config import PRICING_TABLE_UNIVERSE, ExperimentConfig
from itemeval._errors import BudgetError, ConfigError
from itemeval._item import Item
from itemeval._modelsample import ModelSampleResult, resolve_model_sample
from itemeval._templates import Template, rubric_registry, solver_registry
from itemeval.adapters._base import LoadedDataset, load_items
from itemeval.budget._policies import EffectivePlan, apply_items_limit, effective_plan
from itemeval.budget._pricing import (
    PricingTable,
    is_schema_stale,
    load_pricing,
    maybe_refresh_pricing,
    refresh_pricing,
)
from itemeval.budget._routing import active_native_routes, eligible_native_routes
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
    model_sample: ModelSampleResult | None = None  # set when solvers.sample drew the models
    pricing_refreshed: bool = False  # a live OpenRouter refresh ran during prepare
    policy_source: str = "config"  # "config" | "override" (a policy= argument won)
    # Native batch routing (budget.prefer_native_batch): {sampled_id: native_id}
    # applied this run (empty unless a batch plan + the knob + an eligible model
    # with a native key); the sampled id stays the scientific identity. The
    # companion list is eligible-but-keyless models, for the inert/why-not note.
    native_routes: dict[str, str] = field(default_factory=dict)
    native_routes_unavailable: list[str] = field(default_factory=list)


POLICY_CHOICES = ("dev", "full-interactive", "full-batch")


def _samples_pricing_table(config: ExperimentConfig) -> bool:
    """Whether this run draws its model facet from the `pricing-table` universe —
    the one consumer of roster metadata that hard-errors without it."""
    sample = config.solvers.sample
    return sample is not None and sample.universe == PRICING_TABLE_UNIVERSE


def prepare_study(
    config: ExperimentConfig,
    *,
    refresh_pricing_table: bool = False,
    policy: "str | None" = None,
) -> PreparedStudy:
    if policy is not None and policy not in POLICY_CHOICES:
        raise ConfigError(f"invalid policy {policy!r} (choose from {', '.join(POLICY_CHOICES)})")
    # Resolve + validate every template reference first: this is cheap, has no
    # side effects, and a bad reference must fail before any study dir is created.
    solvers = solver_registry(config)
    solver_templates = {name: solvers.get(name) for name in config.facets.prompt}
    rubric_templates: dict[str, Template] = {}
    if config.facets.grader:
        rubrics = rubric_registry(config)
        rubric_templates = {name: rubrics.get(name) for name in config.facets.rubric}

    paths = StudyPaths(config.study_dir)
    paths.ensure()
    datasets = load_items(config, paths.dataset_locks)

    items_all: list[Item] = []
    origins: dict[str, DatasetOrigin] = {}
    for ds in datasets:
        for item in ds.items:
            items_all.append(item)
            origins[item.id] = DatasetOrigin(dataset_id=ds.dataset_id, revision=ds.revision)

    plan = effective_plan(config.budget, config.facets.replications, policy=policy)
    items_effective = apply_items_limit(items_all, plan.items_limit)

    # Pricing is the roster source for model sampling, so resolve it (and the
    # sample, which mutates solvers.models) before grid expansion reads them.
    pricing_refreshed = False
    if refresh_pricing_table:
        pricing = refresh_pricing()  # explicit --refresh-pricing: hard refresh
        pricing_refreshed = True
    elif config.budget.pricing_path is not None:
        pricing = load_pricing(config.budget.pricing_path, config._input_base)  # pinned: as-is
    else:
        loaded = load_pricing(None, config._input_base)
        pricing = maybe_refresh_pricing(loaded, config.budget.pricing_max_age_days)
        # A pricing-table sample universe needs roster metadata (text_model, ...).
        # A cache written before those fields existed reads as fresh by age, so the
        # age-based refresh above can't detect it; refresh once on that schema
        # staleness so the universe isn't spuriously empty. Offline: fall through to
        # the actionable ConfigError in _build_universe.
        if _samples_pricing_table(config) and is_schema_stale(pricing):
            try:
                pricing = refresh_pricing()
            except BudgetError:
                pass
        # maybe_refresh returns the same object on a no-op; a new one means it refreshed.
        pricing_refreshed = pricing is not loaded

    model_sample = resolve_model_sample(config, pricing, paths.model_locks)
    grid = expand_grid(config, solver_templates, rubric_templates)
    # Native batch routing decided once here (resume-safe): solvers.models is now
    # final (post-sample). active = eligible gated by batch + the opt-in knob.
    native_routes = active_native_routes(config, plan)
    _, native_routes_unavailable = eligible_native_routes(config)
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
        model_sample=model_sample,
        pricing_refreshed=pricing_refreshed,
        policy_source="config" if policy is None else "override",
        native_routes=native_routes,
        native_routes_unavailable=native_routes_unavailable,
    )
