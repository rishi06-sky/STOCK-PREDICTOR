"""Backtester honesty, data validation and sentiment behaviour."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting.engine import Backtester, BacktestConfig, compute_performance_metrics
from app.market_data.types import Bar, QuoteData
from app.market_data.validation import detect_gaps, validate_bar, validate_bars, validate_quote
from app.models.enums import DataQuality
from app.sentiment.analyzer import aggregate_sentiment, analyze
from app.models.enums import SentimentLabel


def _panel(n_securities: int = 5, n: int = 400, seed: int = 3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    prices, symbols = {}, {}
    for sid in range(n_securities):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.014, n)))
        prices[sid] = pd.DataFrame(
            {
                "open": close * (1 + rng.normal(0, 0.002, n)),
                "high": close * (1 + np.abs(rng.normal(0, 0.008, n))),
                "low": close * (1 - np.abs(rng.normal(0, 0.008, n))),
                "close": close,
            },
            index=dates,
        )
        symbols[sid] = f"SYM{sid}"
    return prices, symbols, dates, rng


class TestBacktesterHonesty:
    def test_random_signals_do_not_produce_free_money(self):
        prices, symbols, dates, rng = _panel()
        signals = pd.DataFrame(
            [
                {"date": d, "security_id": s, "probability": rng.random()}
                for d in dates[:-1] for s in range(5)
            ]
        )
        result = Backtester(BacktestConfig()).run(prices, signals, symbols=symbols)
        # Random entries pay costs and gain no edge.
        assert result.metrics["total_return"] < 0.05
        assert result.metrics["total_costs"] > 0

    def test_costs_reduce_returns(self):
        prices, symbols, dates, rng = _panel()
        signals = pd.DataFrame(
            [
                {"date": d, "security_id": s, "probability": rng.random()}
                for d in dates[:-1] for s in range(5)
            ]
        )
        with_costs = Backtester(
            BacktestConfig(commission_bps=10, slippage_bps=10)
        ).run(prices, signals, symbols=symbols)
        without_costs = Backtester(
            BacktestConfig(commission_bps=0, slippage_bps=0)
        ).run(prices, signals, symbols=symbols)
        assert with_costs.metrics["total_return"] < without_costs.metrics["total_return"]
        assert without_costs.metrics["total_costs"] == 0

    def test_a_look_ahead_oracle_is_profitable(self):
        """Proves the engine can express an edge -- it is not merely lossy."""
        prices, symbols, dates, _ = _panel()
        rows = []
        for sid in range(5):
            close = prices[sid]["close"]
            forward = close.shift(-5) / close - 1
            for d in dates[:-6]:
                rows.append(
                    {"date": d, "security_id": sid,
                     "probability": 0.95 if forward.loc[d] > 0.01 else 0.05}
                )
        result = Backtester(BacktestConfig()).run(prices, pd.DataFrame(rows), symbols=symbols)
        assert result.metrics["total_return"] > 0.2
        assert result.metrics["win_rate"] > 0.55

    def test_entries_fill_on_the_bar_after_the_signal(self):
        prices, symbols, dates, _ = _panel(n_securities=1, n=60)
        # A single signal on one date.
        signal_date = dates[30]
        signals = pd.DataFrame([{"date": signal_date, "security_id": 0, "probability": 0.99}])
        result = Backtester(
            BacktestConfig(max_positions=1, stop_loss_pct=None, take_profit_pct=None)
        ).run(prices, signals, symbols=symbols)
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Filled the NEXT session, not the signal's own bar.
        assert trade.entry_date > signal_date.date()
        next_open = float(prices[0]["open"].loc[dates[31]])
        assert trade.entry_price > next_open  # adverse slippage applied

    def test_stop_wins_when_a_bar_spans_both_levels(self):
        dates = pd.bdate_range("2024-01-01", periods=6)
        frame = pd.DataFrame(
            {
                "open": [100.0, 100, 100, 100, 100, 100],
                # Third bar's range covers both the stop and the target.
                "high": [101.0, 101, 130, 101, 101, 101],
                "low": [99.0, 99, 70, 99, 99, 99],
                "close": [100.0, 100, 100, 100, 100, 100],
            },
            index=dates,
        )
        signals = pd.DataFrame([{"date": dates[0], "security_id": 0, "probability": 0.99}])
        result = Backtester(
            BacktestConfig(stop_loss_pct=0.08, take_profit_pct=0.15, max_positions=1)
        ).run({0: frame}, signals, symbols={0: "X"})
        assert result.trades[0].exit_reason == "STOP_LOSS"

    def test_too_few_trades_is_flagged(self):
        prices, symbols, dates, _ = _panel(n_securities=1, n=60)
        signals = pd.DataFrame([{"date": dates[10], "security_id": 0, "probability": 0.99}])
        result = Backtester(BacktestConfig(max_positions=1)).run(
            prices, signals, symbols=symbols
        )
        assert any("not statistically meaningful" in w for w in result.warnings)

    def test_empty_signals_are_refused(self):
        prices, symbols, _, _ = _panel()
        with pytest.raises(ValueError):
            Backtester().run(prices, pd.DataFrame(), symbols=symbols)


class TestPerformanceMetrics:
    def test_flat_equity_has_zero_return_and_no_drawdown(self):
        equity = pd.Series([100.0] * 100, index=pd.bdate_range("2024-01-01", periods=100))
        metrics = compute_performance_metrics(equity, [], initial_capital=100.0)
        assert metrics["total_return"] == pytest.approx(0.0)
        assert metrics["max_drawdown"] == pytest.approx(0.0)

    def test_drawdown_is_measured_from_the_peak(self):
        equity = pd.Series(
            [100, 120, 90, 95], index=pd.bdate_range("2024-01-01", periods=4), dtype=float
        )
        metrics = compute_performance_metrics(equity, [], initial_capital=100.0)
        assert metrics["max_drawdown"] == pytest.approx(90 / 120 - 1)

    def test_short_curve_is_reported_not_guessed(self):
        equity = pd.Series([100.0], index=pd.bdate_range("2024-01-01", periods=1))
        assert "error" in compute_performance_metrics(equity, [], initial_capital=100.0)


class TestBarValidation:
    def _bar(self, **kwargs):
        defaults = dict(
            symbol="X", trade_date=date(2026, 1, 5), open=100, high=105,
            low=99, close=104, volume=1000, provider="t",
        )
        return Bar(**{**defaults, **kwargs})

    def test_a_good_bar_passes(self):
        assert validate_bar(self._bar()) is None

    @pytest.mark.parametrize(
        "kwargs,fragment",
        [
            (dict(high=90, low=95), "high < low"),
            (dict(close=-5), "non-positive"),
            (dict(open=0), "non-positive"),
            (dict(high=100, open=110), "high is not"),
            (dict(low=101, close=100, open=100, high=102), "low is not"),
            (dict(trade_date=date(2026, 1, 10)), "weekend"),
            (dict(trade_date=date(2099, 1, 5)), "future"),
        ],
    )
    def test_bad_bars_are_rejected(self, kwargs, fragment):
        reason = validate_bar(self._bar(**kwargs))
        assert reason is not None and fragment in reason

    def test_duplicates_keep_the_latest(self):
        # Both must be individually valid, or the restated bar is rejected on
        # its own merits before deduplication ever sees it.
        first = self._bar(close=100, high=105)
        second = self._bar(close=104, high=106)
        result = validate_bars([first, second])
        assert len(result.valid) == 1
        assert float(result.valid[0].close) == 104.0
        assert any("duplicate" in w for w in result.warnings)

    def test_an_unadjusted_split_is_quarantined(self):
        bar = self._bar(open=200, high=210, low=195, close=205)
        result = validate_bars([bar], previous_close=100)
        assert result.valid == []
        assert "split" in result.rejected[0][1]

    def test_gaps_are_detected(self):
        gaps = detect_gaps(
            [self._bar(trade_date=date(2026, 1, 5)), self._bar(trade_date=date(2026, 3, 5))]
        )
        assert len(gaps) == 1 and gaps[0][2] > 30


class TestQuoteValidation:
    def test_a_fresh_quote_passes(self):
        quote = QuoteData(
            "X", 100.0, datetime.now(timezone.utc), "p", DataQuality.DELAYED
        )
        assert validate_quote(quote, max_age_seconds=900) is None

    def test_a_stale_quote_is_flagged(self):
        quote = QuoteData(
            "X", 100.0, datetime.now(timezone.utc) - timedelta(hours=5), "p",
            DataQuality.DELAYED,
        )
        reason = validate_quote(quote, max_age_seconds=900)
        assert reason is not None and "stale" in reason

    def test_a_future_timestamp_is_rejected(self):
        quote = QuoteData(
            "X", 100.0, datetime.now(timezone.utc) + timedelta(hours=2), "p",
            DataQuality.DELAYED,
        )
        assert "future" in validate_quote(quote, max_age_seconds=900)

    def test_a_non_positive_price_is_rejected(self):
        quote = QuoteData("X", 0.0, datetime.now(timezone.utc), "p", DataQuality.DELAYED)
        assert "non-positive" in validate_quote(quote, max_age_seconds=900)


class TestSentiment:
    def test_positive_headline(self):
        result = analyze("Company beats estimates as profit surges to a record high")
        assert result.label is SentimentLabel.POSITIVE
        assert result.score > 0

    def test_negative_headline(self):
        result = analyze("Shares plunge after fraud investigation and bankruptcy warning")
        assert result.label is SentimentLabel.NEGATIVE
        assert result.score < 0

    def test_negation_flips_polarity(self):
        plain = analyze("Company beats estimates")
        negated = analyze("Company does not beat estimates")
        assert plain.score > 0 > negated.score

    def test_empty_text_has_no_confidence(self):
        result = analyze("")
        assert result.confidence == 0.0
        assert result.label is SentimentLabel.NEUTRAL

    def test_text_without_sentiment_words_is_low_confidence(self):
        result = analyze("The board meeting is scheduled for Tuesday afternoon")
        assert result.confidence < 0.3

    def test_disagreement_lowers_aggregate_confidence(self):
        agreeing = [
            analyze("Stock surges on record profit"),
            analyze("Shares rally after strong earnings beat"),
        ]
        conflicting = [
            analyze("Stock surges on record profit"),
            analyze("Shares plunge after fraud probe and bankruptcy warning"),
        ]
        assert (
            aggregate_sentiment(conflicting)["confidence"]
            < aggregate_sentiment(agreeing)["confidence"]
        )

    def test_empty_aggregate_is_neutral(self):
        assert aggregate_sentiment([])["count"] == 0
