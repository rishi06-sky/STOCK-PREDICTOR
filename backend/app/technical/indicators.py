"""Technical indicators.

Implemented directly on pandas rather than pulled from a TA library so the
maths is auditable and dependency-light.

Every function here is *causal*: the value at index i uses only data at
indices <= i. That property is what keeps the ML pipeline free of look-ahead
bias, and it is asserted in the unit tests.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ averages
def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    # adjust=False gives the recursive form, matching platform conventions.
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def wma(series: pd.Series, window: int) -> pd.Series:
    weights = np.arange(1, window + 1, dtype=float)
    return series.rolling(window=window, min_periods=window).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True
    )


# ------------------------------------------------------------------ momentum
def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI using his smoothing (alpha = 1/window)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # No losses in the window is RSI 100 by definition, not a division error.
    out = out.where(avg_loss.notna() & (avg_loss != 0), other=np.where(avg_gain > 0, 100.0, 50.0))
    return out.where(avg_gain.notna())


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": macd_line - signal_line,
        }
    )


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k_window: int = 14, d_window: int = 3
) -> pd.DataFrame:
    lowest = low.rolling(k_window, min_periods=k_window).min()
    highest = high.rolling(k_window, min_periods=k_window).max()
    span = (highest - lowest).replace(0.0, np.nan)
    k = 100 * (close - lowest) / span
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(d_window, min_periods=d_window).mean()})


def roc(series: pd.Series, window: int = 10) -> pd.Series:
    """Rate of change, in percent."""
    return series.pct_change(periods=window, fill_method=None) * 100


# ---------------------------------------------------------------- volatility
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def bollinger_bands(
    series: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    middle = sma(series, window)
    std = series.rolling(window, min_periods=window).std(ddof=0)
    upper, lower = middle + num_std * std, middle - num_std * std
    width = (upper - lower) / middle.replace(0.0, np.nan)
    # Where price sits inside the channel: 0 at the lower band, 1 at the upper.
    position = (series - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {"bb_upper": upper, "bb_middle": middle, "bb_lower": lower,
         "bb_width": width, "bb_position": position}
    )


def realized_volatility(series: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Annualised standard deviation of log returns."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(periods_per_year)


# --------------------------------------------------------------------- trend
def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )

    alpha = 1 / window
    atr_ = true_range(high, low, close).ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    safe_atr = atr_.replace(0.0, np.nan)

    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / safe_atr

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame(
        {
            "adx": dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean(),
            "plus_di": plus_di,
            "minus_di": minus_di,
        }
    )


# -------------------------------------------------------------------- volume
def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
    window: int | None = None,
) -> pd.Series:
    """Volume-weighted average price.

    With `window` this is a rolling VWAP. Without one it accumulates from the
    start of the series, which is only meaningful within a single session.
    """
    typical = (high + low + close) / 3
    pv = typical * volume
    if window:
        return (
            pv.rolling(window, min_periods=window).sum()
            / volume.rolling(window, min_periods=window).sum().replace(0.0, np.nan)
        )
    return pv.cumsum() / volume.cumsum().replace(0.0, np.nan)


def volume_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    """Current volume against its recent average. >1 means unusually active."""
    avg = volume.rolling(window, min_periods=window).mean().replace(0.0, np.nan)
    return volume / avg


def money_flow_index(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, window: int = 14
) -> pd.Series:
    typical = (high + low + close) / 3
    raw_flow = typical * volume
    direction = typical.diff()
    positive = raw_flow.where(direction > 0, 0.0).rolling(window, min_periods=window).sum()
    negative = raw_flow.where(direction < 0, 0.0).rolling(window, min_periods=window).sum()
    ratio = positive / negative.replace(0.0, np.nan)
    return 100 - (100 / (1 + ratio))


# ---------------------------------------------------------- support/resistance
def rolling_support_resistance(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20
) -> pd.DataFrame:
    """Recent extremes, and where price sits between them."""
    resistance = high.rolling(window, min_periods=window).max()
    support = low.rolling(window, min_periods=window).min()
    span = (resistance - support).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "resistance": resistance,
            "support": support,
            "price_position": (close - support) / span,
            "distance_to_resistance": (resistance - close) / close.replace(0.0, np.nan),
            "distance_to_support": (close - support) / close.replace(0.0, np.nan),
        }
    )


def donchian_channel(high: pd.Series, low: pd.Series, window: int = 20) -> pd.DataFrame:
    upper = high.rolling(window, min_periods=window).max()
    lower = low.rolling(window, min_periods=window).min()
    return pd.DataFrame(
        {"donchian_upper": upper, "donchian_lower": lower, "donchian_mid": (upper + lower) / 2}
    )
