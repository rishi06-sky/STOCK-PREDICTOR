"""Feature engineering.

Two principles shape this module:

1. **Stationarity.** Raw price levels do not generalise across securities --
   a model trained on a 3000-rupee stock learns nothing transferable about a
   150-dollar one. Every feature is therefore a ratio, a z-score, a percentage
   or a bounded oscillator.

2. **Non-redundancy.** Stacking twelve overlapping trend indicators inflates
   dimensionality without adding information and destabilises tree models. One
   representative per concept is selected, and `prune_correlated` removes what
   still ends up collinear.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.technical import indicators as ta

log = get_logger(__name__)

#: Bump when the feature definitions change. Stored on every row and on every
#: model, so training never mixes vectors from different pipeline generations.
FEATURE_SET_VERSION = "v1"

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(slots=True)
class FeatureConfig:
    trend: bool = True
    momentum: bool = True
    volatility: bool = True
    volume: bool = True
    levels: bool = True
    returns: bool = True
    ma_windows: tuple[int, ...] = (10, 20, 50, 200)
    return_windows: tuple[int, ...] = (1, 5, 10, 21)
    rsi_window: int = 14
    atr_window: int = 14
    adx_window: int = 14
    bb_window: int = 20
    volume_window: int = 20
    sr_window: int = 20

    @property
    def min_rows(self) -> int:
        """Longest warm-up any enabled feature needs before it yields a value."""
        needed = [2]
        if self.trend:
            needed.append(max(self.ma_windows) + 1)
            needed.append(self.adx_window * 3)
        if self.momentum:
            needed.append(self.rsi_window * 3)
            needed.append(35)  # MACD 26 + 9 signal
        if self.volatility:
            needed.append(max(self.atr_window * 2, self.bb_window + 1))
        if self.volume:
            needed.append(self.volume_window + 1)
        if self.levels:
            needed.append(self.sr_window + 1)
        if self.returns:
            needed.append(max(self.return_windows) + 1)
        return max(needed)


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0.0, np.nan)


def compute_features(
    df: pd.DataFrame, config: FeatureConfig | None = None
) -> pd.DataFrame:
    """Compute the feature matrix from an OHLCV frame indexed by trade date.

    The frame must be sorted ascending by date. Every column produced uses only
    information available at or before its own row.
    """
    config = config or FeatureConfig()
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    if not df.index.is_monotonic_increasing:
        raise ValueError("input frame must be sorted ascending by date")

    df = df.astype({c: "float64" for c in REQUIRED_COLUMNS})
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)

    # ------------------------------------------------------------- returns
    if config.returns:
        for window in config.return_windows:
            out[f"return_{window}d"] = close.pct_change(window, fill_method=None)
        out["log_return_1d"] = np.log(close / close.shift(1))
        # Gap between today's open and yesterday's close.
        out["overnight_gap"] = _safe_div(df["open"] - close.shift(1), close.shift(1))
        # Where the close sits within its own daily range.
        out["intraday_position"] = _safe_div(close - low, high - low)

    # --------------------------------------------------------------- trend
    if config.trend:
        for window in config.ma_windows:
            # Price relative to its moving average, not the average itself.
            out[f"price_to_sma_{window}"] = _safe_div(close, ta.sma(close, window)) - 1
        fast, slow = config.ma_windows[0], config.ma_windows[2]
        out["sma_fast_slow_ratio"] = _safe_div(
            ta.sma(close, fast), ta.sma(close, slow)
        ) - 1
        out["ema_20_slope"] = ta.ema(close, 20).pct_change(5, fill_method=None)

        adx_frame = ta.adx(high, low, close, config.adx_window)
        out["adx"] = adx_frame["adx"]
        # One directional-balance feature instead of two collinear DI columns.
        out["di_balance"] = _safe_div(
            adx_frame["plus_di"] - adx_frame["minus_di"],
            adx_frame["plus_di"] + adx_frame["minus_di"],
        )

    # ------------------------------------------------------------ momentum
    if config.momentum:
        out["rsi"] = ta.rsi(close, config.rsi_window)
        macd_frame = ta.macd(close)
        # Scale MACD by price so it is comparable across securities.
        out["macd_hist_norm"] = _safe_div(macd_frame["macd_hist"], close)
        out["macd_above_signal"] = (
            macd_frame["macd"] > macd_frame["macd_signal"]
        ).astype(float)
        stoch = ta.stochastic(high, low, close)
        out["stoch_k"] = stoch["stoch_k"]
        out["roc_10"] = ta.roc(close, 10)

    # ---------------------------------------------------------- volatility
    if config.volatility:
        atr_series = ta.atr(high, low, close, config.atr_window)
        # ATR as a fraction of price: comparable across instruments.
        out["atr_pct"] = _safe_div(atr_series, close)
        out["realized_vol_20"] = ta.realized_volatility(close, 20)
        out["vol_ratio_20_60"] = _safe_div(
            ta.realized_volatility(close, 20), ta.realized_volatility(close, 60)
        )
        bb = ta.bollinger_bands(close, config.bb_window)
        out["bb_width"] = bb["bb_width"]
        out["bb_position"] = bb["bb_position"]

    # -------------------------------------------------------------- volume
    if config.volume:
        out["volume_ratio"] = ta.volume_ratio(volume, config.volume_window)
        out["volume_trend"] = _safe_div(
            ta.sma(volume, 5), ta.sma(volume, config.volume_window)
        ) - 1
        # OBV slope rather than the unbounded cumulative level.
        obv_series = ta.obv(close, volume)
        obv_std = obv_series.rolling(config.volume_window, min_periods=config.volume_window).std(ddof=0)
        out["obv_slope"] = _safe_div(obv_series.diff(5), obv_std)
        out["mfi"] = ta.money_flow_index(high, low, close, volume)
        out["vwap_distance"] = _safe_div(
            close - ta.vwap(high, low, close, volume, window=config.volume_window), close
        )

    # -------------------------------------------------------------- levels
    if config.levels:
        sr = ta.rolling_support_resistance(high, low, close, config.sr_window)
        out["price_position_20"] = sr["price_position"]
        out["distance_to_resistance"] = sr["distance_to_resistance"]
        out["distance_to_support"] = sr["distance_to_support"]
        out["dist_52w_high"] = _safe_div(
            close, high.rolling(252, min_periods=60).max()
        ) - 1
        out["dist_52w_low"] = _safe_div(
            close, low.rolling(252, min_periods=60).min()
        ) - 1

    # Infinities arise from division by a zero that survived `replace`; they
    # are missing values, not extreme signals.
    return out.replace([np.inf, -np.inf], np.nan)


def prune_correlated(
    features: pd.DataFrame, threshold: float = 0.95, *, protect: set[str] | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """Drop one of each pair of near-duplicate columns.

    The later column in the frame is dropped, so earlier (more canonical)
    features are the ones that survive.
    """
    protect = protect or set()
    numeric = features.select_dtypes(include=[np.number])
    if numeric.shape[1] < 2:
        return features, []

    corr = numeric.corr().abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    dropped = [
        column
        for column in upper.columns
        if column not in protect and any(upper[column] > threshold)
    ]
    return features.drop(columns=dropped), dropped


def feature_quality_report(features: pd.DataFrame) -> dict:
    """Coverage and variance diagnostics used by the monitoring dashboard."""
    if features.empty:
        return {"rows": 0, "columns": 0, "usable_rows": 0}
    null_fraction = features.isna().mean()
    return {
        "rows": int(len(features)),
        "columns": int(features.shape[1]),
        "usable_rows": int(features.dropna().shape[0]),
        "high_null_columns": sorted(null_fraction[null_fraction > 0.3].index.tolist()),
        "zero_variance_columns": sorted(
            features.columns[features.std(numeric_only=True) == 0].tolist()
        ),
        "mean_null_fraction": float(null_fraction.mean()),
    }
