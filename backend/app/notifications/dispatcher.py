"""Alert creation and delivery.

Deduplication is the central concern: a signal that persists across pipeline
cycles must not re-alert every 30 minutes. Each alert carries a `dedupe_key`
derived from its meaning, and an identical key inside the dedupe window is
suppressed rather than re-sent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.analysis import Signal
from app.models.enums import (
    AlertSeverity, AlertStatus, AlertType, DataQuality, SignalStatus, SignalType,
)
from app.models.market import Quote, Security
from app.models.platform import Alert, User
from app.notifications.channels.base import DeliveryResult, NotificationChannel
from app.notifications.channels.email import EmailChannel
from app.notifications.channels.inapp import InAppChannel
from app.notifications.channels.telegram import TelegramChannel

log = get_logger(__name__)


def make_dedupe_key(
    alert_type: AlertType, security_id: int | None, *, discriminator: str = ""
) -> str:
    basis = f"{alert_type}:{security_id}:{discriminator}"
    return hashlib.sha256(basis.encode()).hexdigest()[:48]


class AlertDispatcher:
    def __init__(self, db: Session, channels: list[NotificationChannel] | None = None):
        self.db = db
        self.channels = channels if channels is not None else [
            InAppChannel(), EmailChannel(), TelegramChannel()
        ]

    # ------------------------------------------------------------- creation
    def create(
        self,
        *,
        alert_type: AlertType,
        title: str,
        body: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        security_id: int | None = None,
        signal_id: int | None = None,
        user_id: int | None = None,
        payload: dict | None = None,
        discriminator: str = "",
        commit: bool = True,
    ) -> Alert | None:
        """Create an alert, or return None when it duplicates a recent one."""
        key = make_dedupe_key(alert_type, security_id, discriminator=discriminator)
        window_start = datetime.now(timezone.utc) - timedelta(
            minutes=settings.alert_dedupe_window_minutes
        )
        existing = self.db.scalars(
            select(Alert)
            .where(Alert.dedupe_key == key, Alert.created_at >= window_start)
            .limit(1)
        ).first()
        if existing is not None:
            return None

        alert = Alert(
            user_id=user_id, security_id=security_id, signal_id=signal_id,
            alert_type=alert_type, severity=severity, status=AlertStatus.PENDING,
            title=title[:256], body=body, payload=payload, dedupe_key=key,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(alert)
        if commit:
            self.db.commit()
        return alert

    def create_signal_alerts(self, *, commit: bool = True) -> int:
        """Raise alerts for actionable signals that have not been alerted yet."""
        created = 0
        rows = self.db.execute(
            select(Signal, Security)
            .join(Security, Security.id == Signal.security_id)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.signal.in_(
                    [SignalType.STRONG_BUY, SignalType.BUY,
                     SignalType.SELL, SignalType.STRONG_SELL]
                ),
                Signal.confidence >= settings.signal_min_confidence,
            )
            .order_by(Signal.opportunity_score.desc().nullslast())
            .limit(settings.signal_max_per_run)
        ).all()

        for signal, security in rows:
            severity = (
                AlertSeverity.CRITICAL
                if signal.signal in (SignalType.STRONG_BUY, SignalType.STRONG_SELL)
                else AlertSeverity.WARNING
            )
            quality_note = (
                "  [SYNTHETIC TEST DATA -- NOT TRADEABLE]"
                if signal.data_quality is DataQuality.SYNTHETIC else ""
            )
            body = (
                f"{signal.signal} {security.symbol} ({security.name})\n"
                f"Confidence: {float(signal.confidence):.0%}\n"
                f"Reference price: {float(signal.reference_price):.2f}\n"
                f"Stop loss: {float(signal.stop_loss):.2f}\n"
                f"Take profit: {float(signal.take_profit):.2f}\n"
                f"Risk: {signal.risk_level} | Horizon: {signal.horizon_days}d\n"
                f"Data: {signal.data_quality} as of "
                f"{signal.price_as_of:%Y-%m-%d %H:%M} UTC{quality_note}\n\n"
                "This is a model-generated estimate, not investment advice, "
                "and it is not a guarantee of any outcome."
            ) if signal.stop_loss and signal.take_profit else (
                f"{signal.signal} {security.symbol} | confidence "
                f"{float(signal.confidence):.0%}{quality_note}"
            )

            alert = self.create(
                alert_type=AlertType.SIGNAL,
                title=f"{signal.signal}: {security.symbol} ({float(signal.confidence):.0%} confidence)",
                body=body,
                severity=severity,
                security_id=security.id,
                signal_id=signal.id,
                payload={
                    "signal": str(signal.signal),
                    "confidence": float(signal.confidence),
                    "reference_price": float(signal.reference_price),
                    "opportunity_score": (
                        float(signal.opportunity_score)
                        if signal.opportunity_score is not None else None
                    ),
                    "data_quality": str(signal.data_quality),
                },
                # A new signal type for the same security is a new alert; the
                # same signal repeating is not.
                discriminator=f"{signal.signal}:{signal.generated_at:%Y-%m-%d}",
                commit=False,
            )
            if alert is not None:
                created += 1

        if commit:
            self.db.commit()
        return created

    def create_price_move_alerts(
        self, *, threshold_pct: float = 5.0, commit: bool = True
    ) -> int:
        """Alert on large single-session moves and volume spikes."""
        created = 0
        rows = self.db.execute(
            select(Quote, Security).join(Security, Security.id == Quote.security_id)
        ).all()

        for quote, security in rows:
            if quote.change_pct is None:
                continue
            move = float(quote.change_pct)
            if abs(move) < threshold_pct:
                continue
            alert = self.create(
                alert_type=AlertType.PRICE_MOVE,
                title=f"{security.symbol} moved {move:+.1f}%",
                body=(
                    f"{security.name} is trading at {float(quote.price):.2f}, "
                    f"{move:+.2f}% against the previous close.\n"
                    f"Data: {quote.quality} as of {quote.source_timestamp:%Y-%m-%d %H:%M} UTC"
                ),
                severity=AlertSeverity.WARNING if abs(move) < 10 else AlertSeverity.CRITICAL,
                security_id=security.id,
                payload={"change_pct": move, "price": float(quote.price)},
                discriminator=f"{quote.source_timestamp:%Y-%m-%d}:{round(move)}",
                commit=False,
            )
            if alert is not None:
                created += 1

        if commit:
            self.db.commit()
        return created

    # ------------------------------------------------------------- delivery
    def dispatch_pending(self, *, limit: int = 100, commit: bool = True) -> dict:
        """Deliver pending alerts across every configured channel."""
        if not settings.notifications_enabled:
            return {"sent": 0, "failed": 0, "suppressed": 0, "note": "notifications disabled"}

        pending = self.db.scalars(
            select(Alert)
            .where(Alert.status == AlertStatus.PENDING)
            .order_by(Alert.created_at)
            .limit(limit)
        ).all()

        sent = failed = suppressed = 0
        for alert in pending:
            recipient = self._recipient(alert)
            results: list[DeliveryResult] = []

            for channel in self.channels:
                if not channel.is_configured():
                    results.append(
                        DeliveryResult(channel.name, False, "channel not configured")
                    )
                    continue
                try:
                    results.append(
                        channel.send(
                            title=alert.title, body=alert.body,
                            payload=alert.payload, recipient=recipient,
                        )
                    )
                except Exception as exc:
                    log.error("channel_send_error", channel=channel.name, error=str(exc))
                    results.append(DeliveryResult(channel.name, False, str(exc)[:200]))

            delivered_any = any(r.delivered for r in results)
            alert.delivery_results = {r.channel: r.as_dict() for r in results}
            alert.channels = [r.channel for r in results if r.delivered]
            alert.status = AlertStatus.SENT if delivered_any else AlertStatus.FAILED
            alert.sent_at = datetime.now(timezone.utc) if delivered_any else None

            if delivered_any:
                sent += 1
            else:
                failed += 1

        if commit:
            self.db.commit()

        # Count what deduplication prevented in this window, for observability.
        window_start = datetime.now(timezone.utc) - timedelta(
            minutes=settings.alert_dedupe_window_minutes
        )
        suppressed = (
            self.db.scalar(
                select(Alert.id).where(
                    Alert.status == AlertStatus.SUPPRESSED, Alert.created_at >= window_start
                )
            )
            is not None
        ) and 1 or 0

        return {"sent": sent, "failed": failed, "suppressed": suppressed}

    def _recipient(self, alert: Alert) -> str | None:
        if alert.user_id:
            user = self.db.get(User, alert.user_id)
            if user:
                return user.email
        admin = self.db.scalars(
            select(User).where(User.is_active.is_(True)).order_by(User.id).limit(1)
        ).first()
        return admin.email if admin else None

    def channel_health(self) -> dict:
        report = {}
        for channel in self.channels:
            ok, detail = channel.health_check()
            report[channel.name] = {"configured": channel.is_configured(), "detail": detail}
        return report
