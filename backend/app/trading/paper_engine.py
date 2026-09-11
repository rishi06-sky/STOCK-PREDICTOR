"""Autonomous paper-trading engine.

Consumes the SAME signals the dashboard shows and routes them through the SAME
order manager and risk engine a live deployment would use. Only the broker
differs. That is what makes paper results a meaningful rehearsal rather than a
separate toy system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    DuplicateOrderError, KillSwitchEngaged, UnknownPriceError,
)
from app.core.logging import get_logger
from app.models.analysis import Signal
from app.models.enums import (
    DataQuality, EventSeverity, ExitReason, PositionStatus, SignalStatus, SignalType,
)
from app.models.market import PriceData, Security
from app.models.platform import SystemEvent
from app.models.trading import Holding, Portfolio
from app.portfolio.engine import PortfolioEngine
from app.risk.engine import RiskEngine
from app.technical import indicators as ta
from app.trading.brokers.paper import PaperBroker
from app.trading.order_manager import OrderManager

log = get_logger(__name__)


@dataclass(slots=True)
class PaperRunReport:
    opened: list[dict] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    equity_before: float = 0.0
    equity_after: float = 0.0
    halted: bool = False
    halt_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "opened": self.opened, "closed": self.closed,
            "skipped_count": len(self.skipped), "skipped": self.skipped[:20],
            "errors": self.errors,
            "equity_before": round(self.equity_before, 2),
            "equity_after": round(self.equity_after, 2),
            "halted": self.halted, "halt_reason": self.halt_reason,
        }


class PaperTradingEngine:
    def __init__(self, db: Session, portfolio: Portfolio):
        self.db = db
        self.portfolio = portfolio
        self.orders = OrderManager(db, broker=PaperBroker())
        self.risk = RiskEngine(db)
        self.portfolios = PortfolioEngine(db)

    def run_cycle(self, *, max_new_positions: int = 5) -> PaperRunReport:
        """One full pass: exits first, then entries, then snapshot."""
        report = PaperRunReport()

        if settings.kill_switch_engaged:
            report.halted = True
            report.halt_reason = "kill switch engaged"
            return report

        view_before = self.portfolios.value(self.portfolio)
        report.equity_before = view_before.equity

        # Refuse to trade a portfolio valued on stale prices.
        stale = [p.symbol for p in view_before.positions if p.price_is_stale]
        if stale and len(stale) == len(view_before.positions) and view_before.positions:
            report.halted = True
            report.halt_reason = (
                f"every held position has a stale price ({', '.join(stale[:5])}); "
                "trading halted until data is fresh"
            )
            self._event("paper_halted_stale_data", report.halt_reason, EventSeverity.WARNING)
            self.db.commit()
            return report

        self._process_exits(report)
        self._process_entries(report, max_new_positions)

        self.portfolios.snapshot(self.portfolio, commit=False)
        view_after = self.portfolios.value(self.portfolio)
        report.equity_after = view_after.equity
        self.db.commit()
        return report

    # ------------------------------------------------------------------ exits
    def _process_exits(self, report: PaperRunReport) -> None:
        for trigger in self.risk.check_exits(self.portfolio):
            holding = self.db.get(Holding, trigger["holding_id"])
            if holding is None or holding.status is not PositionStatus.OPEN:
                continue
            reason = (
                ExitReason.STOP_LOSS
                if trigger["reason"] == "STOP_LOSS"
                else ExitReason.TAKE_PROFIT
            )
            try:
                result = self.orders.close_position(
                    self.portfolio, holding, reason=reason, commit=False
                )
            except (UnknownPriceError, DuplicateOrderError, KillSwitchEngaged) as exc:
                report.errors.append(f"exit {trigger['security_id']}: {exc}")
                continue
            if result.executed:
                report.closed.append({**result.as_dict(), "trigger": trigger["reason"]})

        # Close positions whose opening signal has since reversed.
        open_holdings = self.db.scalars(
            select(Holding).where(
                Holding.portfolio_id == self.portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).all()
        for holding in open_holdings:
            latest = self.db.scalars(
                select(Signal)
                .where(
                    Signal.security_id == holding.security_id,
                    Signal.status == SignalStatus.ACTIVE,
                )
                .order_by(Signal.generated_at.desc())
                .limit(1)
            ).first()
            if latest is None or latest.signal not in (SignalType.SELL, SignalType.STRONG_SELL):
                continue
            try:
                result = self.orders.close_position(
                    self.portfolio, holding, reason=ExitReason.SIGNAL_EXIT, commit=False
                )
            except (UnknownPriceError, DuplicateOrderError, KillSwitchEngaged) as exc:
                report.errors.append(f"signal exit {holding.security_id}: {exc}")
                continue
            if result.executed:
                report.closed.append({**result.as_dict(), "trigger": "SIGNAL_REVERSAL"})

    # ---------------------------------------------------------------- entries
    def _process_entries(self, report: PaperRunReport, max_new_positions: int) -> None:
        now = datetime.now(timezone.utc)
        candidates = self.db.scalars(
            select(Signal)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.expires_at > now,
                Signal.signal.in_([SignalType.BUY, SignalType.STRONG_BUY]),
                Signal.confidence >= settings.signal_min_confidence,
            )
            .order_by(Signal.opportunity_score.desc().nullslast())
            .limit(max_new_positions * 4)
        ).all()

        opened = 0
        for signal in candidates:
            if opened >= max_new_positions:
                break
            security = self.db.get(Security, signal.security_id)
            if security is None:
                continue

            atr_value, volatility = self._volatility(security.id)
            try:
                result = self.orders.open_position(
                    self.portfolio, security,
                    confidence=float(signal.confidence),
                    atr=atr_value, volatility=volatility,
                    signal_id=signal.id, commit=False,
                )
            except DuplicateOrderError:
                report.skipped.append({"symbol": security.symbol, "reason": "already ordered today"})
                continue
            except (UnknownPriceError, KillSwitchEngaged) as exc:
                report.skipped.append({"symbol": security.symbol, "reason": str(exc)})
                continue

            if result.executed:
                opened += 1
                report.opened.append(result.as_dict())
            else:
                report.skipped.append({"symbol": security.symbol, "reason": result.reason})

    def _volatility(self, security_id: int) -> tuple[float | None, float | None]:
        rows = self.db.execute(
            select(PriceData.high, PriceData.low, PriceData.close)
            .where(PriceData.security_id == security_id)
            .order_by(PriceData.trade_date.desc())
            .limit(120)
        ).all()
        if len(rows) < 30:
            return None, None

        import pandas as pd

        rows = list(reversed(rows))
        frame = pd.DataFrame(
            {
                "high": [float(r[0]) for r in rows],
                "low": [float(r[1]) for r in rows],
                "close": [float(r[2]) for r in rows],
            }
        )
        atr_series = ta.atr(frame["high"], frame["low"], frame["close"], 14)
        vol_series = ta.realized_volatility(frame["close"], 20)
        atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else None
        volatility = float(vol_series.iloc[-1]) if not pd.isna(vol_series.iloc[-1]) else None
        return atr_value, volatility

    def _event(self, event_type: str, message: str, severity) -> None:
        self.db.add(
            SystemEvent(
                component="paper_trading", event_type=event_type, severity=severity,
                message=message, created_at=datetime.now(timezone.utc),
            )
        )


def get_or_create_paper_portfolio(
    db: Session, user_id: int, *, name: str = "Paper Portfolio",
    currency: str = "USD",
) -> Portfolio:
    from app.models.enums import TradingMode

    portfolio = db.scalars(
        select(Portfolio).where(Portfolio.user_id == user_id, Portfolio.name == name)
    ).first()
    if portfolio is not None:
        return portfolio

    portfolio = Portfolio(
        user_id=user_id, name=name, mode=TradingMode.PAPER, currency=currency,
        starting_cash=settings.paper_starting_cash,
        cash=settings.paper_starting_cash,
        peak_equity=settings.paper_starting_cash,
    )
    db.add(portfolio)
    db.commit()
    return portfolio
