"""Market regime detection.

Classifies an index into a directional regime (bull / bear / sideways) and a
separate volatility regime, using trend slope, drawdown and realised
volatility percentiles. Deliberately rule-based and inspectable rather than a
latent-state model: a regime nobody can explain is not actionable, and with a
few years of daily data an HMM mostly fits noise.

Returns UNKNOWN when there is not enough history, rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.models.enums import MarketRegime

log = get_logger(__name__)

MIN_HISTORY = 120        # trading days needed before any call is made
VOL_LOOKBACK = 252       # window for the volatility percentile


@dataclass(slots=True)
class RegimeResult:
    regime: MarketRegime
    volatility_regime: MarketRegime
    trend_strength: float | None = None
    realized_volatility: float | None = None
    drawdown: float | None = None
    vol_percentile: float | None = None
    breadth: float | None = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "regime": str(self.regime),
            "volatility_regime": str(self.volatility_regime),
            "trend_strength": _round(self.trend_strength, 6),
            "realized_volatility": _round(self.realized_volatility, 6),
            "drawdown": _round(self.drawdown, 6),
            "vol_percentile": _round(self.vol_percentile, 4),
            "breadth": _round(self.breadth, 4),
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
        }


def _round(value, digits):
    return None if value is None else round(float(value), digits)


def detect_regime(
    close: pd.Series, *, breadth: float | None = None, as_of: date | None = None
) -> RegimeResult:
    """Classify the regime from an index close series."""
    close = pd.Series(close).dropna()
    if len(close) < MIN_HISTORY:
        return RegimeResult(
            regime=MarketRegime.UNKNOWN, volatility_regime=MarketRegime.UNKNOWN,
            reasons=[f"only {len(close)} bars; {MIN_HISTORY} required"],
        )

    reasons: list[str] = []

    sma_50 = close.rolling(50, min_periods=50).mean()
    sma_200 = close.rolling(200, min_periods=100).mean()
    last = float(close.iloc[-1])

    # Trend: slope of the 50-day average over the last quarter, in percent.
    slope_window = min(63, len(sma_50.dropna()))
    sma_recent = sma_50.dropna().iloc[-slope_window:]
    trend_strength = (
        float((sma_recent.iloc[-1] / sma_recent.iloc[0] - 1))
        if len(sma_recent) > 1 and sma_recent.iloc[0] != 0 else 0.0
    )

    running_max = close.cummax()
    drawdown = float(last / running_max.iloc[-1] - 1)

    log_returns = np.log(close / close.shift(1)).dropna()
    realized_vol = float(log_returns.iloc[-20:].std(ddof=1) * np.sqrt(252)) if len(log_returns) >= 20 else None

    vol_percentile = None
    if realized_vol is not None and len(log_returns) >= 60:
        rolling_vol = log_returns.rolling(20).std(ddof=1) * np.sqrt(252)
        history = rolling_vol.dropna().iloc[-VOL_LOOKBACK:]
        if len(history) >= 30:
            vol_percentile = float((history < realized_vol).mean())

    # ------------------------------------------------------------- direction
    above_200 = bool(sma_200.notna().iloc[-1] and last > sma_200.iloc[-1])
    golden_cross = bool(
        sma_50.notna().iloc[-1] and sma_200.notna().iloc[-1] and sma_50.iloc[-1] > sma_200.iloc[-1]
    )

    if drawdown <= -0.20:
        regime = MarketRegime.BEAR
        reasons.append(f"drawdown {drawdown:.1%} from peak exceeds -20%")
    elif trend_strength > 0.03 and above_200 and golden_cross:
        regime = MarketRegime.BULL
        reasons.append(f"50d average rising {trend_strength:+.1%}, price above rising 200d")
    elif trend_strength < -0.03 and not above_200:
        regime = MarketRegime.BEAR
        reasons.append(f"50d average falling {trend_strength:+.1%}, price below 200d")
    else:
        regime = MarketRegime.SIDEWAYS
        reasons.append(f"trend {trend_strength:+.1%} inside the +/-3% band")

    # ------------------------------------------------------------ volatility
    if vol_percentile is None:
        volatility_regime = MarketRegime.UNKNOWN
        reasons.append("insufficient history for a volatility percentile")
    elif vol_percentile >= 0.75:
        volatility_regime = MarketRegime.HIGH_VOLATILITY
        reasons.append(f"realised vol in the {vol_percentile:.0%} percentile of the past year")
    elif vol_percentile <= 0.25:
        volatility_regime = MarketRegime.LOW_VOLATILITY
        reasons.append(f"realised vol in the {vol_percentile:.0%} percentile of the past year")
    else:
        volatility_regime = MarketRegime.SIDEWAYS
        reasons.append(f"realised vol mid-range ({vol_percentile:.0%} percentile)")

    # Confidence rises when the signals agree and history is deep.
    confidence = 0.4
    if regime is MarketRegime.BULL and above_200 and golden_cross:
        confidence += 0.3
    if regime is MarketRegime.BEAR and drawdown <= -0.20:
        confidence += 0.3
    if abs(trend_strength) > 0.06:
        confidence += 0.15
    if len(close) >= 400:
        confidence += 0.1
    confidence = min(1.0, confidence)

    if breadth is not None:
        reasons.append(f"breadth: {breadth:.0%} of constituents above their 50d average")

    return RegimeResult(
        regime=regime, volatility_regime=volatility_regime,
        trend_strength=trend_strength, realized_volatility=realized_vol,
        drawdown=drawdown, vol_percentile=vol_percentile, breadth=breadth,
        confidence=confidence, reasons=reasons,
    )


def compute_breadth(closes: dict[int, pd.Series], window: int = 50) -> float | None:
    """Fraction of securities trading above their own N-day average."""
    above, total = 0, 0
    for series in closes.values():
        series = pd.Series(series).dropna()
        if len(series) < window:
            continue
        average = series.rolling(window, min_periods=window).mean().iloc[-1]
        if pd.isna(average):
            continue
        total += 1
        above += int(series.iloc[-1] > average)
    return (above / total) if total else None


#: How the signal engine should lean in each regime. Modest, evidence-shaped
#: adjustments -- not a claim that any regime guarantees an outcome.
REGIME_POSTURE = {
    MarketRegime.BULL: {
        "long_confidence_multiplier": 1.05, "short_confidence_multiplier": 0.85,
        "position_size_multiplier": 1.0,
    },
    MarketRegime.BEAR: {
        "long_confidence_multiplier": 0.80, "short_confidence_multiplier": 1.05,
        "position_size_multiplier": 0.6,
    },
    MarketRegime.SIDEWAYS: {
        "long_confidence_multiplier": 0.95, "short_confidence_multiplier": 0.95,
        "position_size_multiplier": 0.8,
    },
    MarketRegime.UNKNOWN: {
        "long_confidence_multiplier": 0.9, "short_confidence_multiplier": 0.9,
        "position_size_multiplier": 0.5,
    },
}

VOLATILITY_POSTURE = {
    MarketRegime.HIGH_VOLATILITY: {"position_size_multiplier": 0.6, "stop_atr_multiplier": 2.5},
    MarketRegime.LOW_VOLATILITY: {"position_size_multiplier": 1.0, "stop_atr_multiplier": 1.8},
    MarketRegime.SIDEWAYS: {"position_size_multiplier": 0.85, "stop_atr_multiplier": 2.0},
    MarketRegime.UNKNOWN: {"position_size_multiplier": 0.6, "stop_atr_multiplier": 2.5},
}


def posture_for(regime: MarketRegime, volatility_regime: MarketRegime) -> dict:
    base = dict(REGIME_POSTURE.get(regime, REGIME_POSTURE[MarketRegime.UNKNOWN]))
    vol = VOLATILITY_POSTURE.get(volatility_regime, VOLATILITY_POSTURE[MarketRegime.UNKNOWN])
    base["position_size_multiplier"] *= vol["position_size_multiplier"]
    base["stop_atr_multiplier"] = vol["stop_atr_multiplier"]
    return base
