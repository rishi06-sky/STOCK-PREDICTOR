"""Background scheduler.

APScheduler in-process rather than Celery: the workload is a handful of
periodic jobs, and a broker plus worker pool would be more moving parts to
operate for no benefit at this scale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging import get_logger
from app.database.session import session_scope
from app.models.enums import EventSeverity
from app.models.platform import SystemEvent

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def _record(component: str, event_type: str, message: str, severity=EventSeverity.INFO) -> None:
    try:
        with session_scope() as db:
            db.add(
                SystemEvent(
                    component=component, event_type=event_type, severity=severity,
                    message=message, created_at=datetime.now(timezone.utc),
                )
            )
    except Exception as exc:  # scheduling must not die because logging failed
        log.error("scheduler_event_record_failed", error=str(exc))


def job_pipeline_cycle() -> None:
    from app.services.pipeline import Pipeline

    try:
        with session_scope() as db:
            report = Pipeline(db).run_full_cycle(horizon_days=5)
        log.info("scheduled_pipeline_complete", ok=report.ok)
    except Exception as exc:
        log.error("scheduled_pipeline_failed", error=str(exc))
        _record("scheduler", "pipeline_failed", str(exc)[:500], EventSeverity.ERROR)


def job_ingest_quotes() -> None:
    from app.market_data.ingest import IngestionService
    from app.models.market import Security
    from sqlalchemy import select

    try:
        with session_scope() as db:
            service = IngestionService(db)
            securities = list(
                db.scalars(select(Security).where(Security.is_active.is_(True)))
            )
            ok = sum(1 for s in securities if service.ingest_quote(s, commit=False).ok)
            db.commit()
        log.info("scheduled_quotes_complete", updated=ok, total=len(securities))
    except Exception as exc:
        log.error("scheduled_quotes_failed", error=str(exc))


def job_expire_signals() -> None:
    from app.signals.engine import SignalEngine

    try:
        with session_scope() as db:
            engine = SignalEngine(db)
            expired = engine.expire_stale_signals()
            invalidated = engine.invalidate_on_price_move()
        if expired or invalidated:
            log.info("signals_maintained", expired=expired, invalidated=invalidated)
    except Exception as exc:
        log.error("signal_maintenance_failed", error=str(exc))


def job_daily_snapshot() -> None:
    from sqlalchemy import select

    from app.models.trading import Portfolio
    from app.portfolio.engine import PortfolioEngine

    try:
        with session_scope() as db:
            engine = PortfolioEngine(db)
            for portfolio in db.scalars(
                select(Portfolio).where(Portfolio.is_active.is_(True))
            ):
                engine.snapshot(portfolio, commit=False)
            db.commit()
    except Exception as exc:
        log.error("snapshot_failed", error=str(exc))


def job_retrain() -> None:
    """Scheduled retraining.

    Trains a candidate and puts it through the promotion gate. A model that
    fails the gate is retained as a CANDIDATE and the incumbent keeps serving:
    retraining never silently replaces a validated model with a worse one.
    """
    from sqlalchemy import select

    from app.ml.dataset import build_dataset
    from app.ml.registry import PromotionGate, promote_model, register_model
    from app.ml.trainer import train_and_select
    from app.models.enums import AssetType, PredictionTarget
    from app.models.market import Security

    try:
        with session_scope() as db:
            securities = list(
                db.scalars(
                    select(Security).where(
                        Security.is_active.is_(True),
                        Security.asset_type == AssetType.EQUITY,
                    )
                )
            )
            for horizon in settings.horizons:
                try:
                    dataset = build_dataset(db, securities, horizon_days=horizon)
                    winner, _ = train_and_select(dataset)
                    version = register_model(
                        db, winner, name=f"direction_{horizon}d",
                        target=PredictionTarget.DIRECTION,
                    )
                    promoted, failures = promote_model(
                        db, version, winner, gate=PromotionGate(), actor="scheduler"
                    )
                    log.info(
                        "retrain_complete", horizon=horizon,
                        promoted=promoted, failures=failures,
                    )
                except Exception as exc:
                    log.error("retrain_horizon_failed", horizon=horizon, error=str(exc))
    except Exception as exc:
        log.error("retrain_failed", error=str(exc))
        _record("scheduler", "retrain_failed", str(exc)[:500], EventSeverity.ERROR)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled:
        log.info("scheduler_disabled")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,       # collapse missed runs into one
            "max_instances": 1,     # never overlap a long job with itself
            "misfire_grace_time": 300,
        },
    )

    scheduler.add_job(
        job_ingest_quotes, IntervalTrigger(minutes=settings.ingest_intraday_interval_minutes),
        id="ingest_quotes", name="ingest quotes", replace_existing=True,
    )
    scheduler.add_job(
        job_pipeline_cycle, IntervalTrigger(minutes=settings.pipeline_interval_minutes),
        id="pipeline_cycle", name="full pipeline cycle", replace_existing=True,
    )
    scheduler.add_job(
        job_expire_signals, IntervalTrigger(minutes=10),
        id="expire_signals", name="expire and invalidate signals", replace_existing=True,
    )
    scheduler.add_job(
        job_daily_snapshot, CronTrigger(hour=settings.ingest_eod_cron_hour, minute=30),
        id="daily_snapshot", name="daily portfolio snapshot", replace_existing=True,
    )
    scheduler.add_job(
        job_retrain, CronTrigger(day_of_week="sun", hour=settings.ml_retrain_cron_hour),
        id="retrain", name="weekly model retraining", replace_existing=True,
    )

    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
    _record("scheduler", "started", "background scheduler started")
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler_stopped")


def scheduler_status() -> dict:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    return {
        "running": _scheduler.running,
        "jobs": [
            {
                "id": job.id, "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
    }
