"""Risk engine.

Two responsibilities:

1. **Sizing.** Turn a signal into a position size from volatility and a fixed
   fraction of capital at risk -- never a fixed share count.
2. **Vetoing.** Refuse orders that breach exposure, concentration, drawdown or
   daily-loss limits.

Rule: a model prediction can never override a risk control. Confidence affects
how large a position is, never whether the limits apply.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import OrderSide, PositionStatus, RiskLevel
from app.models.market import Quote, Security
from app.models.trading import Holding, Portfolio

log = get_logger(__name__)


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    quantity: float = 0.0
    notional: float = 0.0
    reasons: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MODERATE
    stop_loss: float | None = None
    take_profit: float | None = None
    reward_to_risk: float | None = None
    capital_at_risk: float = 0.0

    def as_dict(self) -> dict:
        return {
            "approved": self.approved, "quantity": round(self.quantity, 6),
            "notional": round(self.notional, 2),
            "risk_level": str(self.risk_level),
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "take_profit": round(self.take_profit, 4) if self.take_profit else None,
            "reward_to_risk": round(self.reward_to_risk, 3) if self.reward_to_risk else None,
            "capital_at_risk": round(self.capital_at_risk, 2),
            "reasons": self.reasons, "violations": self.violations,
        }


def classify_risk(volatility: float | None, *, atr_pct: float | None = None) -> RiskLevel:
    """Map annualised volatility onto a risk band."""
    measure = volatility if volatility is not None else (
        (atr_pct * math.sqrt(252)) if atr_pct is not None else None
    )
    if measure is None:
        # Unknown risk is treated as high, not average.
        return RiskLevel.HIGH
    if measure < 0.20:
        return RiskLevel.LOW
    if measure < 0.35:
        return RiskLevel.MODERATE
    if measure < 0.60:
        return RiskLevel.HIGH
    return RiskLevel.VERY_HIGH


def volatility_stop(
    price: float, atr: float | None, *, side: OrderSide = OrderSide.BUY,
    atr_multiplier: float = 2.0, fallback_pct: float = 0.08,
) -> float:
    """Stop placed at a volatility-scaled distance, not a flat percentage."""
    distance = (atr * atr_multiplier) if atr and atr > 0 else price * fallback_pct
    # Keep the stop within sane bounds: too tight guarantees noise stop-outs,
    # too wide makes position sizing meaningless.
    distance = max(price * 0.02, min(distance, price * 0.25))
    return price - distance if side is OrderSide.BUY else price + distance


def take_profit_from_rr(
    price: float, stop: float, *, side: OrderSide = OrderSide.BUY,
    reward_to_risk: float = 2.0,
) -> float:
    risk = abs(price - stop)
    return price + risk * reward_to_risk if side is OrderSide.BUY else price - risk * reward_to_risk


def position_size(
    equity: float, price: float, stop: float, *,
    risk_per_trade_pct: float, max_position_pct: float, size_multiplier: float = 1.0,
) -> tuple[float, list[str]]:
    """Size so that a stop-out costs a fixed fraction of equity.

    Returns (quantity, notes). The position-value cap is applied afterwards so
    a very tight stop cannot imply an enormous position.
    """
    notes: list[str] = []
    risk_per_share = abs(price - stop)
    if risk_per_share <= 0 or price <= 0 or equity <= 0:
        return 0.0, ["invalid price, stop or equity"]

    capital_at_risk = equity * risk_per_trade_pct * max(0.0, size_multiplier)
    quantity = capital_at_risk / risk_per_share

    max_notional = equity * max_position_pct
    if quantity * price > max_notional:
        quantity = max_notional / price
        notes.append(
            f"position capped at {max_position_pct:.0%} of equity "
            f"(volatility-based size would have been larger)"
        )
    return max(quantity, 0.0), notes


class RiskEngine:
    def __init__(self, db: Session):
        self.db = db

    # --------------------------------------------------------------- equity
    def portfolio_equity(self, portfolio: Portfolio) -> tuple[float, float]:
        """Return (equity, positions_value) using the latest known prices."""
        positions_value = 0.0
        holdings = self.db.scalars(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).all()
        for holding in holdings:
            quote = self.db.get(Quote, holding.security_id)
            price = float(quote.price) if quote else float(holding.average_cost)
            positions_value += price * float(holding.quantity)
        return float(portfolio.cash) + positions_value, positions_value

    # ---------------------------------------------------------------- checks
    def evaluate_entry(
        self,
        portfolio: Portfolio,
        security: Security,
        price: float,
        *,
        side: OrderSide = OrderSide.BUY,
        atr: float | None = None,
        volatility: float | None = None,
        confidence: float = 0.5,
        size_multiplier: float = 1.0,
        atr_multiplier: float = 2.0,
    ) -> RiskDecision:
        decision = RiskDecision(approved=False)

        if settings.kill_switch_engaged:
            decision.violations.append("kill switch engaged; no new positions")
            return decision
        if price <= 0:
            decision.violations.append("invalid price")
            return decision

        equity, positions_value = self.portfolio_equity(portfolio)
        if equity <= 0:
            decision.violations.append("portfolio equity is zero or negative")
            return decision

        decision.risk_level = classify_risk(volatility, atr_pct=(atr / price) if atr else None)

        # ---------------------------------------------------- levels first
        stop = volatility_stop(price, atr, side=side, atr_multiplier=atr_multiplier)
        target = take_profit_from_rr(
            price, stop, side=side, reward_to_risk=max(settings.risk_min_reward_to_risk, 2.0)
        )
        decision.stop_loss, decision.take_profit = stop, target
        risk_amount = abs(price - stop)
        decision.reward_to_risk = abs(target - price) / risk_amount if risk_amount > 0 else None

        if decision.reward_to_risk is not None and (
            decision.reward_to_risk < settings.risk_min_reward_to_risk
        ):
            decision.violations.append(
                f"reward:risk {decision.reward_to_risk:.2f} below the required "
                f"{settings.risk_min_reward_to_risk}"
            )

        # --------------------------------------------------- portfolio gates
        open_count = self.db.scalar(
            select(func.count())
            .select_from(Holding)
            .where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ) or 0
        if open_count >= settings.risk_max_open_positions:
            decision.violations.append(
                f"already holding {open_count} positions "
                f"(limit {settings.risk_max_open_positions})"
            )

        existing = self.db.scalars(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id,
                Holding.security_id == security.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).first()
        if existing is not None:
            decision.violations.append(f"already holding {security.symbol}")

        # Drawdown circuit breaker.
        peak = float(portfolio.peak_equity or portfolio.starting_cash)
        if peak > 0:
            drawdown = equity / peak - 1
            if drawdown <= -settings.risk_max_drawdown_pct:
                decision.violations.append(
                    f"portfolio drawdown {drawdown:.1%} breaches the "
                    f"{-settings.risk_max_drawdown_pct:.0%} limit; entries halted"
                )

        # Daily loss limit.
        today = datetime.now(timezone.utc).date()
        if portfolio.day_pnl_date == today:
            day_loss_pct = float(portfolio.day_realized_pnl) / equity
            if day_loss_pct <= -settings.risk_max_daily_loss_pct:
                decision.violations.append(
                    f"daily realised loss {day_loss_pct:.1%} breaches the "
                    f"{-settings.risk_max_daily_loss_pct:.0%} limit"
                )

        # ------------------------------------------------------------- size
        # Lower-confidence signals get smaller positions, but confidence never
        # relaxes a limit.
        confidence_scale = max(0.0, min(1.0, (confidence - 0.5) * 2)) if confidence > 0.5 else 0.0
        effective_multiplier = size_multiplier * (0.5 + 0.5 * confidence_scale)

        quantity, notes = position_size(
            equity, price, stop,
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_position_pct=settings.risk_max_position_pct,
            size_multiplier=effective_multiplier,
        )
        decision.reasons.extend(notes)

        notional = quantity * price
        if notional <= 0:
            decision.violations.append("computed position size is zero")

        # Total exposure ceiling.
        if equity > 0 and (positions_value + notional) / equity > settings.risk_max_portfolio_exposure_pct:
            headroom = max(
                0.0, equity * settings.risk_max_portfolio_exposure_pct - positions_value
            )
            if headroom < price:
                decision.violations.append(
                    f"portfolio exposure limit {settings.risk_max_portfolio_exposure_pct:.0%} "
                    "leaves no room for this position"
                )
            else:
                quantity = headroom / price
                notional = quantity * price
                decision.reasons.append("size reduced to fit the portfolio exposure limit")

        # Sector concentration.
        if security.sector:
            sector_value = self._sector_exposure(portfolio, security.sector)
            if equity > 0 and (sector_value + notional) / equity > settings.risk_max_sector_pct:
                headroom = max(0.0, equity * settings.risk_max_sector_pct - sector_value)
                if headroom < price:
                    decision.violations.append(
                        f"sector '{security.sector}' is at its "
                        f"{settings.risk_max_sector_pct:.0%} concentration limit"
                    )
                else:
                    quantity = min(quantity, headroom / price)
                    notional = quantity * price
                    decision.reasons.append(
                        f"size reduced to respect the {security.sector} sector limit"
                    )

        # Cash.
        if notional > float(portfolio.cash):
            affordable = float(portfolio.cash) / price
            if affordable < 1e-6:
                decision.violations.append("insufficient cash")
            else:
                quantity = affordable
                notional = quantity * price
                decision.reasons.append("size reduced to available cash")

        decision.quantity = quantity
        decision.notional = notional
        decision.capital_at_risk = quantity * abs(price - stop)
        decision.approved = not decision.violations and quantity > 0

        if decision.approved:
            decision.reasons.append(
                f"risking {decision.capital_at_risk:,.0f} "
                f"({decision.capital_at_risk / equity:.2%} of equity) to the stop"
            )
        return decision

    def _sector_exposure(self, portfolio: Portfolio, sector: str) -> float:
        rows = self.db.execute(
            select(Holding.quantity, Holding.average_cost, Holding.security_id)
            .join(Security, Security.id == Holding.security_id)
            .where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
                Security.sector == sector,
            )
        ).all()
        total = 0.0
        for quantity, average_cost, security_id in rows:
            quote = self.db.get(Quote, security_id)
            price = float(quote.price) if quote else float(average_cost)
            total += price * float(quantity)
        return total

    def check_exits(self, portfolio: Portfolio) -> list[dict]:
        """Positions whose stop or target has been reached at the latest price."""
        triggered = []
        holdings = self.db.scalars(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).all()

        for holding in holdings:
            quote = self.db.get(Quote, holding.security_id)
            if quote is None:
                continue
            price = float(quote.price)
            if holding.stop_loss is not None and price <= float(holding.stop_loss):
                triggered.append(
                    {"holding_id": holding.id, "security_id": holding.security_id,
                     "reason": "STOP_LOSS", "price": price,
                     "level": float(holding.stop_loss)}
                )
            elif holding.take_profit is not None and price >= float(holding.take_profit):
                triggered.append(
                    {"holding_id": holding.id, "security_id": holding.security_id,
                     "reason": "TAKE_PROFIT", "price": price,
                     "level": float(holding.take_profit)}
                )
        return triggered
