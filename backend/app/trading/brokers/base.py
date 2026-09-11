"""Broker abstraction.

The same interface serves paper and live trading so the paper engine exercises
the identical code path a real broker would. Live adapters are opt-in and
disabled by default; see `app.trading.brokers.live_guard`.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.models.enums import OrderSide, OrderStatus, OrderType, TradingMode


@dataclass(slots=True)
class OrderRequest:
    security_id: int
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    #: Caller-supplied key. The same key must never produce two orders.
    idempotency_key: str = ""
    signal_id: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class OrderResult:
    accepted: bool
    status: OrderStatus
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    commission: float = 0.0
    slippage_cost: float = 0.0
    reference_price: float | None = None
    rejection_reason: str | None = None
    executed_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted, "status": str(self.status),
            "broker_order_id": self.broker_order_id,
            "filled_quantity": round(self.filled_quantity, 6),
            "average_fill_price": (
                round(self.average_fill_price, 6) if self.average_fill_price else None
            ),
            "commission": round(self.commission, 4),
            "slippage_cost": round(self.slippage_cost, 4),
            "rejection_reason": self.rejection_reason,
        }


class Broker(abc.ABC):
    name: str = "base"
    mode: TradingMode = TradingMode.PAPER

    @abc.abstractmethod
    def submit_order(self, request: OrderRequest, *, reference_price: float) -> OrderResult:
        ...

    @abc.abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abc.abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        ...

    def is_connected(self) -> bool:
        return True

    def health_check(self) -> tuple[bool, str]:
        return self.is_connected(), "ok" if self.is_connected() else "disconnected"
