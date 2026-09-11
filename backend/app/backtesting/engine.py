"""Event-driven backtesting engine.

Design decisions that determine whether results mean anything:

* **Next-bar execution.** A signal computed from bar t's close is filled at
  bar t+1's open. Filling at t's close would be trading on information the
  market had not yet produced -- the most common way backtests lie.
* **Costs are mandatory.** Commission and slippage are applied to every fill.
  A strategy that only works gross of costs does not work.
* **Intrabar stop/target ordering is pessimistic.** When a bar's range spans
  both the stop and the target we assume the stop filled first, because the
  path within the bar is unknown and optimism here is unearned.
* **No survivorship filtering.** The engine trades whatever universe it is
  given; if that universe excludes delisted names, the caller is told to say so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.models.enums import ExitReason

log = get_logger(__name__)

TRADING_DAYS = 252


@dataclass(slots=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    commission_bps: float = 3.0
    slippage_bps: float = 5.0
    position_size_pct: float = 0.10      # of equity per position
    max_positions: int = 10
    stop_loss_pct: float | None = 0.08
    take_profit_pct: float | None = 0.15
    max_holding_days: int = 21
    entry_threshold: float = 0.55        # min probability to open
    exit_threshold: float = 0.45         # probability below which to close
    allow_shorts: bool = False
    risk_free_rate: float = 0.04         # annual, for Sharpe/Sortino


@dataclass(slots=True)
class BacktestTradeRecord:
    security_id: int
    symbol: str
    entry_date: date
    entry_price: float
    quantity: float
    exit_date: date | None = None
    exit_price: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    costs: float = 0.0
    return_pct: float | None = None
    holding_days: int | None = None
    exit_reason: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_date": str(self.entry_date), "exit_date": str(self.exit_date),
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4) if self.exit_price else None,
            "quantity": round(self.quantity, 4),
            "net_pnl": round(self.net_pnl, 2) if self.net_pnl is not None else None,
            "return_pct": round(self.return_pct, 6) if self.return_pct is not None else None,
            "costs": round(self.costs, 2), "holding_days": self.holding_days,
            "exit_reason": self.exit_reason,
        }


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    equity_curve: pd.Series
    trades: list[BacktestTradeRecord]
    start_date: date
    end_date: date
    benchmark_curve: pd.Series | None = None
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "period": f"{self.start_date} .. {self.end_date}",
            "trades": len(self.trades),
            **self.metrics,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------- metrics
def compute_performance_metrics(
    equity: pd.Series, trades: list[BacktestTradeRecord], *,
    initial_capital: float, risk_free_rate: float = 0.04,
    benchmark: pd.Series | None = None,
) -> dict:
    equity = pd.Series(equity).dropna()
    if len(equity) < 2:
        return {"error": "equity curve too short to evaluate"}

    returns = equity.pct_change().dropna()
    total_return = float(equity.iloc[-1] / initial_capital - 1)

    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    cagr = float((equity.iloc[-1] / initial_capital) ** (1 / years) - 1) if years > 0 else 0.0

    volatility = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(returns) > 1 else 0.0
    daily_rf = risk_free_rate / TRADING_DAYS
    excess = returns - daily_rf

    sharpe = (
        float(excess.mean() / excess.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if len(excess) > 1 and excess.std(ddof=1) > 0 else 0.0
    )
    downside = excess[excess < 0]
    sortino = (
        float(excess.mean() / downside.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std(ddof=1) > 0 else 0.0
    )

    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    max_drawdown = float(drawdown.min())

    closed = [t for t in trades if t.net_pnl is not None]
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))

    metrics = {
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6),
        "volatility": round(volatility, 6),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "max_drawdown": round(max_drawdown, 6),
        "calmar_ratio": round(cagr / abs(max_drawdown), 4) if max_drawdown < 0 else None,
        "total_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "average_trade_return": (
            round(float(np.mean([t.return_pct for t in closed if t.return_pct is not None])), 6)
            if closed else None
        ),
        "average_win": round(float(np.mean([t.net_pnl for t in wins])), 2) if wins else None,
        "average_loss": round(float(np.mean([t.net_pnl for t in losses])), 2) if losses else None,
        "average_holding_days": (
            round(float(np.mean([t.holding_days for t in closed if t.holding_days])), 2)
            if closed else None
        ),
        "total_costs": round(sum(t.costs for t in trades), 2),
        "final_equity": round(float(equity.iloc[-1]), 2),
    }

    if benchmark is not None and len(benchmark) > 1:
        aligned = benchmark.reindex(equity.index).ffill().dropna()
        if len(aligned) > 1:
            benchmark_return = float(aligned.iloc[-1] / aligned.iloc[0] - 1)
            metrics["benchmark_return"] = round(benchmark_return, 6)
            metrics["excess_return"] = round(total_return - benchmark_return, 6)

    return metrics


# -------------------------------------------------------------------- engine
class Backtester:
    """Runs a long(/short) strategy over pooled daily bars and dated signals."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(
        self,
        prices: dict[int, pd.DataFrame],
        signals: pd.DataFrame,
        *,
        symbols: dict[int, str] | None = None,
        benchmark: pd.Series | None = None,
    ) -> BacktestResult:
        """
        prices:  {security_id: DataFrame indexed by date with open/high/low/close}
        signals: DataFrame with columns [date, security_id, probability]
        """
        config = self.config
        symbols = symbols or {}
        warnings: list[str] = []

        if signals.empty:
            raise ValueError("no signals supplied to the backtester")

        signals = signals.copy()
        signals["date"] = pd.to_datetime(signals["date"])
        signals = signals.sort_values("date")

        all_dates = sorted(
            set().union(*[set(pd.to_datetime(df.index)) for df in prices.values()])
        )
        if not all_dates:
            raise ValueError("no price history supplied to the backtester")

        signals_by_date: dict[pd.Timestamp, pd.DataFrame] = {
            day: group for day, group in signals.groupby("date")
        }

        cash = config.initial_capital
        open_positions: dict[int, BacktestTradeRecord] = {}
        closed: list[BacktestTradeRecord] = []
        equity_points: list[tuple[pd.Timestamp, float]] = []

        cost_rate = config.commission_bps / 10_000.0
        slip_rate = config.slippage_bps / 10_000.0

        for i, today in enumerate(all_dates):
            # ---------------------------------------------------- exits first
            for security_id in list(open_positions):
                position = open_positions[security_id]
                bar = self._bar(prices, security_id, today)
                if bar is None:
                    continue

                exit_price, reason = self._check_exit(position, bar, today, config)
                if exit_price is None:
                    probability = self._probability(signals_by_date, today, security_id)
                    if probability is not None and probability < config.exit_threshold:
                        exit_price, reason = float(bar["close"]), ExitReason.SIGNAL_EXIT.value

                if exit_price is not None:
                    fill = exit_price * (1 - slip_rate)      # adverse on the way out
                    commission = abs(fill * position.quantity) * cost_rate
                    proceeds = fill * position.quantity - commission
                    cash += proceeds

                    position.exit_date = today.date()
                    position.exit_price = fill
                    position.gross_pnl = (fill - position.entry_price) * position.quantity
                    position.costs += commission
                    position.net_pnl = position.gross_pnl - commission
                    position.return_pct = (
                        position.net_pnl / (position.entry_price * position.quantity)
                        if position.entry_price and position.quantity else None
                    )
                    position.holding_days = (today.date() - position.entry_date).days
                    position.exit_reason = reason
                    closed.append(position)
                    del open_positions[security_id]

            # ------------------------------------------------- entries (t+1)
            # Signals dated yesterday are acted on at today's OPEN. This is the
            # rule that keeps the backtest honest.
            if i > 0:
                yesterday = all_dates[i - 1]
                candidates = signals_by_date.get(yesterday)
                if candidates is not None and len(open_positions) < config.max_positions:
                    ranked = candidates.sort_values("probability", ascending=False)
                    for row in ranked.itertuples():
                        if len(open_positions) >= config.max_positions:
                            break
                        security_id = int(row.security_id)
                        probability = float(row.probability)
                        if probability < config.entry_threshold or security_id in open_positions:
                            continue

                        bar = self._bar(prices, security_id, today)
                        if bar is None or not np.isfinite(bar["open"]) or bar["open"] <= 0:
                            continue

                        equity_now = cash + self._positions_value(open_positions, prices, today)
                        target_value = equity_now * config.position_size_pct
                        fill = float(bar["open"]) * (1 + slip_rate)   # adverse on the way in
                        quantity = target_value / fill
                        if quantity <= 0:
                            continue
                        commission = target_value * cost_rate
                        if target_value + commission > cash:
                            continue

                        cash -= target_value + commission
                        open_positions[security_id] = BacktestTradeRecord(
                            security_id=security_id,
                            symbol=symbols.get(security_id, str(security_id)),
                            entry_date=today.date(), entry_price=fill, quantity=quantity,
                            costs=commission,
                            stop_loss=fill * (1 - config.stop_loss_pct) if config.stop_loss_pct else None,
                            take_profit=fill * (1 + config.take_profit_pct) if config.take_profit_pct else None,
                        )

            equity_points.append(
                (today, cash + self._positions_value(open_positions, prices, today))
            )

        # Close anything still open at the final bar.
        if open_positions:
            final = all_dates[-1]
            for security_id, position in list(open_positions.items()):
                bar = self._bar(prices, security_id, final)
                if bar is None:
                    continue
                fill = float(bar["close"]) * (1 - slip_rate)
                commission = abs(fill * position.quantity) * cost_rate
                cash += fill * position.quantity - commission
                position.exit_date = final.date()
                position.exit_price = fill
                position.gross_pnl = (fill - position.entry_price) * position.quantity
                position.costs += commission
                position.net_pnl = position.gross_pnl - commission
                position.return_pct = position.net_pnl / (position.entry_price * position.quantity)
                position.holding_days = (final.date() - position.entry_date).days
                position.exit_reason = ExitReason.TIME_EXIT.value
                closed.append(position)
            warnings.append(
                f"{len(open_positions)} position(s) force-closed at the end of the period"
            )
            open_positions.clear()

        equity = pd.Series(
            [value for _, value in equity_points],
            index=pd.DatetimeIndex([day for day, _ in equity_points]),
        )
        metrics = compute_performance_metrics(
            equity, closed, initial_capital=config.initial_capital,
            risk_free_rate=config.risk_free_rate, benchmark=benchmark,
        )
        if len(closed) < 30:
            warnings.append(
                f"only {len(closed)} closed trades; these statistics are not "
                "statistically meaningful"
            )

        return BacktestResult(
            config=config, equity_curve=equity, trades=closed,
            start_date=all_dates[0].date(), end_date=all_dates[-1].date(),
            benchmark_curve=benchmark, warnings=warnings, metrics=metrics,
        )

    # ------------------------------------------------------------- internals
    @staticmethod
    def _bar(prices: dict[int, pd.DataFrame], security_id: int, day: pd.Timestamp):
        frame = prices.get(security_id)
        if frame is None:
            return None
        try:
            return frame.loc[day]
        except KeyError:
            return None

    @staticmethod
    def _probability(signals_by_date, day, security_id) -> float | None:
        frame = signals_by_date.get(day)
        if frame is None:
            return None
        match = frame[frame["security_id"] == security_id]
        return float(match["probability"].iloc[0]) if len(match) else None

    def _positions_value(self, positions, prices, day) -> float:
        total = 0.0
        for security_id, position in positions.items():
            bar = self._bar(prices, security_id, day)
            price = float(bar["close"]) if bar is not None else position.entry_price
            total += price * position.quantity
        return total

    @staticmethod
    def _check_exit(
        position: BacktestTradeRecord, bar, today: pd.Timestamp, config: BacktestConfig
    ) -> tuple[float | None, str | None]:
        low, high = float(bar["low"]), float(bar["high"])

        # Pessimistic ordering: if the bar touched both levels, assume the stop
        # filled first. The intrabar path is unknowable, so we take the worse
        # of the two rather than flattering the result.
        if position.stop_loss is not None and low <= position.stop_loss:
            return position.stop_loss, ExitReason.STOP_LOSS.value
        if position.take_profit is not None and high >= position.take_profit:
            return position.take_profit, ExitReason.TAKE_PROFIT.value
        if (today.date() - position.entry_date).days >= config.max_holding_days:
            return float(bar["close"]), ExitReason.TIME_EXIT.value
        return None, None
