"""Users, alerts, model registry, backtests and audit trail."""
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
    AlertSeverity, AlertStatus, AlertType, DataQuality, EventSeverity,
    ModelStatus, PredictionTarget, UserRole,
)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, native_enum=False, length=16), default=UserRole.USER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-user notification routing, e.g. {"email": true, "telegram": false}.
    notification_prefs: Mapped[dict | None] = mapped_column(JSONB)


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Only the digest is persisted; the plaintext is shown once at creation.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelVersion(Base, TimestampMixin):
    """Registry entry for a trained model artefact."""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        Index("ix_model_versions_status_target", "status", "target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[PredictionTarget] = mapped_column(
        SAEnum(PredictionTarget, native_enum=False, length=24), nullable=False
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ModelStatus] = mapped_column(
        SAEnum(ModelStatus, native_enum=False, length=16),
        default=ModelStatus.TRAINING, nullable=False,
    )
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_names: Mapped[list | None] = mapped_column(JSONB)
    hyperparameters: Mapped[dict | None] = mapped_column(JSONB)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))

    train_start: Mapped[date | None] = mapped_column(Date)
    train_end: Mapped[date | None] = mapped_column(Date)
    training_rows: Mapped[int | None] = mapped_column(Integer)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Baseline feature distribution, compared against live data to detect drift.
    feature_baseline: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    metrics: Mapped[list["ModelMetric"]] = relationship(
        back_populates="model_version", cascade="all, delete-orphan"
    )


class ModelMetric(Base):
    """One evaluation result. `split` separates CV folds from holdout and live."""

    __tablename__ = "model_metrics"
    __table_args__ = (
        Index("ix_model_metrics_model_version_id_split", "model_version_id", "split"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_version_id: Mapped[int] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False
    )
    split: Mapped[str] = mapped_column(String(32), nullable=False)  # fold_0/holdout/live
    metric_name: Mapped[str] = mapped_column(String(48), nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    model_version: Mapped[ModelVersion] = relationship(back_populates="metrics")


class Backtest(Base, TimestampMixin):
    """A completed backtest run. Never to be presented as live performance."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB)
    # Flags a run executed against synthetic data so results can never be
    # mistaken for a real-market backtest.
    data_quality: Mapped[DataQuality] = mapped_column(
        SAEnum(DataQuality, native_enum=False, length=16), nullable=False
    )

    total_return: Mapped[float | None] = mapped_column(Float)
    cagr: Mapped[float | None] = mapped_column(Float)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float)
    sortino_ratio: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)
    win_rate: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    total_trades: Mapped[int | None] = mapped_column(Integer)
    average_trade_return: Mapped[float | None] = mapped_column(Float)
    volatility: Mapped[float | None] = mapped_column(Float)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32))
    benchmark_return: Mapped[float | None] = mapped_column(Float)
    equity_curve: Mapped[list | None] = mapped_column(JSONB)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trades: Mapped[list["BacktestTrade"]] = relationship(
        back_populates="backtest", cascade="all, delete-orphan"
    )


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backtest_id: Mapped[int] = mapped_column(
        ForeignKey("backtests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date)
    entry_price: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    gross_pnl: Mapped[float | None] = mapped_column(Numeric(24, 2))
    net_pnl: Mapped[float | None] = mapped_column(Numeric(24, 2))
    costs: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    return_pct: Mapped[float | None] = mapped_column(Float)
    holding_days: Mapped[int | None] = mapped_column(Integer)
    exit_reason: Mapped[str | None] = mapped_column(String(24))

    backtest: Mapped[Backtest] = relationship(back_populates="trades")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_user_id_created_at", "user_id", "created_at"),
        Index("ix_alerts_dedupe_key_created_at", "dedupe_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))

    alert_type: Mapped[AlertType] = mapped_column(
        SAEnum(AlertType, native_enum=False, length=24), nullable=False, index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, native_enum=False, length=12),
        default=AlertSeverity.INFO, nullable=False,
    )
    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus, native_enum=False, length=12),
        default=AlertStatus.PENDING, nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    # Stable hash of the alert's meaning; identical keys inside the dedupe
    # window are suppressed rather than re-sent.
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    channels: Mapped[list | None] = mapped_column(JSONB)
    delivery_results: Mapped[dict | None] = mapped_column(JSONB)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class SystemEvent(Base):
    """Operational log of anything worth alarming or reporting on."""

    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_events_component_created_at", "component", "created_at"),
        Index("ix_system_events_severity_created_at", "severity", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[EventSeverity] = mapped_column(
        SAEnum(EventSeverity, native_enum=False, length=12),
        default=EventSeverity.INFO, nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    """Who did what. Append-only; never updated or deleted by application code."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_id_created_at", "user_id", "created_at"),
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # email or "system"
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataFreshness(Base):
    """Per-provider/per-dataset heartbeat driving the staleness guards."""

    __tablename__ = "data_freshness"
    __table_args__ = (
        UniqueConstraint("provider", "dataset", name="uq_data_freshness_provider_dataset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)  # quotes/daily/news
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_ingested: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
