"""itemeval: item-level LLM evaluation over any API, with built-in budget control."""

from importlib.metadata import version

from itemeval._config import ExperimentConfig, load_config
from itemeval._item import Item

__version__ = version("itemeval")
__all__ = ["ExperimentConfig", "Item", "__version__", "load_config"]
