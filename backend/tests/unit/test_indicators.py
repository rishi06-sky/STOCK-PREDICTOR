"""Indicator correctness, bounds and -- most importantly -- causality."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.technical import indicators as ta


class TestBounds:
    def test_rsi_is_bounded(self, ohlcv):
        rsi = ta.rsi(ohlcv["close"]).dropna()
        assert len(rsi) > 300
        assert rsi.between(0, 100).all()

    def test_adx_is_bounded(self, ohlcv):
        adx = ta.adx(ohlcv["high"], ohlcv["low"], ohlcv["close"])["adx"].dropna()
        assert adx.between(0, 100).all()

    def test_stochastic_is_bounded(self, ohlcv):
        k = ta.stochastic(ohlcv["high"], ohlcv["low"], ohlcv["close"])["stoch_k"].dropna()
        assert k.between(0, 100).all()

    def test_money_flow_index_is_bounded(self, ohlcv):
        mfi = ta.money_flow_index(
            ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"]
        ).dropna()
        assert mfi.between(0, 100).all()

    def test_atr_is_positive(self, ohlcv):
        atr = ta.atr(ohlcv["high"], ohlcv["low"], ohlcv["close"]).dropna()
        assert (atr > 0).all()


class TestCorrectness:
    def test_sma_matches_manual_mean(self, ohlcv):
        window, i = 20, 100
        expected = ohlcv["close"].iloc[i - window + 1 : i + 1].mean()
        assert ta.sma(ohlcv["close"], window).iloc[i] == pytest.approx(expected)

    def test_wma_weights_recent_values_more(self):
        series = pd.Series([1.0, 2.0, 3.0])
        # (1*1 + 2*2 + 3*3) / 6
        assert ta.wma(series, 3).iloc[-1] == pytest.approx(14 / 6)

    def test_bollinger_bands_are_symmetric(self, ohlcv):
        bb = ta.bollinger_bands(ohlcv["close"], 20, 2.0).dropna()
        assert np.allclose(bb["bb_upper"] - bb["bb_middle"], bb["bb_middle"] - bb["bb_lower"])

    def test_macd_histogram_is_the_difference(self, ohlcv):
        macd = ta.macd(ohlcv["close"]).dropna()
        assert np.allclose(macd["macd"] - macd["macd_signal"], macd["macd_hist"])

    def test_rsi_of_a_monotonic_rise_is_one_hundred(self):
        rising = pd.Series(np.arange(1, 60, dtype=float))
        assert ta.rsi(rising).dropna().iloc[-1] == pytest.approx(100.0)

    def test_rsi_of_a_monotonic_fall_is_zero(self):
        falling = pd.Series(np.arange(60, 1, -1, dtype=float))
        assert ta.rsi(falling).dropna().iloc[-1] == pytest.approx(0.0, abs=1e-6)

    def test_true_range_accounts_for_gaps(self):
        high = pd.Series([10.0, 20.0])
        low = pd.Series([9.0, 19.0])
        close = pd.Series([9.5, 19.5])
        # Second bar gapped up: the range from the prior close is 20 - 9.5.
        assert ta.true_range(high, low, close).iloc[1] == pytest.approx(10.5)

    def test_obv_follows_direction(self):
        close = pd.Series([10.0, 11.0, 10.5, 12.0])
        volume = pd.Series([100.0, 200.0, 150.0, 300.0])
        obv = ta.obv(close, volume)
        assert obv.iloc[-1] == pytest.approx(200 - 150 + 300)

    def test_volume_ratio_flags_a_spike(self):
        volume = pd.Series([100.0] * 25 + [500.0])
        # The rolling average includes the current bar, as charting platforms
        # conventionally define it: mean = (19*100 + 500)/20 = 120.
        assert ta.volume_ratio(volume, 20).iloc[-1] == pytest.approx(500 / 120)

    def test_volume_ratio_is_one_when_volume_is_flat(self):
        volume = pd.Series([100.0] * 30)
        assert ta.volume_ratio(volume, 20).dropna().iloc[-1] == pytest.approx(1.0)


class TestCausality:
    """No indicator may see the future.

    Recomputing on a truncated series must reproduce the earlier values
    exactly. This is the property the whole ML pipeline rests on.
    """

    @pytest.mark.parametrize(
        "name,fn",
        [
            ("sma", lambda d: ta.sma(d["close"], 20)),
            ("ema", lambda d: ta.ema(d["close"], 20)),
            ("wma", lambda d: ta.wma(d["close"], 10)),
            ("rsi", lambda d: ta.rsi(d["close"])),
            ("macd", lambda d: ta.macd(d["close"])["macd"]),
            ("atr", lambda d: ta.atr(d["high"], d["low"], d["close"])),
            ("adx", lambda d: ta.adx(d["high"], d["low"], d["close"])["adx"]),
            ("bollinger", lambda d: ta.bollinger_bands(d["close"])["bb_upper"]),
            ("stochastic", lambda d: ta.stochastic(d["high"], d["low"], d["close"])["stoch_k"]),
            ("obv", lambda d: ta.obv(d["close"], d["volume"])),
            ("mfi", lambda d: ta.money_flow_index(d["high"], d["low"], d["close"], d["volume"])),
            ("realized_vol", lambda d: ta.realized_volatility(d["close"])),
            ("support", lambda d: ta.rolling_support_resistance(d["high"], d["low"], d["close"])["support"]),
            ("donchian", lambda d: ta.donchian_channel(d["high"], d["low"])["donchian_upper"]),
            ("roc", lambda d: ta.roc(d["close"])),
            ("vwap_rolling", lambda d: ta.vwap(d["high"], d["low"], d["close"], d["volume"], window=20)),
        ],
    )
    def test_indicator_is_causal(self, ohlcv, name, fn):
        cut = 250
        full = fn(ohlcv).iloc[:cut]
        partial = fn(ohlcv.iloc[:cut])
        assert np.allclose(
            full.to_numpy(dtype=float), partial.to_numpy(dtype=float), equal_nan=True
        ), f"{name} changed when future bars were removed -- it is look-ahead biased"
