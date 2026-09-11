"""Derived analytics: features, fundamentals, news, predictions, signals."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum as SAEnum,
    Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import (
    DataQuality, MarketRegime, PredictionTarget, RiskLevel, SentimentLabel,
    SignalStatus, SignalType,
)
from app.models.market import Price


class TechnicalFeature(Base):
    """One row per security per session holding the computed feature vector.

    Features are stored as JSONB rather than columns so the pipeline can evolve
    without a migration per indicator. `feature_set_version` records which
    pipeline produced the row, so training never mixes incompatible vectors.
    """

    __tablename__ = "technical_features"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "trade_date", "feature_set_version",
            name="uq_technical_features_sec_date_version",
        ),
        Index("ix_technical_features_security_id_trade_date", "security_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Fundamental(Base, TimestampMixin):
    """Point-in-time fundamentals.

    `as_of_date` is the period the figures describe; `reported_at` is when they
    became public. Training must filter on `reported_at` -- using `as_of_date`
    would leak figures into a window before anyone could have known them.
    """

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "as_of_date", "period",
            name="uq_fundamentals_sec_date_period",
        ),
        Index("ix_fundamentals_security_id_reported_at", "security_id", "reported_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # TTM/FY/Q

    pe_ratio: Mapped[float | None] = mapped_column(Float)
    pb_ratio: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    revenue: Mapped[float | None] = mapped_column(Numeric(24, 2))
    revenue_growth: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Numeric(24, 2))
    profit_growth: Mapped[float | None] = mapped_column(Float)
    roe: Mapped[float | None] = mapped_column(Float)
    roce: Mapped[float | None] = mapped_column(Float)
    debt_to_equity: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Numeric(24, 2))
    dividend_yield: Mapped[float | None] = mapped_column(Float)
    gross_margin: Mapped[float | None] = mapped_column(Float)
    operating_margin: Mapped[float | None] = mapped_column(Float)
    net_margin: Mapped[float | None] = mapped_column(Float)
    earnings_growth: Mapped[float | None] = mapped_column(Float)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB)


class NewsArticle(Base):
    __tablename__ = "news"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_news_content_hash"),
        Index("ix_news_published_at", "published_at"),
        Index("ix_news_security_id_published_at", "security_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    # Distinguishing these two is what stops old news being shown as breaking.
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    sentiment: Mapped["NewsSentiment | None"] = relationship(
        back_populates="article", cascade="all, delete-orphan", uselist=False
    )


class NewsSentiment(Base):
    __tablename__ = "sentiment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_id: Mapped[int] = mapped_column(
        ForeignKey("news.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[SentimentLabel] = mapped_column(
        SAEnum(SentimentLabel, native_enum=False, length=16), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)       # -1..1
    confidence: Mapped[float] = mapped_column(Float, nullable=False)  # 0..1
    analyzer: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    article: Mapped[NewsArticle] = relationship(back_populates="sentiment")

    __table_args__ = (
        CheckConstraint("score >= -1 AND score <= 1", name="sentiment_score_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="sentiment_conf_range"),
    )


class RegimeState(Base):
    """Detected market regime per exchange per session."""

    __tablename__ = "regime_states"
    __table_args__ = (
        UniqueConstraint("exchange_id", "trade_date", name="uq_regime_states_exch_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    regime: Mapped[MarketRegime] = mapped_column(
        SAEnum(MarketRegime, native_enum=False, length=24), nullable=False
    )
    volatility_regime: Mapped[MarketRegime] = mapped_column(
        SAEnum(MarketRegime, native_enum=False, length=24), nullable=False
    )
    trend_strength: Mapped[float | None] = mapped_column(Float)
    realized_volatility: Mapped[float | None] = mapped_column(Float)
    breadth: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Prediction(Base):
    """A raw model output, before any risk or signal logic is applied."""

    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "model_version_id", "as_of_date", "horizon_days",
            name="uq_predictions_sec_model_date_horizon",
        ),
        Index("ix_predictions_security_id_as_of_date", "security_id", "as_of_date"),
        CheckConstraint("horizon_days > 0", name="horizon_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    model_version_id: Mapped[int] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[PredictionTarget] = mapped_column(
        SAEnum(PredictionTarget, native_enum=False, length=24), nullable=False
    )

    # Raw model probability, and the calibrated value actually used downstream.
    raw_probability: Mapped[float | None] = mapped_column(Float)
    calibrated_probability: Mapped[float | None] = mapped_column(Float)
    expected_return: Mapped[float | None] = mapped_column(Float)
    predicted_volatility: Mapped[float | None] = mapped_column(Float)
    feature_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    reference_price: Mapped[float | None] = mapped_column(Price)
    data_quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Signal(Base):
    """An actionable, risk-checked recommendation with a full audit trail."""

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_status_generated_at", "status", "generated_at"),
        Index("ix_signals_security_id_generated_at", "security_id", "generated_at"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="signal_confidence_range"),
        CheckConstraint("expires_at > generated_at", name="signal_expiry_after_generation"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    prediction_id: Mapped[int | None] = mapped_column(
        ForeignKey("predictions.id", ondelete="SET NULL")
    )
    model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )

    signal: Mapped[SignalType] = mapped_column(
        SAEnum(SignalType, native_enum=False, length=16), nullable=False, index=True
    )
    status: Mapped[SignalStatus] = mapped_column(
        SAEnum(SignalStatus, native_enum=False, length=16),
        default=SignalStatus.ACTIVE, nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    expected_return: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[RiskLevel] = mapped_column(
        SAEnum(RiskLevel, native_enum=False, length=16), nullable=False
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)

    reference_price: Mapped[float] = mapped_column(Price, nullable=False)
    entry_low: Mapped[float | None] = mapped_column(Price)
    entry_high: Mapped[float | None] = mapped_column(Price)
    stop_loss: Mapped[float | None] = mapped_column(Price)
    take_profit: Mapped[float | None] = mapped_column(Price)
    reward_to_risk: Mapped[float | None] = mapped_column(Float)

    opportunity_score: Mapped[float | None] = mapped_column(Float, index=True)
    regime: Mapped[MarketRegime | None] = mapped_column(
        SAEnum(MarketRegime, native_enum=False, length=24)
    )
    # Human-readable drivers behind the call, each with its contribution.
    rationale: Mapped[list | None] = mapped_column(JSONB)
    data_quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )
    price_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(256))
