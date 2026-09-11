"""Domain enumerations shared across the platform."""
from __future__ import annotations

import enum


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DataQuality(StrEnum):
    """How current a market-data record is. Never conflate these."""

    LIVE = "LIVE"              # real-time from an exchange-licensed feed
    DELAYED = "DELAYED"        # typically 15-20 minutes behind
    EOD = "EOD"                # end-of-day settlement data
    HISTORICAL = "HISTORICAL"  # backfilled history
    SYNTHETIC = "SYNTHETIC"    # test fixtures only, never tradeable


class AssetType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"


class SignalType(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    NO_ACTION = "NO_ACTION"    # emitted when confidence is insufficient

    @property
    def is_actionable(self) -> bool:
        return self in {
            SignalType.STRONG_BUY,
            SignalType.BUY,
            SignalType.SELL,
            SignalType.STRONG_SELL,
        }

    @property
    def direction(self) -> int:
        return {
            SignalType.STRONG_BUY: 1,
            SignalType.BUY: 1,
            SignalType.HOLD: 0,
            SignalType.NO_ACTION: 0,
            SignalType.SELL: -1,
            SignalType.STRONG_SELL: -1,
        }[self]


class SignalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"  # market conditions changed materially


class RiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


class MarketRegime(StrEnum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    TIME_EXIT = "TIME_EXIT"
    MANUAL = "MANUAL"
    RISK_EXIT = "RISK_EXIT"


class AlertType(StrEnum):
    SIGNAL = "SIGNAL"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    TAKE_PROFIT_HIT = "TAKE_PROFIT_HIT"
    PRICE_MOVE = "PRICE_MOVE"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    NEWS = "NEWS"
    PREDICTION_CHANGE = "PREDICTION_CHANGE"
    SYSTEM = "SYSTEM"
    RISK_BREACH = "RISK_BREACH"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"  # deduplicated


class ModelStatus(StrEnum):
    TRAINING = "TRAINING"
    VALIDATING = "VALIDATING"
    CANDIDATE = "CANDIDATE"   # passed training, not yet promoted
    PRODUCTION = "PRODUCTION"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class PredictionTarget(StrEnum):
    DIRECTION = "DIRECTION"          # classification: up / not up
    EXPECTED_RETURN = "EXPECTED_RETURN"
    VOLATILITY = "VOLATILITY"


class SentimentLabel(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class EventSeverity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    USER = "USER"
    READONLY = "READONLY"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
