"""Portfolios, positions, orders and executed trades."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum as SAEnum,
    Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.database.base import Base, TimestampMixin
from app.models.enums import (
    ExitReason, OrderSide, OrderStatus, OrderType, PositionStatus, TradingMode,
)
from app.models.market import Price

Qty = Numeric(20, 6)


class Portfolio(Base, TimestampMixin):
    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_portfolios_user_id_name"),
        CheckConstraint("cash >= 0", name="cash_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, native_enum=False, length=8),
        default=TradingMode.PAPER, nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String(8), default=settings.base_currency, nullable=False
    )
    starting_cash: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    cash: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # High-water mark of equity, used for drawdown protection.
    peak_equity: Mapped[float | None] = mapped_column(Numeric(24, 2))
    # Realised P&L accumulated today, reset by the daily-loss guard.
    day_realized_pnl: Mapped[float] = mapped_column(Numeric(24, 2), default=0, nullable=False)
    day_pnl_date: Mapped[date | None] = mapped_column(Date)

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class Holding(Base, TimestampMixin):
    """An open position. Closed positions keep a row for history."""

    __tablename__ = "holdings"
    __table_args__ = (
        Index("ix_holdings_portfolio_id_status", "portfolio_id", "status"),
        CheckConstraint("quantity >= 0", name="holding_qty_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[float] = mapped_column(Qty, nullable=False)
    average_cost: Mapped[float] = mapped_column(Price, nullable=False)
    status: Mapped[PositionStatus] = mapped_column(
        SAEnum(PositionStatus, native_enum=False, length=8),
        default=PositionStatus.OPEN, nullable=False,
    )
    stop_loss: Mapped[float | None] = mapped_column(Price)
    take_profit: Mapped[float | None] = mapped_column(Price)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    realized_pnl: Mapped[float] = mapped_column(Numeric(24, 2), default=0, nullable=False)
    opening_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL")
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings")


class Order(Base, TimestampMixin):
    """An order intent. Immutable once terminal."""

    __tablename__ = "orders"
    __table_args__ = (
        # Idempotency: the same client key can never produce two orders.
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_portfolio_id_status", "portfolio_id", "status"),
        CheckConstraint("quantity > 0", name="order_qty_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        SAEnum(OrderSide, native_enum=False, length=8), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        SAEnum(OrderType, native_enum=False, length=8),
        default=OrderType.MARKET, nullable=False,
    )
    quantity: Mapped[float] = mapped_column(Qty, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Price)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, native_enum=False, length=20),
        default=OrderStatus.PENDING, nullable=False,
    )
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, native_enum=False, length=8), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    filled_quantity: Mapped[float] = mapped_column(Qty, default=0, nullable=False)
    average_fill_price: Mapped[float | None] = mapped_column(Price)
    rejection_reason: Mapped[str | None] = mapped_column(String(256))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    portfolio: Mapped[Portfolio] = relationship(back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class Trade(Base):
    """A fill. Records the costs that separate gross from net P&L."""

    __tablename__ = "paper_trades"
    __table_args__ = (
        Index("ix_paper_trades_portfolio_id_executed_at", "portfolio_id", "executed_at"),
        CheckConstraint("quantity > 0 AND price > 0", name="trade_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    side: Mapped[OrderSide] = mapped_column(
        SAEnum(OrderSide, native_enum=False, length=8), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Qty, nullable=False)
    # Price actually transacted, after slippage was applied to the reference.
    price: Mapped[float] = mapped_column(Price, nullable=False)
    reference_price: Mapped[float] = mapped_column(Price, nullable=False)
    commission: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    slippage_cost: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(24, 2))
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        SAEnum(ExitReason, native_enum=False, length=20)
    )
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, native_enum=False, length=8), nullable=False
    )
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    order: Mapped[Order] = relationship(back_populates="trades")


class PortfolioSnapshot(Base):
    """Daily equity curve, the basis for live performance statistics."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "snapshot_date",
            name="uq_portfolio_snapshots_pf_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    cash: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    positions_value: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    equity: Mapped[float] = mapped_column(Numeric(24, 2), nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(24, 2), default=0, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Numeric(24, 2), default=0, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exposure_pct: Mapped[float | None] = mapped_column(Float)
    drawdown_pct: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
