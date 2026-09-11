"""Portfolio valuation, allocation and diversification analytics."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import DataQuality, PositionStatus
from app.models.market import PriceData, Quote, Security
from app.models.trading import Holding, Portfolio, PortfolioSnapshot, Trade

log = get_logger(__name__)


@dataclass(slots=True)
class PositionView:
    security_id: int
    symbol: str
    name: str
    sector: str | None
    quantity: float
    average_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    weight: float
    stop_loss: float | None
    take_profit: float | None
    price_is_stale: bool
    opened_at: datetime

    def as_dict(self) -> dict:
        return {
            "security_id": self.security_id, "symbol": self.symbol, "name": self.name,
            "sector": self.sector, "quantity": round(self.quantity, 4),
            "average_cost": round(self.average_cost, 4),
            "current_price": round(self.current_price, 4),
            "market_value": round(self.market_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 6),
            "weight": round(self.weight, 6),
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "take_profit": round(self.take_profit, 4) if self.take_profit else None,
            "price_is_stale": self.price_is_stale,
            "opened_at": self.opened_at.isoformat(),
        }


@dataclass(slots=True)
class PortfolioView:
    portfolio_id: int
    name: str
    mode: str
    currency: str
    cash: float
    positions_value: float
    equity: float
    starting_cash: float
    total_pnl: float
    total_pnl_pct: float
    unrealized_pnl: float
    realized_pnl: float
    exposure_pct: float
    drawdown_pct: float
    peak_equity: float
    open_positions: int
    positions: list[PositionView] = field(default_factory=list)
    sector_allocation: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "portfolio_id": self.portfolio_id, "name": self.name, "mode": self.mode,
            "currency": self.currency,
            "cash": round(self.cash, 2),
            "positions_value": round(self.positions_value, 2),
            "equity": round(self.equity, 2),
            "starting_cash": round(self.starting_cash, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "exposure_pct": round(self.exposure_pct, 6),
            "drawdown_pct": round(self.drawdown_pct, 6),
            "peak_equity": round(self.peak_equity, 2),
            "open_positions": self.open_positions,
            "positions": [p.as_dict() for p in self.positions],
            "sector_allocation": {k: round(v, 6) for k, v in self.sector_allocation.items()},
            "warnings": self.warnings,
        }


class PortfolioEngine:
    def __init__(self, db: Session):
        self.db = db

    def value(self, portfolio: Portfolio) -> PortfolioView:
        holdings = self.db.execute(
            select(Holding, Security)
            .join(Security, Security.id == Holding.security_id)
            .where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).all()

        warnings: list[str] = []
        positions: list[PositionView] = []
        positions_value = 0.0
        unrealized = 0.0
        now = datetime.now(timezone.utc)

        for holding, security in holdings:
            quote = self.db.get(Quote, holding.security_id)
            stale = True
            if quote is not None:
                price = float(quote.price)
                if quote.quality is DataQuality.SYNTHETIC:
                    stale = False
                else:
                    age = (now - quote.source_timestamp).total_seconds()
                    stale = age > settings.quote_staleness_seconds
            else:
                # No quote at all: fall back to the last stored close and say so.
                last = self.db.scalars(
                    select(PriceData)
                    .where(PriceData.security_id == holding.security_id)
                    .order_by(PriceData.trade_date.desc())
                    .limit(1)
                ).first()
                price = float(last.close) if last else float(holding.average_cost)
                warnings.append(
                    f"{security.symbol}: no live quote; valued at "
                    f"{'last close' if last else 'cost'}"
                )

            if stale and quote is not None:
                warnings.append(f"{security.symbol}: quote is stale; valuation may be out of date")

            quantity = float(holding.quantity)
            cost = float(holding.average_cost)
            market_value = price * quantity
            position_pnl = (price - cost) * quantity

            positions_value += market_value
            unrealized += position_pnl
            positions.append(
                PositionView(
                    security_id=security.id, symbol=security.symbol, name=security.name,
                    sector=security.sector, quantity=quantity, average_cost=cost,
                    current_price=price, market_value=market_value,
                    unrealized_pnl=position_pnl,
                    unrealized_pnl_pct=(position_pnl / (cost * quantity)) if cost and quantity else 0.0,
                    weight=0.0,
                    stop_loss=float(holding.stop_loss) if holding.stop_loss else None,
                    take_profit=float(holding.take_profit) if holding.take_profit else None,
                    price_is_stale=stale, opened_at=holding.opened_at,
                )
            )

        cash = float(portfolio.cash)
        equity = cash + positions_value
        for position in positions:
            position.weight = position.market_value / equity if equity > 0 else 0.0

        realized = float(
            self.db.scalar(
                select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(
                    Trade.portfolio_id == portfolio.id
                )
            )
            or 0.0
        )

        peak = max(float(portfolio.peak_equity or portfolio.starting_cash), equity)
        starting = float(portfolio.starting_cash)

        sector_allocation: dict[str, float] = {}
        for position in positions:
            key = position.sector or "Unclassified"
            sector_allocation[key] = sector_allocation.get(key, 0.0) + position.weight

        if equity > 0:
            for sector, weight in sector_allocation.items():
                if weight > settings.risk_max_sector_pct:
                    warnings.append(
                        f"sector '{sector}' is {weight:.0%} of the portfolio, above the "
                        f"{settings.risk_max_sector_pct:.0%} limit"
                    )

        return PortfolioView(
            portfolio_id=portfolio.id, name=portfolio.name, mode=str(portfolio.mode),
            currency=portfolio.currency, cash=cash, positions_value=positions_value,
            equity=equity, starting_cash=starting,
            total_pnl=equity - starting,
            total_pnl_pct=(equity / starting - 1) if starting else 0.0,
            unrealized_pnl=unrealized, realized_pnl=realized,
            exposure_pct=(positions_value / equity) if equity > 0 else 0.0,
            drawdown_pct=(equity / peak - 1) if peak > 0 else 0.0,
            peak_equity=peak, open_positions=len(positions),
            positions=sorted(positions, key=lambda p: p.market_value, reverse=True),
            sector_allocation=dict(
                sorted(sector_allocation.items(), key=lambda kv: kv[1], reverse=True)
            ),
            warnings=warnings,
        )

    def snapshot(self, portfolio: Portfolio, *, commit: bool = True) -> PortfolioSnapshot:
        """Record today's equity point and advance the high-water mark."""
        view = self.value(portfolio)
        today = datetime.now(timezone.utc).date()

        if view.equity > float(portfolio.peak_equity or 0):
            portfolio.peak_equity = view.equity

        stmt = pg_insert(PortfolioSnapshot).values(
            portfolio_id=portfolio.id, snapshot_date=today,
            cash=view.cash, positions_value=view.positions_value, equity=view.equity,
            unrealized_pnl=view.unrealized_pnl, realized_pnl=view.realized_pnl,
            open_positions=view.open_positions, exposure_pct=view.exposure_pct,
            drawdown_pct=view.drawdown_pct, created_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_portfolio_snapshots_pf_date",
            set_={
                c: getattr(stmt.excluded, c)
                for c in ("cash", "positions_value", "equity", "unrealized_pnl",
                          "realized_pnl", "open_positions", "exposure_pct", "drawdown_pct")
            },
        )
        self.db.execute(stmt)
        if commit:
            self.db.commit()
        return self.db.scalars(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == portfolio.id,
                PortfolioSnapshot.snapshot_date == today,
            )
        ).first()

    def equity_curve(self, portfolio_id: int, *, days: int = 365) -> pd.Series:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        rows = self.db.execute(
            select(PortfolioSnapshot.snapshot_date, PortfolioSnapshot.equity)
            .where(
                PortfolioSnapshot.portfolio_id == portfolio_id,
                PortfolioSnapshot.snapshot_date >= cutoff,
            )
            .order_by(PortfolioSnapshot.snapshot_date)
        ).all()
        if not rows:
            return pd.Series(dtype="float64")
        return pd.Series(
            [float(equity) for _, equity in rows],
            index=pd.DatetimeIndex([day for day, _ in rows]),
        )

    def live_performance(self, portfolio: Portfolio) -> dict:
        """Realised performance from the actual equity curve.

        Explicitly labelled LIVE so it is never confused with a backtest.
        """
        curve = self.equity_curve(portfolio.id)
        if len(curve) < 2:
            return {
                "basis": "LIVE",
                "note": "not enough snapshots yet for performance statistics",
                "snapshots": int(len(curve)),
            }

        from app.backtesting.engine import compute_performance_metrics

        metrics = compute_performance_metrics(
            curve, [], initial_capital=float(portfolio.starting_cash)
        )
        metrics.update({"basis": "LIVE", "snapshots": int(len(curve))})
        # Trade statistics come from the ledger, not from the equity curve.
        closed = self.db.scalars(
            select(Trade).where(
                Trade.portfolio_id == portfolio.id, Trade.realized_pnl.isnot(None)
            )
        ).all()
        if closed:
            wins = [t for t in closed if float(t.realized_pnl) > 0]
            metrics["total_trades"] = len(closed)
            metrics["win_rate"] = round(len(wins) / len(closed), 4)
        return metrics

    def correlation_matrix(self, portfolio: Portfolio, *, days: int = 180) -> dict:
        """Return correlations between holdings, for diversification analysis."""
        holdings = self.db.scalars(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id,
                Holding.status == PositionStatus.OPEN,
            )
        ).all()
        if len(holdings) < 2:
            return {"note": "at least two open positions are required", "matrix": {}}

        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        series: dict[str, pd.Series] = {}
        for holding in holdings:
            security = self.db.get(Security, holding.security_id)
            rows = self.db.execute(
                select(PriceData.trade_date, PriceData.close)
                .where(
                    PriceData.security_id == holding.security_id,
                    PriceData.trade_date >= cutoff,
                )
                .order_by(PriceData.trade_date)
            ).all()
            if len(rows) > 20:
                series[security.symbol] = pd.Series(
                    [float(close) for _, close in rows],
                    index=pd.DatetimeIndex([day for day, _ in rows]),
                )

        if len(series) < 2:
            return {"note": "insufficient overlapping price history", "matrix": {}}

        returns = pd.DataFrame(series).pct_change().dropna()
        if len(returns) < 20:
            return {"note": "insufficient overlapping observations", "matrix": {}}

        corr = returns.corr()
        pairs = []
        symbols = list(corr.columns)
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                value = float(corr.loc[a, b])
                if np.isfinite(value):
                    pairs.append({"a": a, "b": b, "correlation": round(value, 4)})
        pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)

        average = float(np.mean([abs(p["correlation"]) for p in pairs])) if pairs else 0.0
        return {
            "matrix": {a: {b: round(float(corr.loc[a, b]), 4) for b in symbols} for a in symbols},
            "highest_pairs": pairs[:10],
            "average_absolute_correlation": round(average, 4),
            "diversification_note": (
                "holdings move together closely; diversification benefit is limited"
                if average > 0.7
                else "holdings are reasonably diversified"
            ),
            "observations": int(len(returns)),
        }
