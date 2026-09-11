"""Provider-neutral data transfer objects.

Every provider adapter converts its wire format into these, so nothing
downstream needs to know which vendor supplied a number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from app.models.enums import DataQuality, SentimentLabel


def _as_decimal(value: float | str | Decimal | None) -> Decimal | None:
    """Coerce to Decimal via str, so 0.1 stays 0.1 rather than 0.1000000000000000055."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_prices(obj, fields: tuple[str, ...]) -> None:
    """Normalise price-bearing attributes of a frozen dataclass to Decimal."""
    for name in fields:
        object.__setattr__(obj, name, _as_decimal(getattr(obj, name)))


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar."""

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adj_close: Decimal | None = None
    provider: str = ""
    quality: DataQuality = DataQuality.EOD
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        _coerce_prices(self, ("open", "high", "low", "close", "adj_close"))


@dataclass(frozen=True, slots=True)
class IntradayBar:
    symbol: str
    bar_timestamp: datetime
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    provider: str = ""
    quality: DataQuality = DataQuality.DELAYED

    def __post_init__(self) -> None:
        _coerce_prices(self, ("open", "high", "low", "close"))


@dataclass(frozen=True, slots=True)
class QuoteData:
    """A point-in-time price.

    `source_timestamp` is when the venue struck the price, NOT when we fetched
    it. Freshness checks depend on that distinction being honest.
    """

    symbol: str
    price: Decimal
    source_timestamp: datetime
    provider: str
    quality: DataQuality
    previous_close: Decimal | None = None
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    volume: int | None = None
    currency: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _coerce_prices(
            self, ("price", "previous_close", "day_open", "day_high", "day_low")
        )

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.source_timestamp).total_seconds()

    @property
    def change(self) -> Decimal | None:
        if self.previous_close is None:
            return None
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if not self.previous_close:
            return None
        return float((self.price - self.previous_close) / self.previous_close * 100)


@dataclass(frozen=True, slots=True)
class FundamentalsData:
    """Fundamentals. Every field is optional -- missing data stays missing."""

    symbol: str
    provider: str
    as_of_date: date
    period: str = "TTM"
    reported_at: datetime | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    eps: float | None = None
    revenue: float | None = None
    revenue_growth: float | None = None
    net_income: float | None = None
    profit_growth: float | None = None
    roe: float | None = None
    roce: float | None = None
    debt_to_equity: float | None = None
    free_cash_flow: float | None = None
    dividend_yield: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    earnings_growth: float | None = None
    market_cap: float | None = None
    raw: dict | None = None


@dataclass(frozen=True, slots=True)
class NewsItem:
    headline: str
    source: str
    published_at: datetime
    provider: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str | None = None
    url: str | None = None
    symbol: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityInfo:
    symbol: str
    name: str
    currency: str
    exchange_code: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
