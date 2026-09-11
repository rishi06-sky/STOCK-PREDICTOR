"""Paper broker.

Simulates fills against the latest known price with explicit slippage and
commission. It shares the Broker interface with live adapters, so the paper
engine and a real one differ only in where the fill comes from.

It deliberately refuses to fill when no trustworthy price exists: an unknown
price means no trade, in paper as in production.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.exceptions import UnknownPriceError
from app.core.logging import get_logger
from app.models.enums import OrderSide, OrderStatus, OrderType, TradingMode
from app.trading.brokers.base import Broker, OrderRequest, OrderResult

log = get_logger(__name__)


class PaperBroker(Broker):
    name = "paper"
    mode = TradingMode.PAPER

    def __init__(
        self, *, commission_bps: float | None = None, slippage_bps: float | None = None
    ):
        self.commission_bps = (
            commission_bps if commission_bps is not None else settings.commission_bps
        )
        self.slippage_bps = (
            slippage_bps if slippage_bps is not None else settings.slippage_bps
        )
        self._orders: dict[str, OrderStatus] = {}

    def submit_order(self, request: OrderRequest, *, reference_price: float) -> OrderResult:
        if reference_price is None or reference_price <= 0:
            raise UnknownPriceError(
                f"no trustworthy price for {request.symbol}; refusing to simulate a fill"
            )
        if request.quantity <= 0:
            return OrderResult(
                accepted=False, status=OrderStatus.REJECTED,
                rejection_reason="quantity must be positive",
            )

        # Slippage always works against the order, both directions.
        slip_rate = self.slippage_bps / 10_000.0
        fill_price = (
            reference_price * (1 + slip_rate)
            if request.side is OrderSide.BUY
            else reference_price * (1 - slip_rate)
        )

        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            crossed = (
                fill_price <= request.limit_price
                if request.side is OrderSide.BUY
                else fill_price >= request.limit_price
            )
            if not crossed:
                order_id = uuid.uuid4().hex
                self._orders[order_id] = OrderStatus.PENDING
                return OrderResult(
                    accepted=True, status=OrderStatus.PENDING, broker_order_id=order_id,
                    reference_price=reference_price,
                    rejection_reason="limit price not reached at simulation time",
                )

        notional = fill_price * request.quantity
        commission = notional * (self.commission_bps / 10_000.0)
        slippage_cost = abs(fill_price - reference_price) * request.quantity

        order_id = uuid.uuid4().hex
        self._orders[order_id] = OrderStatus.FILLED
        return OrderResult(
            accepted=True, status=OrderStatus.FILLED, broker_order_id=order_id,
            filled_quantity=request.quantity, average_fill_price=fill_price,
            commission=commission, slippage_cost=slippage_cost,
            reference_price=reference_price, executed_at=datetime.now(timezone.utc),
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        if self._orders.get(broker_order_id) is OrderStatus.PENDING:
            self._orders[broker_order_id] = OrderStatus.CANCELLED
            return True
        return False

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        return self._orders.get(broker_order_id, OrderStatus.REJECTED)
