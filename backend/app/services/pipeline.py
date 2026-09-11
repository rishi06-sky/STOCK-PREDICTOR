"""End-to-end pipeline orchestration.

One pass of the whole system:

    ingest -> validate -> features -> predict -> regime -> signals
           -> risk -> rank -> alerts -> paper trade -> portfolio -> record

Each stage records what it did and what it refused to do. A stage that cannot
run safely (stale data, no validated model) halts the stages downstream of it
rather than letting them operate on assumptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    AllProvidersFailed, InsufficientDataError, ModelNotAvailable, StaleDataError,
)
from app.core.logging import get_logger
from app.market_data.ingest import IngestionService
from app.ml.dataset import load_price_frame
from app.ml.prediction import PredictionService
from app.ml.regime import compute_breadth, detect_regime
from app.ml.registry import get_production_model
from app.models.analysis import RegimeState, Signal
from app.models.enums import AssetType, EventSeverity, MarketRegime, SignalStatus
from app.models.market import Exchange, Security
from app.models.platform import ModelMetric, SystemEvent
from app.models.trading import Portfolio
from app.news.pipeline import NewsPipeline
from app.signals.engine import SignalEngine

log = get_logger(__name__)


@dataclass(slots=True)
class StageResult:
    name: str
    ok: bool
    detail: dict = field(default_factory=dict)
    error: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "stage": self.name, "ok": self.ok, "detail": self.detail,
            "error": self.error, "skipped_reason": self.skipped_reason,
        }


@dataclass(slots=True)
class PipelineReport:
    started_at: datetime
    finished_at: datetime | None = None
    stages: list[StageResult] = field(default_factory=list)

    def add(self, stage: StageResult) -> StageResult:
        self.stages.append(stage)
        return stage

    @property
    def ok(self) -> bool:
        return all(s.ok or s.skipped_reason for s in self.stages)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": (
                round((self.finished_at - self.started_at).total_seconds(), 2)
                if self.finished_at else None
            ),
            "ok": self.ok,
            "stages": [s.as_dict() for s in self.stages],
        }


class Pipeline:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------ data
    def _universe(self) -> list[Security]:
        """Tradeable securities: equities only."""
        return list(
            self.db.scalars(
                select(Security).where(
                    Security.is_active.is_(True),
                    Security.asset_type == AssetType.EQUITY,
                )
            )
        )

    def _benchmarks(self) -> list[Security]:
        """Index series. Not tradeable, but required for regime detection."""
        return list(
            self.db.scalars(
                select(Security).where(
                    Security.is_active.is_(True),
                    Security.asset_type == AssetType.INDEX,
                )
            )
        )

    def ingest(self, *, lookback_days: int = 400, quotes: bool = True) -> StageResult:
        service = IngestionService(self.db)
        securities = self._universe()
        benchmarks = self._benchmarks()
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=lookback_days)

        reports = service.ingest_universe_daily(securities, start, end)
        written = sum(r.written for r in reports)
        failed = [r.symbol for r in reports if not r.ok]

        # Index history is ingested alongside, since regime detection depends
        # on it and a missing benchmark silently degrades every signal.
        index_reports = service.ingest_universe_daily(benchmarks, start, end)
        index_written = sum(r.written for r in index_reports)
        index_failed = [r.symbol for r in index_reports if not r.ok]

        quote_ok = 0
        if quotes:
            for security in securities:
                try:
                    if service.ingest_quote(security, commit=False).ok:
                        quote_ok += 1
                except Exception as exc:
                    log.warning("quote_ingest_failed", symbol=security.symbol, error=str(exc))
            self.db.commit()

        # Every provider failing for every symbol is an outage, not a bad day.
        total_failure = len(failed) == len(securities) and securities
        return StageResult(
            name="ingest",
            ok=not total_failure,
            detail={
                "securities": len(securities), "bars_written": written,
                "quotes_written": quote_ok, "failed_symbols": failed[:10],
                "failed_count": len(failed),
                "benchmarks": len(benchmarks), "benchmark_bars": index_written,
                "benchmark_failed": index_failed,
            },
            error=(
                "every provider failed for every security; downstream stages halted"
                if total_failure else None
            ),
        )

    def ingest_news(self, *, limit: int = 15) -> StageResult:
        pipeline = NewsPipeline(self.db)
        stored, errors = 0, []
        for security in self._universe():
            try:
                report = pipeline.ingest_for_security(security, limit=limit, commit=False)
                stored += report.stored
                errors.extend(report.errors[:1])
            except Exception as exc:
                errors.append(f"{security.symbol}: {exc}")
        self.db.commit()
        return StageResult(
            name="news", ok=True,
            detail={"articles_stored": stored, "provider_errors": len(errors)},
        )

    # ---------------------------------------------------------------- regime
    def detect_regimes(self) -> StageResult:
        results: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for exchange in self.db.scalars(select(Exchange).where(Exchange.is_active.is_(True))):
            index = self.db.scalars(
                select(Security).where(
                    Security.exchange_id == exchange.id,
                    Security.asset_type == AssetType.INDEX,
                )
            ).first()
            if index is None:
                continue

            frame = load_price_frame(self.db, index.id)
            if frame.empty:
                results[exchange.code] = "no index history"
                continue

            constituents = self.db.scalars(
                select(Security).where(
                    Security.exchange_id == exchange.id,
                    Security.asset_type == AssetType.EQUITY,
                    Security.is_active.is_(True),
                )
            ).all()
            closes = {}
            for security in constituents[:60]:
                series = load_price_frame(self.db, security.id)
                if not series.empty:
                    closes[security.id] = series["close"]

            detected = detect_regime(frame["close"], breadth=compute_breadth(closes))
            # Upsert on the natural key. Session.merge() matches on PRIMARY KEY
            # only, so it would insert a second row for the same
            # (exchange, date) and violate the unique constraint on any rerun
            # within the same session.
            stmt = pg_insert(RegimeState).values(
                exchange_id=exchange.id,
                trade_date=frame.index[-1].date(),
                regime=detected.regime,
                volatility_regime=detected.volatility_regime,
                trend_strength=detected.trend_strength,
                realized_volatility=detected.realized_volatility,
                breadth=detected.breadth,
                details=detected.as_dict(),
                computed_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_regime_states_exch_date",
                set_={
                    c: getattr(stmt.excluded, c)
                    for c in (
                        "regime", "volatility_regime", "trend_strength",
                        "realized_volatility", "breadth", "details", "computed_at",
                    )
                },
            )
            self.db.execute(stmt)
            results[exchange.code] = f"{detected.regime}/{detected.volatility_regime}"

        self.db.commit()
        return StageResult(name="regime", ok=True, detail={"exchanges": results})

    def current_regime(self, exchange_code: str = "NSE") -> tuple[MarketRegime, MarketRegime]:
        row = self.db.scalars(
            select(RegimeState)
            .join(Exchange, Exchange.id == RegimeState.exchange_id)
            .where(Exchange.code == exchange_code)
            .order_by(RegimeState.trade_date.desc())
            .limit(1)
        ).first()
        if row is None:
            return MarketRegime.UNKNOWN, MarketRegime.UNKNOWN
        return row.regime, row.volatility_regime

    # ----------------------------------------------------- predict + signal
    def generate_signals(self, *, horizon_days: int = 5) -> StageResult:
        model_version = get_production_model(self.db, horizon_days=horizon_days)
        if model_version is None:
            reason = (
                f"no validated production model for a {horizon_days}-day horizon; "
                "signal generation skipped"
            )
            self._event("signals_skipped", reason, EventSeverity.WARNING)
            self.db.commit()
            return StageResult(
                name="signals", ok=True, skipped_reason=reason,
                detail={"signals_generated": 0},
            )

        model_auc = self.db.scalar(
            select(ModelMetric.metric_value).where(
                ModelMetric.model_version_id == model_version.id,
                ModelMetric.split == "cv_mean",
                ModelMetric.metric_name == "roc_auc",
            )
        )
        model_auc = float(model_auc) if model_auc is not None else None

        securities = self._universe()
        predictor = PredictionService(self.db)
        engine = SignalEngine(self.db)

        predictions, failures = predictor.predict_universe(
            securities, horizon_days=horizon_days
        )
        by_id = {s.id: s for s in securities}

        counts: dict[str, int] = {}
        halted: list[str] = []
        news = NewsPipeline(self.db)

        for prediction in predictions:
            security = by_id.get(prediction.security_id)
            if security is None:
                continue
            regime, volatility_regime = self.current_regime(security.exchange.code)
            try:
                generated = engine.generate(
                    security, prediction,
                    price_frame=load_price_frame(self.db, security.id),
                    model_auc=model_auc,
                    regime=regime, volatility_regime=volatility_regime,
                    sentiment=news.recent_sentiment(security.id),
                )
            except StaleDataError as exc:
                halted.append(f"{security.symbol}: {exc}")
                continue
            engine.persist(generated, commit=False)
            counts[str(generated.signal)] = counts.get(str(generated.signal), 0) + 1

        self.db.commit()
        engine.expire_stale_signals()

        return StageResult(
            name="signals", ok=True,
            detail={
                "model": f"{model_version.name}:{model_version.version}",
                "model_auc": round(model_auc, 4) if model_auc else None,
                "predictions": len(predictions),
                "prediction_failures": len(failures),
                "signal_mix": counts,
                "halted_stale": len(halted),
            },
        )

    # ------------------------------------------------------------ ranking
    def rank_opportunities(self, *, limit: int = 50) -> StageResult:
        from app.models.enums import SignalType

        rows = self.db.execute(
            select(Signal, Security)
            .join(Security, Security.id == Signal.security_id)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.signal.in_(
                    [SignalType.BUY, SignalType.STRONG_BUY,
                     SignalType.SELL, SignalType.STRONG_SELL]
                ),
            )
            .order_by(Signal.opportunity_score.desc().nullslast())
            .limit(limit)
        ).all()

        return StageResult(
            name="ranking", ok=True,
            detail={
                "ranked": len(rows),
                "top": [
                    {
                        "rank": i + 1, "symbol": security.symbol,
                        "signal": str(signal.signal),
                        "confidence": round(float(signal.confidence), 4),
                        "score": (
                            round(float(signal.opportunity_score), 2)
                            if signal.opportunity_score is not None else None
                        ),
                        "expected_return": (
                            round(float(signal.expected_return), 5)
                            if signal.expected_return is not None else None
                        ),
                        "risk": str(signal.risk_level),
                    }
                    for i, (signal, security) in enumerate(rows[:10])
                ],
            },
        )

    # ---------------------------------------------------------- paper trade
    def run_paper_trading(self, *, max_new_positions: int = 5) -> StageResult:
        from app.trading.paper_engine import PaperTradingEngine

        portfolios = list(
            self.db.scalars(
                select(Portfolio).where(
                    Portfolio.is_active.is_(True), Portfolio.mode == "PAPER"
                )
            )
        )
        if not portfolios:
            return StageResult(
                name="paper_trading", ok=True,
                skipped_reason="no active paper portfolio configured",
            )

        summaries = []
        for portfolio in portfolios:
            engine = PaperTradingEngine(self.db, portfolio)
            report = engine.run_cycle(max_new_positions=max_new_positions)
            summaries.append({"portfolio": portfolio.name, **report.as_dict()})

        return StageResult(name="paper_trading", ok=True, detail={"portfolios": summaries})

    # ---------------------------------------------------------------- alerts
    def dispatch_alerts(self) -> StageResult:
        from app.notifications.dispatcher import AlertDispatcher

        dispatcher = AlertDispatcher(self.db)
        created = dispatcher.create_signal_alerts()
        sent = dispatcher.dispatch_pending()
        return StageResult(
            name="alerts", ok=True,
            detail={"created": created, "sent": sent["sent"], "suppressed": sent["suppressed"]},
        )

    # ------------------------------------------------------------------ run
    def run_full_cycle(
        self, *, horizon_days: int = 5, include_news: bool = True,
        include_trading: bool = True,
    ) -> PipelineReport:
        report = PipelineReport(started_at=datetime.now(timezone.utc))

        ingest = report.add(self.ingest())
        if not ingest.ok:
            self._event(
                "pipeline_halted",
                "market data ingestion failed for every security; downstream stages skipped",
                EventSeverity.CRITICAL,
            )
            self.db.commit()
            report.finished_at = datetime.now(timezone.utc)
            return report

        if include_news:
            report.add(self.ingest_news())
        report.add(self.detect_regimes())

        signals = report.add(self.generate_signals(horizon_days=horizon_days))
        report.add(self.rank_opportunities())

        if signals.skipped_reason:
            # No model means no signals; trading and alerting would have nothing
            # legitimate to act on.
            report.add(
                StageResult(
                    name="paper_trading", ok=True,
                    skipped_reason="no signals were generated this cycle",
                )
            )
            report.add(
                StageResult(
                    name="alerts", ok=True,
                    skipped_reason="no signals were generated this cycle",
                )
            )
        else:
            report.add(self.dispatch_alerts())
            if include_trading:
                report.add(self.run_paper_trading())

        report.finished_at = datetime.now(timezone.utc)
        self._event(
            "pipeline_cycle_complete",
            f"pipeline finished in {(report.finished_at - report.started_at).total_seconds():.1f}s",
            EventSeverity.INFO,
            {"stages": [s.name for s in report.stages]},
        )
        self.db.commit()
        return report

    def _event(self, event_type: str, message: str, severity, details: dict | None = None) -> None:
        self.db.add(
            SystemEvent(
                component="pipeline", event_type=event_type, severity=severity,
                message=message, details=details, created_at=datetime.now(timezone.utc),
            )
        )
