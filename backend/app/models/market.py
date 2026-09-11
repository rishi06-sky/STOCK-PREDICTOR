"""Reference data and market price history."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum as SAEnum,
    Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import AssetType, DataQuality

# Prices are stored as NUMERIC, not float: binary floating point cannot
# represent decimal money exactly and the error compounds across P&L maths.
Price = Numeric(20, 6)


class Market(Base, TimestampMixin):
    """A national market, e.g. India or United States."""

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    exchanges: Mapped[list["Exchange"]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )


class Exchange(Base, TimestampMixin):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Local-time trading session, interpreted in the parent market's timezone.
    open_time: Mapped[str] = mapped_column(String(8), nullable=False)   # "09:15"
    close_time: Mapped[str] = mapped_column(String(8), nullable=False)  # "15:30"
    # Provider symbol decoration, e.g. ".NS" for NSE on Yahoo Finance.
    yahoo_suffix: Mapped[str | None] = mapped_column(String(8))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    market: Mapped[Market] = relationship(back_populates="exchanges")
    securities: Mapped[list["Security"]] = relationship(back_populates="exchange")


class Security(Base, TimestampMixin):
    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_securities_exchange_id_symbol"),
        Index("ix_securities_active_type", "is_active", "asset_type"),
        Index("ix_securities_sector", "sector"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(
        SAEnum(AssetType, native_enum=False, length=16), default=AssetType.EQUITY, nullable=False
    )
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(16), index=True)
    # Provider-specific symbol overrides, e.g. {"yahoo": "RELIANCE.NS"}.
    provider_symbols: Mapped[dict | None] = mapped_column(JSONB)
    market_cap: Mapped[float | None] = mapped_column(Numeric(24, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set when a listing is delisted, so backtests can avoid survivorship bias.
    delisted_on: Mapped[date | None] = mapped_column(Date)

    exchange: Mapped[Exchange] = relationship(back_populates="securities")

    def provider_symbol(self, provider: str) -> str:
        overrides = self.provider_symbols or {}
        if provider in overrides:
            return overrides[provider]
        if provider == "yahoo" and self.exchange and self.exchange.yahoo_suffix:
            return f"{self.symbol}{self.exchange.yahoo_suffix}"
        return self.symbol


class PriceData(Base):
    """Daily OHLCV bars. One row per security per session."""

    __tablename__ = "price_data"
    __table_args__ = (
        UniqueConstraint("security_id", "trade_date", name="uq_price_data_sec_date"),
        Index("ix_price_data_sec_date", "security_id", "trade_date"),
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint("high >= open AND high >= close", name="high_is_max"),
        CheckConstraint("low <= open AND low <= close", name="low_is_min"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Price, nullable=False)
    high: Mapped[float] = mapped_column(Price, nullable=False)
    low: Mapped[float] = mapped_column(Price, nullable=False)
    close: Mapped[float] = mapped_column(Price, nullable=False)
    # Split/dividend adjusted close, used for return calculations.
    adj_close: Mapped[float | None] = mapped_column(Price)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )
    # When the provider says the bar was struck vs. when we stored it. The gap
    # between these two is what the freshness monitor watches.
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntradayData(Base):
    """Intraday bars. Retained on a shorter horizon than daily history."""

    __tablename__ = "intraday_data"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "bar_timestamp", "interval",
            name="uq_intraday_data_sec_ts_interval",
        ),
        Index("ix_intraday_data_security_id_bar_timestamp", "security_id", "bar_timestamp"),
        CheckConstraint("high >= low", name="intraday_high_ge_low"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="intraday_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    bar_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)  # 1m,5m,15m,1h
    open: Mapped[float] = mapped_column(Price, nullable=False)
    high: Mapped[float] = mapped_column(Price, nullable=False)
    low: Mapped[float] = mapped_column(Price, nullable=False)
    close: Mapped[float] = mapped_column(Price, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Quote(Base):
    """Latest known quote per security. Upserted, one row per security."""

    __tablename__ = "quotes"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[float] = mapped_column(Price, nullable=False)
    previous_close: Mapped[float | None] = mapped_column(Price)
    change: Mapped[float | None] = mapped_column(Price)
    change_pct: Mapped[float | None] = mapped_column(Float)
    day_open: Mapped[float | None] = mapped_column(Price)
    day_high: Mapped[float | None] = mapped_column(Price)
    day_low: Mapped[float | None] = mapped_column(Price)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorporateAction(Base, TimestampMixin):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "ex_date", "action_type",
            name="uq_corporate_actions_sec_exdate_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)  # SPLIT/DIVIDEND/BONUS
    ex_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    ratio: Mapped[float | None] = mapped_column(Float)      # splits
    amount: Mapped[float | None] = mapped_column(Price)     # dividends
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
