"""Domain exceptions.

These map onto the fail-safe rules: when data cannot be trusted the system
raises rather than silently substituting a guess.
"""
from __future__ import annotations


class StockIntelError(Exception):
    """Base class for all application errors."""


# ------------------------------------------------------------- market data
class ProviderError(StockIntelError):
    """A market-data provider failed to return usable data."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True):
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


class ProviderRateLimited(ProviderError):
    def __init__(self, provider: str, message: str = "rate limited"):
        super().__init__(provider, message, retryable=True)


class ProviderUnavailable(ProviderError):
    """Network/policy level failure -- host unreachable or refused."""


class SymbolNotFound(ProviderError):
    def __init__(self, provider: str, symbol: str):
        super().__init__(provider, f"symbol not found: {symbol}", retryable=False)


class AllProvidersFailed(StockIntelError):
    def __init__(self, symbol: str, errors: dict[str, str]):
        self.symbol = symbol
        self.errors = errors
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        super().__init__(f"every provider failed for {symbol} -- {detail}")


# ------------------------------------------------------------------ quality
class DataValidationError(StockIntelError):
    """An incoming record failed integrity checks and was rejected."""


class StaleDataError(StockIntelError):
    """Data is too old to be treated as current. Never trade on this."""

    def __init__(self, symbol: str, age_seconds: float, limit_seconds: float):
        self.symbol = symbol
        self.age_seconds = age_seconds
        super().__init__(
            f"{symbol}: data is {age_seconds:.0f}s old (limit {limit_seconds:.0f}s)"
        )


class UnknownPriceError(StockIntelError):
    """No trustworthy price is available. No trade may be placed."""


# ----------------------------------------------------------------- ml/model
class ModelNotAvailable(StockIntelError):
    """No validated production model exists for this request."""


class InsufficientDataError(StockIntelError):
    """Not enough history to compute the requested quantity honestly."""


# -------------------------------------------------------------- risk/trade
class RiskRejection(StockIntelError):
    """A risk control refused an order. This is never bypassable."""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        super().__init__(f"risk rule '{rule}' rejected order: {message}")


class KillSwitchEngaged(StockIntelError):
    """Trading is globally halted."""


class BrokerError(StockIntelError):
    pass


class DuplicateOrderError(StockIntelError):
    pass


# ------------------------------------------------------------------- auth
class AuthError(StockIntelError):
    pass


class PermissionDenied(StockIntelError):
    pass
