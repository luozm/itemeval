"""itemeval: item-level LLM evaluation over any API, with built-in budget control.

Two equivalent ways to drive a study:

CLI        itemeval estimate|generate|grade|export|status CONFIG
Python     cfg  = load_config("configs/my_study.yaml")
           prep = prepare_study(cfg)
           estimate_study(prep)          # projected $ per stage
           run_generate(prep)            # stage 1 -> solutions store
           run_grade(prep)               # stage 2 -> gradings store
           export_study(cfg)             # long-format parquet + CSV + ledger
           build_status(cfg, prep)       # grid completion report

The Python pipeline functions do NOT apply the budget confirmation gate (a
CLI feature) — compare `estimate_study(...)` totals against your own
threshold before paid runs.
"""

from importlib import import_module
from importlib.metadata import version

from itemeval._config import ExperimentConfig, load_config
from itemeval._item import Item

__version__ = version("itemeval")

# Pipeline functions resolve lazily (PEP 562) so `import itemeval` stays
# light: eager imports here would pull inspect_ai/pandas into every CLI start.
_LAZY = {
    "prepare_study": ("itemeval._prepare", "prepare_study"),
    "estimate_study": ("itemeval.budget._estimator", "estimate_study"),
    "run_generate": ("itemeval.generate._run", "run_generate"),
    "run_grade": ("itemeval.grade._run", "run_grade"),
    "export_study": ("itemeval.store._export", "export_study"),
    "build_status": ("itemeval._status", "build_status"),
}

__all__ = [
    "ExperimentConfig",
    "Item",
    "__version__",
    "build_status",
    "estimate_study",
    "export_study",
    "load_config",
    "prepare_study",
    "run_generate",
    "run_grade",
]


def __getattr__(name: str):
    if name in _LAZY:
        module_name, attr = _LAZY[name]
        return getattr(import_module(module_name), attr)
    raise AttributeError(f"module 'itemeval' has no attribute {name!r}")


def __dir__() -> "list[str]":
    return sorted(__all__)
