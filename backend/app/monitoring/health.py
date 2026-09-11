"""System health and observability.

Health is reported per component with a reason. "Healthy" is never asserted
without a check actually passing -- an unknown component reports UNHEALTHY,
not HEALTHY.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.analysis import Signal
from app.models.enums import AlertStatus, HealthStatus, ModelStatus, SignalStatus
from app.models.market import PriceData, Quote, Security
from app.models.platform import Alert, DataFreshness, ModelVersion, SystemEvent

log = get_logger(__name__)


@dataclass(slots=True)
class ComponentHealth:
    name: str
    status: HealthStatus
    detail: str
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "component": self.name, "status": str(self.status),
            "detail": self.detail, "metrics": self.metrics,
        }


def check_database(db: Session) -> ComponentHealth:
    try:
        count = db.scalar(select(func.count()).select_from(Security))
        return ComponentHealth(
            "database", HealthStatus.HEALTHY, "reachable",
            {"securities": int(count or 0)},
        )
    except Exception as exc:
        return ComponentHealth("database", HealthStatus.UNHEALTHY, str(exc)[:200])


def check_redis() -> ComponentHealth:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=3)
        client.ping()
        client.close()
        return ComponentHealth("redis", HealthStatus.HEALTHY, "ping ok")
    except Exception as exc:
        return ComponentHealth(
            "redis", HealthStatus.DEGRADED,
            f"unavailable: {str(exc)[:150]} (caching and live updates disabled)",
        )


def check_providers() -> ComponentHealth:
    from app.market_data.registry import get_chain

    try:
        report = get_chain().health()
    except Exception as exc:
        return ComponentHealth("market_data_providers", HealthStatus.UNHEALTHY, str(exc)[:200])

    healthy = [name for name, info in report.items() if info["healthy"]]
    if not report:
        return ComponentHealth(
            "market_data_providers", HealthStatus.UNHEALTHY, "no providers configured"
        )
    if not healthy:
        return ComponentHealth(
            "market_data_providers", HealthStatus.UNHEALTHY,
            "every configured provider failed its probe; ingestion cannot run",
            {"providers": report},
        )
    status = HealthStatus.HEALTHY if len(healthy) == len(report) else HealthStatus.DEGRADED
    return ComponentHealth(
        "market_data_providers", status,
        f"{len(healthy)}/{len(report)} providers reachable",
        {"providers": report},
    )


def check_data_freshness(db: Session) -> ComponentHealth:
    """Are we holding data recent enough to act on?"""
    now = datetime.now(timezone.utc)
    newest_quote = db.scalar(select(func.max(Quote.source_timestamp)))
    newest_bar = db.scalar(select(func.max(PriceData.trade_date)))

    metrics = {
        "newest_quote": newest_quote.isoformat() if newest_quote else None,
        "newest_daily_bar": str(newest_bar) if newest_bar else None,
    }

    rows = db.scalars(select(DataFreshness)).all()
    metrics["datasets"] = [
        {
            "provider": r.provider, "dataset": r.dataset,
            "last_success": r.last_success_at.isoformat() if r.last_success_at else None,
            "consecutive_failures": r.consecutive_failures,
            "last_error": (r.last_error or "")[:120] or None,
        }
        for r in rows
    ]

    if newest_quote is None and newest_bar is None:
        return ComponentHealth(
            "data_freshness", HealthStatus.UNHEALTHY, "no market data stored at all", metrics
        )

    failing = [r for r in rows if r.consecutive_failures >= 3]
    if newest_quote is not None:
        age_minutes = (now - newest_quote).total_seconds() / 60
        metrics["quote_age_minutes"] = round(age_minutes, 1)
        if age_minutes > settings.signal_halt_staleness_seconds / 60:
            return ComponentHealth(
                "data_freshness", HealthStatus.UNHEALTHY,
                f"newest quote is {age_minutes:.0f} minutes old; signal generation halts",
                metrics,
            )
        if age_minutes > settings.quote_staleness_seconds / 60:
            return ComponentHealth(
                "data_freshness", HealthStatus.DEGRADED,
                f"quotes are {age_minutes:.0f} minutes old; treated as delayed", metrics,
            )

    if failing:
        return ComponentHealth(
            "data_freshness", HealthStatus.DEGRADED,
            f"{len(failing)} dataset(s) failing repeatedly", metrics,
        )
    return ComponentHealth("data_freshness", HealthStatus.HEALTHY, "data is current", metrics)


def check_models(db: Session) -> ComponentHealth:
    production = db.scalars(
        select(ModelVersion).where(ModelVersion.status == ModelStatus.PRODUCTION)
    ).all()
    if not production:
        return ComponentHealth(
            "models", HealthStatus.DEGRADED,
            "no model has passed the promotion gate; no signals will be generated",
            {"production_models": 0},
        )

    now = datetime.now(timezone.utc)
    entries, stale = [], 0
    for model in production:
        age_days = (now - model.trained_at).days if model.trained_at else None
        if age_days is not None and age_days > 45:
            stale += 1
        entries.append(
            {
                "name": model.name, "version": model.version,
                "horizon_days": model.horizon_days,
                "trained_at": model.trained_at.isoformat() if model.trained_at else None,
                "age_days": age_days,
                "feature_set_version": model.feature_set_version,
            }
        )

    status = HealthStatus.DEGRADED if stale else HealthStatus.HEALTHY
    detail = (
        f"{stale} production model(s) older than 45 days; retraining is due"
        if stale else f"{len(production)} production model(s) current"
    )
    return ComponentHealth("models", status, detail, {"models": entries})


def check_signals(db: Session) -> ComponentHealth:
    now = datetime.now(timezone.utc)
    active = db.scalar(
        select(func.count()).select_from(Signal).where(
            Signal.status == SignalStatus.ACTIVE, Signal.expires_at > now
        )
    )
    newest = db.scalar(select(func.max(Signal.generated_at)))
    metrics = {
        "active_signals": int(active or 0),
        "newest_signal": newest.isoformat() if newest else None,
    }
    if newest is None:
        return ComponentHealth(
            "signals", HealthStatus.DEGRADED, "no signals have been generated yet", metrics
        )
    age_hours = (now - newest).total_seconds() / 3600
    metrics["age_hours"] = round(age_hours, 2)
    if age_hours > 24:
        return ComponentHealth(
            "signals", HealthStatus.DEGRADED,
            f"newest signal is {age_hours:.1f} hours old", metrics,
        )
    return ComponentHealth("signals", HealthStatus.HEALTHY, f"{active} active signals", metrics)


def check_notifications(db: Session) -> ComponentHealth:
    from app.notifications.dispatcher import AlertDispatcher

    window = datetime.now(timezone.utc) - timedelta(hours=24)
    failed = db.scalar(
        select(func.count()).select_from(Alert).where(
            Alert.status == AlertStatus.FAILED, Alert.created_at >= window
        )
    ) or 0
    sent = db.scalar(
        select(func.count()).select_from(Alert).where(
            Alert.status == AlertStatus.SENT, Alert.created_at >= window
        )
    ) or 0

    channels = AlertDispatcher(db).channel_health()
    metrics = {"sent_24h": int(sent), "failed_24h": int(failed), "channels": channels}

    if not settings.notifications_enabled:
        return ComponentHealth(
            "notifications", HealthStatus.DEGRADED, "notifications are disabled", metrics
        )
    if failed and failed > sent:
        return ComponentHealth(
            "notifications", HealthStatus.DEGRADED,
            f"{failed} alert(s) failed to deliver in the last 24h", metrics,
        )
    return ComponentHealth("notifications", HealthStatus.HEALTHY, "delivering", metrics)


def check_trading() -> ComponentHealth:
    from app.trading.brokers.live_guard import live_trading_status

    status_info = live_trading_status(broker_connected=False)
    if settings.kill_switch_engaged:
        return ComponentHealth(
            "trading", HealthStatus.DEGRADED,
            "kill switch is ENGAGED; no orders will be placed", status_info.as_dict(),
        )
    return ComponentHealth(
        "trading", HealthStatus.HEALTHY,
        f"{settings.trading_mode} mode", status_info.as_dict(),
    )


def system_health(db: Session, *, include_providers: bool = True) -> dict:
    components = [
        check_database(db), check_redis(), check_data_freshness(db),
        check_models(db), check_signals(db), check_notifications(db), check_trading(),
    ]
    if include_providers:
        components.append(check_providers())

    statuses = [c.status for c in components]
    if HealthStatus.UNHEALTHY in statuses:
        overall = HealthStatus.UNHEALTHY
    elif HealthStatus.DEGRADED in statuses:
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.HEALTHY

    return {
        "status": str(overall),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": settings.environment,
        "components": [c.as_dict() for c in components],
    }


def recent_events(db: Session, *, limit: int = 50, min_severity: str | None = None) -> list[dict]:
    stmt = select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)
    if min_severity:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        allowed = order[order.index(min_severity):] if min_severity in order else order
        stmt = stmt.where(SystemEvent.severity.in_(allowed))
    return [
        {
            "id": e.id, "component": e.component, "event_type": e.event_type,
            "severity": str(e.severity), "message": e.message,
            "details": e.details, "created_at": e.created_at.isoformat(),
        }
        for e in db.scalars(stmt)
    ]
