"""Exception hierarchy for itemeval."""


class ItemevalError(Exception):
    """Base class for all itemeval errors."""


class ConfigError(ItemevalError):
    """YAML shape/validation failures and bad config references."""


class AdapterError(ItemevalError):
    """Dataset load or field-mapping failures."""


class TemplateError(ItemevalError):
    """Missing template file or required placeholder."""


class StoreError(ItemevalError):
    """Parquet schema or IO problems."""


class BudgetError(ItemevalError):
    """Pricing refresh or estimator failures."""
