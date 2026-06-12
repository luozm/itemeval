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


class BudgetExceededError(ItemevalError):
    """Projected spend exceeds a cap (`max_usd=` argument or budget.max_usd).

    Raised by run_generate/run_grade BEFORE any API call. The Python surface
    never prompts (UX-PATTERNS Law 3) — the parameter is the consent.
    """
