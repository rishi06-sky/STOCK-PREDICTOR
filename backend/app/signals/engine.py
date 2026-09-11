"""Signal engine.

Converts a calibrated model probability into an actionable recommendation with
levels, an explanation and an expiry.

Design commitments:
  * A probability is not a certainty. Confidence is derived from the calibrated
    probability, the model's validated quality and the freshness of the data --
    not from the raw model output alone.
  * Below the confidence floor the engine emits NO_ACTION. A forced signal is
    worse than no signal.
  * Stale data halts generation entirely.
  * Every signal carries its rationale and the model version that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import StaleDataError
from app.core.logging import get_logger
from app.ml.prediction import PredictionResult, estimate_expected_return
from app.ml.regime import posture_for
from app.models.analysis import Signal
from app.models.enums import (
    DataQuality, MarketRegime, OrderSide, RiskLevel, SignalStatus, SignalType,
)
from app.models.market import Security
from app.risk.engine import RiskEngine, classify_risk, take_profit_from_rr, volatility_stop
from app.technical import indicators as ta

log = get_logger(__name__)

#: Probability bands for each recommendation. Deliberately conservative: a
#: STRONG_BUY requires the model to be calibrated well above a coin flip.
STRONG_BUY_THRESHOLD = 0.68
BUY_THRESHOLD = 0.58
SELL_THRESHOLD = 0.42
STRONG_SELL_THRESHOLD = 0.32

#: A model whose validated AUC is at or below this has too thin an edge to act
#: on, whatever probability it emits. Confidence from such a model is capped
#: below the actionable floor so it can only ever produce NO_ACTION.
MIN_TRUSTWORTHY_AUC = 0.53


@dataclass(slots=True)
class SignalRationale:
    factor: str
    detail: str
    contribution: str      # "bullish" | "bearish" | "neutral"

    def as_dict(self) -> dict:
        return {"factor": self.factor, "detail": self.detail, "contribution": self.contribution}


@dataclass(slots=True)
class GeneratedSignal:
    security_id: int
    symbol: str
    signal: SignalType
    confidence: float
    reference_price: float
    horizon_days: int
    risk_level: RiskLevel
    data_quality: DataQuality
    price_as_of: datetime
    model_version_id: int | None = None
    prediction_id: int | None = None
    expected_return: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reward_to_risk: float | None = None
    opportunity_score: float | None = None
    regime: MarketRegime | None = None
    rationale: list[SignalRationale] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "signal": str(self.signal),
            "confidence": round(self.confidence, 4),
            "expected_return": (
                round(self.expected_return, 5) if self.expected_return is not None else None
            ),
            "risk_level": str(self.risk_level), "horizon_days": self.horizon_days,
            "reference_price": round(self.reference_price, 4),
            "entry_zone": (
                [round(self.entry_low, 4), round(self.entry_high, 4)]
                if self.entry_low and self.entry_high else None
            ),
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "take_profit": round(self.take_profit, 4) if self.take_profit else None,
            "reward_to_risk": round(self.reward_to_risk, 3) if self.reward_to_risk else None,
            "opportunity_score": (
                round(self.opportunity_score, 2) if self.opportunity_score is not None else None
            ),
            "regime": str(self.regime) if self.regime else None,
            "data_quality": str(self.data_quality),
            "price_as_of": self.price_as_of.isoformat(),
            "rationale": [r.as_dict() for r in self.rationale],
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def classify_signal(probability: float) -> SignalType:
    if probability >= STRONG_BUY_THRESHOLD:
        return SignalType.STRONG_BUY
    if probability >= BUY_THRESHOLD:
        return SignalType.BUY
    if probability <= STRONG_SELL_THRESHOLD:
        return SignalType.STRONG_SELL
    if probability <= SELL_THRESHOLD:
        return SignalType.SELL
    return SignalType.HOLD


def derive_confidence(
    probability: float, *, model_auc: float | None, data_quality: DataQuality,
    calibrated: bool, regime_multiplier: float = 1.0,
) -> tuple[float, list[str]]:
    """Quality-adjusted probability that the predicted direction is correct.

    Confidence is deliberately expressed on the same scale as the thing it
    describes: a calibrated model saying 0.70 means roughly a 70% chance of an
    up move, so confidence starts at 0.70 and is then *discounted* for model
    weakness, uncalibrated output and stale data. It is never inflated above
    the model's own probability, and 0.5 always means "a coin flip".
    """
    notes: list[str] = []
    # Probability of the direction actually being predicted (0.5 .. 1.0).
    directional = max(probability, 1.0 - probability)

    if model_auc is None:
        quality = 0.6
        notes.append("model has no validated AUC on record; confidence discounted")
    elif model_auc <= 0.5:
        quality = 0.0
        notes.append("model does not beat chance out-of-sample; confidence zeroed")
    else:
        # AUC 0.55 is a real but weak edge for daily equity direction; 0.70 is
        # strong. Map that range onto a 0.6-1.0 quality factor.
        quality = 0.6 + 0.4 * min(1.0, (model_auc - 0.50) / 0.20)
        if model_auc < 0.55:
            notes.append(f"weak validated edge (AUC {model_auc:.3f}); confidence discounted")

    if not calibrated:
        quality *= 0.85
        notes.append("probabilities are uncalibrated; confidence discounted 15%")

    if data_quality is DataQuality.EOD:
        quality *= 0.9
        notes.append("based on end-of-day data, not live prices")
    elif data_quality is DataQuality.DELAYED:
        quality *= 0.95
        notes.append("based on delayed prices")
    elif data_quality is DataQuality.SYNTHETIC:
        notes.append("SYNTHETIC test data -- not a tradeable signal")

    quality *= max(0.0, regime_multiplier)

    # Discount toward 0.5 (no information) rather than toward 0: a perfectly
    # useless model leaves us at a coin flip, not at certainty of the opposite.
    confidence = 0.5 + (directional - 0.5) * min(quality, 1.0)
    if quality <= 0:
        confidence = 0.0

    # Hard ceiling: an unvalidated or barely-better-than-chance model must not
    # be able to clear the actionable floor no matter how extreme its output.
    if model_auc is None or model_auc <= MIN_TRUSTWORTHY_AUC:
        ceiling = min(settings.signal_min_confidence - 0.01, 0.54)
        if confidence > ceiling:
            confidence = ceiling
            notes.append(
                "confidence capped below the actionable floor: the model's validated "
                f"edge (AUC {model_auc if model_auc is not None else 'unknown'}) is too "
                "thin to trade on"
            )

    return float(np.clip(confidence, 0.0, 1.0)), notes


def opportunity_score(
    confidence: float, expected_return: float | None, risk_level: RiskLevel,
    reward_to_risk: float | None,
) -> float:
    """Rank candidates on risk-adjusted attractiveness, 0-100."""
    risk_penalty = {
        RiskLevel.LOW: 1.0, RiskLevel.MODERATE: 0.9,
        RiskLevel.HIGH: 0.72, RiskLevel.VERY_HIGH: 0.5,
    }[risk_level]

    return_component = min(abs(expected_return or 0.0) / 0.10, 1.0)
    rr_component = min((reward_to_risk or 0.0) / 3.0, 1.0)

    raw = (0.5 * confidence + 0.3 * return_component + 0.2 * rr_component) * risk_penalty
    return float(np.clip(raw * 100, 0, 100))


class SignalEngine:
    def __init__(self, db: Session):
        self.db = db
        self.risk = RiskEngine(db)

    def generate(
        self,
        security: Security,
        prediction: PredictionResult,
        *,
        price_frame: pd.DataFrame | None = None,
        model_auc: float | None = None,
        regime: MarketRegime = MarketRegime.UNKNOWN,
        volatility_regime: MarketRegime = MarketRegime.UNKNOWN,
        sentiment: dict | None = None,
        fundamental_score: float | None = None,
    ) -> GeneratedSignal:
        """Produce a signal from a prediction, or NO_ACTION when unjustified."""
        self._assert_fresh(prediction)

        posture = posture_for(regime, volatility_regime)
        probability = prediction.probability
        direction_multiplier = (
            posture["long_confidence_multiplier"]
            if probability >= 0.5
            else posture["short_confidence_multiplier"]
        )

        confidence, notes = derive_confidence(
            probability,
            model_auc=model_auc,
            data_quality=prediction.data_quality,
            calibrated=prediction.calibrated,
            regime_multiplier=direction_multiplier,
        )

        atr_value, volatility = self._volatility(price_frame)
        risk_level = classify_risk(volatility, atr_pct=(atr_value / prediction.reference_price) if atr_value else None)

        signal_type = classify_signal(probability)
        rationale = self._build_rationale(
            probability, prediction, regime, volatility_regime, risk_level,
            sentiment, fundamental_score, notes,
        )

        # ------------------------------------------- the confidence floor
        if confidence < settings.signal_min_confidence and signal_type.is_actionable:
            rationale.append(
                SignalRationale(
                    "confidence floor",
                    f"confidence {confidence:.2f} is below the {settings.signal_min_confidence} "
                    "threshold; no action is preferred to a forced call",
                    "neutral",
                )
            )
            signal_type = SignalType.NO_ACTION

        expected_return = estimate_expected_return(
            probability, prediction.horizon_days, volatility
        )

        generated = GeneratedSignal(
            security_id=security.id, symbol=security.symbol, signal=signal_type,
            confidence=confidence, reference_price=prediction.reference_price,
            horizon_days=prediction.horizon_days, risk_level=risk_level,
            data_quality=prediction.data_quality, price_as_of=prediction.price_as_of,
            model_version_id=prediction.model_version_id,
            expected_return=expected_return, regime=regime, rationale=rationale,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=settings.signal_ttl_minutes),
        )

        if signal_type.is_actionable:
            self._attach_levels(generated, atr_value, posture["stop_atr_multiplier"])
            generated.opportunity_score = opportunity_score(
                confidence, expected_return, risk_level, generated.reward_to_risk
            )
        else:
            generated.opportunity_score = 0.0

        return generated

    # ------------------------------------------------------------- internals
    @staticmethod
    def _assert_fresh(prediction: PredictionResult) -> None:
        if prediction.data_quality is DataQuality.SYNTHETIC:
            return
        age = (datetime.now(timezone.utc) - prediction.price_as_of).total_seconds()
        if age > settings.signal_halt_staleness_seconds:
            raise StaleDataError(
                prediction.symbol, age, settings.signal_halt_staleness_seconds
            )

    @staticmethod
    def _volatility(frame: pd.DataFrame | None) -> tuple[float | None, float | None]:
        if frame is None or len(frame) < 30:
            return None, None
        atr_series = ta.atr(frame["high"], frame["low"], frame["close"], 14)
        vol_series = ta.realized_volatility(frame["close"], 20)
        atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else None
        volatility = float(vol_series.iloc[-1]) if not pd.isna(vol_series.iloc[-1]) else None
        return atr_value, volatility

    def _attach_levels(
        self, signal: GeneratedSignal, atr_value: float | None, atr_multiplier: float
    ) -> None:
        price = signal.reference_price
        side = OrderSide.BUY if signal.signal.direction > 0 else OrderSide.SELL

        stop = volatility_stop(price, atr_value, side=side, atr_multiplier=atr_multiplier)
        target = take_profit_from_rr(price, stop, side=side, reward_to_risk=2.0)

        # Entry zone: a band around the reference price scaled by volatility,
        # acknowledging that the exact fill is unknowable.
        band = (atr_value * 0.4) if atr_value else price * 0.005
        if side is OrderSide.BUY:
            signal.entry_low, signal.entry_high = price - band, price + band * 0.5
        else:
            signal.entry_low, signal.entry_high = price - band * 0.5, price + band

        signal.stop_loss, signal.take_profit = stop, target
        risk = abs(price - stop)
        signal.reward_to_risk = (abs(target - price) / risk) if risk > 0 else None

    @staticmethod
    def _build_rationale(
        probability, prediction, regime, volatility_regime, risk_level,
        sentiment, fundamental_score, notes,
    ) -> list[SignalRationale]:
        rationale = [
            SignalRationale(
                "model probability",
                f"calibrated probability of a positive {prediction.horizon_days}-day return "
                f"is {probability:.1%}",
                "bullish" if probability > 0.5 else "bearish",
            ),
            SignalRationale(
                "market regime",
                f"{regime} market, {volatility_regime} volatility",
                "bullish" if regime is MarketRegime.BULL
                else "bearish" if regime is MarketRegime.BEAR else "neutral",
            ),
            SignalRationale("risk", f"volatility band: {risk_level}", "neutral"),
        ]

        features = prediction.feature_snapshot or {}
        for name, label in (
            ("rsi", "RSI"), ("price_to_sma_50", "price vs 50-day average"),
            ("adx", "trend strength (ADX)"), ("volume_ratio", "volume vs 20-day average"),
        ):
            value = features.get(name)
            if value is None:
                continue
            if name == "rsi":
                contribution = "bearish" if value > 70 else "bullish" if value < 30 else "neutral"
                detail = f"RSI {value:.1f}" + (
                    " (overbought)" if value > 70 else " (oversold)" if value < 30 else ""
                )
            elif name == "price_to_sma_50":
                contribution = "bullish" if value > 0 else "bearish"
                detail = f"trading {value:+.1%} against its 50-day average"
            elif name == "adx":
                contribution = "neutral"
                detail = f"ADX {value:.1f}" + (" (trending)" if value > 25 else " (choppy)")
            else:
                contribution = "bullish" if value > 1.5 else "neutral"
                detail = f"volume at {value:.2f}x its 20-day average"
            rationale.append(SignalRationale(label, detail, contribution))

        if sentiment and sentiment.get("count"):
            score = sentiment.get("score", 0.0)
            rationale.append(
                SignalRationale(
                    "news sentiment",
                    f"{sentiment['count']} article(s) over {sentiment.get('window_days', 7)} days, "
                    f"net score {score:+.2f} (confidence {sentiment.get('confidence', 0):.2f})",
                    "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral",
                )
            )

        if fundamental_score is not None:
            rationale.append(
                SignalRationale(
                    "fundamentals",
                    f"peer-relative quality score {fundamental_score:.0f}/100",
                    "bullish" if fundamental_score > 60
                    else "bearish" if fundamental_score < 40 else "neutral",
                )
            )

        rationale.extend(
            SignalRationale("caveat", note, "neutral") for note in notes
        )
        return rationale

    # -------------------------------------------------------------- persistence
    def persist(self, generated: GeneratedSignal, *, commit: bool = True) -> Signal:
        """Store a signal, superseding any active one for the same security."""
        active = self.db.scalars(
            select(Signal).where(
                Signal.security_id == generated.security_id,
                Signal.status == SignalStatus.ACTIVE,
                Signal.horizon_days == generated.horizon_days,
            )
        ).all()
        for previous in active:
            previous.status = SignalStatus.SUPERSEDED
            previous.invalidated_at = datetime.now(timezone.utc)
            previous.invalidation_reason = "replaced by a newer signal"

        record = Signal(
            security_id=generated.security_id,
            prediction_id=generated.prediction_id,
            model_version_id=generated.model_version_id,
            signal=generated.signal, status=SignalStatus.ACTIVE,
            confidence=generated.confidence, expected_return=generated.expected_return,
            risk_level=generated.risk_level, horizon_days=generated.horizon_days,
            reference_price=generated.reference_price,
            entry_low=generated.entry_low, entry_high=generated.entry_high,
            stop_loss=generated.stop_loss, take_profit=generated.take_profit,
            reward_to_risk=generated.reward_to_risk,
            opportunity_score=generated.opportunity_score, regime=generated.regime,
            rationale=[r.as_dict() for r in generated.rationale],
            data_quality=generated.data_quality, price_as_of=generated.price_as_of,
            generated_at=generated.generated_at, expires_at=generated.expires_at,
        )
        self.db.add(record)
        if commit:
            self.db.commit()
        return record

    def expire_stale_signals(self, *, commit: bool = True) -> int:
        """Expire signals past their TTL. Called by the scheduler."""
        now = datetime.now(timezone.utc)
        stale = self.db.scalars(
            select(Signal).where(Signal.status == SignalStatus.ACTIVE, Signal.expires_at <= now)
        ).all()
        for signal in stale:
            signal.status = SignalStatus.EXPIRED
            signal.invalidated_at = now
            signal.invalidation_reason = "time to live elapsed"
        if commit:
            self.db.commit()
        return len(stale)

    def invalidate_on_price_move(
        self, *, move_threshold: float = 0.05, commit: bool = True
    ) -> int:
        """Invalidate signals whose premise has been overtaken by the market."""
        from app.models.market import Quote

        now = datetime.now(timezone.utc)
        invalidated = 0
        active = self.db.scalars(
            select(Signal).where(Signal.status == SignalStatus.ACTIVE)
        ).all()

        for signal in active:
            quote = self.db.get(Quote, signal.security_id)
            if quote is None or not signal.reference_price:
                continue
            move = abs(float(quote.price) / float(signal.reference_price) - 1)
            hit_stop = signal.stop_loss and float(quote.price) <= float(signal.stop_loss)
            hit_target = signal.take_profit and float(quote.price) >= float(signal.take_profit)

            if move >= move_threshold or hit_stop or hit_target:
                signal.status = SignalStatus.INVALIDATED
                signal.invalidated_at = now
                signal.invalidation_reason = (
                    "stop level reached" if hit_stop
                    else "target reached" if hit_target
                    else f"price moved {move:.1%} from the signal reference"
                )
                invalidated += 1

        if commit:
            self.db.commit()
        return invalidated
