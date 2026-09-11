"""Order manager: the only path from a signal to a fill.

Order of operations is fixed and not bypassable:

    kill switch -> idempotency -> risk engine -> broker -> ledger

Risk sits between the signal and the broker by construction, so no caller can
place an order that skipped it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    DuplicateOrderError, KillSwitchEngaged, RiskRejection, UnknownPriceError,
)
from app.core.logging import get_logger
from app.models.enums import (
    EventSeverity, ExitReason, OrderSide, OrderStatus, OrderType, PositionStatus,
    TradingMode,
)
from app.models.market import Quote, Security
from app.models.platform import AuditLog, SystemEvent
from app.models.trading import Holding, Order, Portfolio, Trade
from app.risk.engine import RiskEngine
from app.trading.brokers.base import Broker, OrderRequest
from app.trading.brokers.live_guard import assert_trading_allowed
from app.trading.brokers.paper import PaperBroker

log = get_logger(__name__)


@dataclass(slots=True)
class ExecutionReport:
    executed: bool
    order_id: int | None = None
    trade_id: int | None = None
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float | None = None
    reason: str | None = None
    risk: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "executed": self.executed, "order_id": self.order_id,
            "symbol": self.symbol, "side": self.side,
            "quantity": round(self.quantity, 6),
            "price": round(self.price, 4) if self.price else None,
            "reason": self.reason, "risk": self.risk,
        }


def make_idempotency_key(
    portfolio_id: int, security_id: int, side: OrderSide, day: date, nonce: str = ""
) -> str:
    """Stable key: the same intent on the same day maps to one order."""
    basis = f"{portfolio_id}:{security_id}:{side}:{day.isoformat()}:{nonce}"
    return hashlib.sha256(basis.encode()).hexdigest()[:48]


class OrderManager:
    def __init__(self, db: Session, broker: Broker | None = None):
        self.db = db
        self.broker = broker or PaperBroker()
        self.risk = RiskEngine(db)

    # ----------------------------------------------------------------- entry
    def open_position(
        self,
        portfolio: Portfolio,
        security: Security,
        *,
        confidence: float = 0.5,
        atr: float | None = None,
        volatility: float | None = None,
        size_multiplier: float = 1.0,
        atr_multiplier: float = 2.0,
        signal_id: int | None = None,
        nonce: str = "",
        commit: bool = True,
    ) -> ExecutionReport:
        assert_trading_allowed()

        price = self._current_price(security)
        key = make_idempotency_key(
            portfolio.id, security.id, OrderSide.BUY,
            datetime.now(timezone.utc).date(), nonce,
        )
        if self._already_ordered(key):
            raise DuplicateOrderError(
                f"an order for {security.symbol} with this key already exists today"
            )

        decision = self.risk.evaluate_entry(
            portfolio, security, price, side=OrderSide.BUY, atr=atr,
            volatility=volatility, confidence=confidence,
            size_multiplier=size_multiplier, atr_multiplier=atr_multiplier,
        )
        if not decision.approved:
            self._record_event(
                "risk_rejected_entry",
                f"{security.symbol}: {'; '.join(decision.violations)}",
                EventSeverity.INFO,
                {"symbol": security.symbol, "violations": decision.violations},
            )
            if commit:
                self.db.commit()
            return ExecutionReport(
                executed=False, symbol=security.symbol, side="BUY",
                reason="; ".join(decision.violations), risk=decision.as_dict(),
            )

        order = self._persist_order(
            portfolio, security, OrderSide.BUY, decision.quantity, key, signal_id
        )
        result = self.broker.submit_order(
            OrderRequest(
                security_id=security.id, symbol=security.symbol, side=OrderSide.BUY,
                quantity=decision.quantity, idempotency_key=key, signal_id=signal_id,
            ),
            reference_price=price,
        )

        if not result.accepted or result.status is not OrderStatus.FILLED:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = result.rejection_reason or "broker did not fill"
            order.completed_at = datetime.now(timezone.utc)
            if commit:
                self.db.commit()
            return ExecutionReport(
                executed=False, order_id=order.id, symbol=security.symbol, side="BUY",
                reason=order.rejection_reason, risk=decision.as_dict(),
            )

        trade = self._record_fill(
            order, portfolio, security, OrderSide.BUY, result, exit_reason=None
        )

        cost = result.average_fill_price * result.filled_quantity + result.commission
        portfolio.cash = float(portfolio.cash) - cost

        self.db.add(
            Holding(
                portfolio_id=portfolio.id, security_id=security.id,
                quantity=result.filled_quantity,
                average_cost=result.average_fill_price,
                status=PositionStatus.OPEN,
                stop_loss=decision.stop_loss, take_profit=decision.take_profit,
                opened_at=datetime.now(timezone.utc),
                opening_signal_id=signal_id,
            )
        )
        self._audit("order.open", security.symbol, {
            "quantity": result.filled_quantity, "price": result.average_fill_price,
            "mode": str(self.broker.mode),
        })

        if commit:
            self.db.commit()
        return ExecutionReport(
            executed=True, order_id=order.id, trade_id=trade.id, symbol=security.symbol,
            side="BUY", quantity=result.filled_quantity,
            price=result.average_fill_price, risk=decision.as_dict(),
        )

    # ------------------------------------------------------------------ exit
    def close_position(
        self, portfolio: Portfolio, holding: Holding, *,
        reason: ExitReason = ExitReason.MANUAL, nonce: str = "", commit: bool = True,
    ) -> ExecutionReport:
        assert_trading_allowed()

        security = self.db.get(Security, holding.security_id)
        price = self._current_price(security)
        key = make_idempotency_key(
            portfolio.id, security.id, OrderSide.SELL,
            datetime.now(timezone.utc).date(), nonce or str(holding.id),
        )
        if self._already_ordered(key):
            raise DuplicateOrderError(f"a closing order for {security.symbol} already exists")

        quantity = float(holding.quantity)
        order = self._persist_order(portfolio, security, OrderSide.SELL, quantity, key, None)
        result = self.broker.submit_order(
            OrderRequest(
                security_id=security.id, symbol=security.symbol, side=OrderSide.SELL,
                quantity=quantity, idempotency_key=key,
            ),
            reference_price=price,
        )

        if not result.accepted or result.status is not OrderStatus.FILLED:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = result.rejection_reason or "broker did not fill"
            order.completed_at = datetime.now(timezone.utc)
            if commit:
                self.db.commit()
            return ExecutionReport(
                executed=False, order_id=order.id, symbol=security.symbol,
                side="SELL", reason=order.rejection_reason,
            )

        gross = (result.average_fill_price - float(holding.average_cost)) * quantity
        realized = gross - result.commission
        trade = self._record_fill(
            order, portfolio, security, OrderSide.SELL, result,
            exit_reason=reason, realized_pnl=realized,
        )

        portfolio.cash = float(portfolio.cash) + (
            result.average_fill_price * quantity - result.commission
        )
        today = datetime.now(timezone.utc).date()
        if portfolio.day_pnl_date != today:
            portfolio.day_pnl_date = today
            portfolio.day_realized_pnl = 0
        portfolio.day_realized_pnl = float(portfolio.day_realized_pnl) + realized

        holding.status = PositionStatus.CLOSED
        holding.closed_at = datetime.now(timezone.utc)
        holding.realized_pnl = realized
        holding.quantity = 0

        self._audit("order.close", security.symbol, {
            "quantity": quantity, "price": result.average_fill_price,
            "realized_pnl": realized, "reason": str(reason),
        })

        if commit:
            self.db.commit()
        return ExecutionReport(
            executed=True, order_id=order.id, trade_id=trade.id, symbol=security.symbol,
            side="SELL", quantity=quantity, price=result.average_fill_price,
            reason=str(reason),
        )

    # ------------------------------------------------------------- internals
    def _current_price(self, security: Security) -> float:
        """Latest trustworthy price, or refuse.

        Freshness is enforced here rather than trusted from the caller: an
        unknown or stale price must never become a fill.
        """
        quote = self.db.get(Quote, security.id)
        if quote is None:
            raise UnknownPriceError(f"no quote stored for {security.symbol}; no trade")

        from app.models.enums import DataQuality

        if quote.quality is not DataQuality.SYNTHETIC:
            age = (datetime.now(timezone.utc) - quote.source_timestamp).total_seconds()
            if age > settings.signal_halt_staleness_seconds:
                raise UnknownPriceError(
                    f"{security.symbol}: price is {age / 60:.0f} minutes old; refusing to trade"
                )
        price = float(quote.price)
        if price <= 0:
            raise UnknownPriceError(f"{security.symbol}: stored price is not positive")
        return price

    def _already_ordered(self, key: str) -> bool:
        return bool(
            self.db.scalar(select(Order.id).where(Order.idempotency_key == key))
        )

    def _persist_order(
        self, portfolio, security, side, quantity, key, signal_id
    ) -> Order:
        order = Order(
            portfolio_id=portfolio.id, security_id=security.id, signal_id=signal_id,
            idempotency_key=key, side=side, order_type=OrderType.MARKET,
            quantity=quantity, status=OrderStatus.SUBMITTED,
            mode=TradingMode.PAPER if self.broker.mode is TradingMode.PAPER else TradingMode.LIVE,
            broker=self.broker.name, submitted_at=datetime.now(timezone.utc),
        )
        self.db.add(order)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateOrderError(f"duplicate order rejected by the database: {key}") from exc
        return order

    def _record_fill(
        self, order, portfolio, security, side, result, *, exit_reason, realized_pnl=None
    ) -> Trade:
        order.status = OrderStatus.FILLED
        order.broker_order_id = result.broker_order_id
        order.filled_quantity = result.filled_quantity
        order.average_fill_price = result.average_fill_price
        order.completed_at = result.executed_at or datetime.now(timezone.utc)

        trade = Trade(
            order_id=order.id, portfolio_id=portfolio.id, security_id=security.id,
            side=side, quantity=result.filled_quantity, price=result.average_fill_price,
            reference_price=result.reference_price or result.average_fill_price,
            commission=result.commission, slippage_cost=result.slippage_cost,
            realized_pnl=realized_pnl, exit_reason=exit_reason,
            mode=order.mode, executed_at=order.completed_at,
        )
        self.db.add(trade)
        self.db.flush()
        return trade

    def _record_event(self, event_type: str, message: str, severity, details: dict) -> None:
        self.db.add(
            SystemEvent(
                component="trading", event_type=event_type, severity=severity,
                message=message, details=details, created_at=datetime.now(timezone.utc),
            )
        )

    def _audit(self, action: str, resource: str, details: dict) -> None:
        self.db.add(
            AuditLog(
                actor="system", action=action, resource_type="order",
                resource_id=resource, success=True, details=details,
                created_at=datetime.now(timezone.utc),
            )
        )
