"""System health, models, backtesting and admin controls."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, rate_limit, record_audit, require_admin
from app.backtesting.engine import BacktestConfig, Backtester
from app.core.config import settings
from app.database.session import get_db, session_scope
from app.ml.dataset import load_price_frame
from app.ml.registry import detect_feature_drift, get_production_model, rollback_model
from app.models.analysis import Prediction, RegimeState
from app.models.enums import AssetType, DataQuality, ModelStatus
from app.models.market import Exchange, Security
from app.models.platform import Backtest, BacktestTrade, ModelMetric, ModelVersion, User
from app.monitoring.health import recent_events, system_health
from app.schemas.common import BacktestOut, BacktestRequest, Message, ModelVersionOut

router = APIRouter(tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness/readiness probe. Deliberately unauthenticated and cheap."""
    return system_health(db, include_providers=False)


@router.get("/health/full")
def health_full(
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Full health including live provider probes (makes outbound calls)."""
    return system_health(db, include_providers=True)


@router.get("/system/events")
def system_events(
    limit: int = Query(default=50, ge=1, le=200),
    min_severity: str | None = Query(default=None, pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return recent_events(db, limit=limit, min_severity=min_severity)


@router.get("/system/regime")
def current_regimes(db: Session = Depends(get_db), _: None = Depends(rate_limit)):
    rows = db.execute(
        select(RegimeState, Exchange)
        .join(Exchange, Exchange.id == RegimeState.exchange_id)
        .order_by(RegimeState.trade_date.desc())
    ).all()
    seen, out = set(), []
    for state, exchange in rows:
        if exchange.code in seen:
            continue
        seen.add(exchange.code)
        out.append(
            {
                "exchange": exchange.code, "trade_date": state.trade_date.isoformat(),
                "regime": str(state.regime),
                "volatility_regime": str(state.volatility_regime),
                "trend_strength": state.trend_strength,
                "realized_volatility": state.realized_volatility,
                "breadth": state.breadth, "details": state.details,
            }
        )
    return out


# --------------------------------------------------------------------- models
@router.get("/models", response_model=list[ModelVersionOut])
def list_models(
    status_filter: ModelStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    stmt = select(ModelVersion).order_by(ModelVersion.id.desc())
    if status_filter:
        stmt = stmt.where(ModelVersion.status == status_filter)
    return list(db.scalars(stmt.limit(100)))


@router.get("/models/{model_id}")
def model_detail(
    model_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    model = db.get(ModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")

    metrics = db.scalars(
        select(ModelMetric).where(ModelMetric.model_version_id == model_id)
    ).all()
    grouped: dict[str, dict] = {}
    for metric in metrics:
        grouped.setdefault(metric.split, {})[metric.metric_name] = round(
            float(metric.metric_value), 6
        )

    return {
        "model": ModelVersionOut.model_validate(model).model_dump(),
        "metrics": grouped,
        "feature_names": model.feature_names,
        "hyperparameters": model.hyperparameters,
        "artifact_sha256": model.artifact_sha256,
        # Stated explicitly so nobody reads validation numbers as live results.
        "disclaimer": (
            "Metrics are out-of-sample validation results measured during "
            "training, not live trading performance."
        ),
    }


@router.get("/models/{model_id}/drift")
def model_drift(
    model_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    model = db.get(ModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")

    from app.technical.features import FeatureConfig, compute_features

    frames = []
    for security in db.scalars(
        select(Security).where(
            Security.is_active.is_(True), Security.asset_type == AssetType.EQUITY
        ).limit(25)
    ):
        prices = load_price_frame(db, security.id)
        if len(prices) < 250:
            continue
        features = compute_features(prices, FeatureConfig()).dropna()
        if len(features):
            frames.append(features.tail(60))

    if not frames:
        return {"note": "not enough recent feature rows to assess drift", "drift_detected": False}
    return detect_feature_drift(model, pd.concat(frames)).as_dict()


@router.post("/models/{model_id}/rollback", response_model=Message)
def rollback(
    model_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    model = db.get(ModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    previous = rollback_model(db, model.name, model.horizon_days, actor=user.email)
    if previous is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no archived version is available to roll back to",
        )
    return Message(message=f"rolled back to {previous.name}:{previous.version}")


# ------------------------------------------------------------------ backtests
@router.get("/backtests", response_model=list[BacktestOut])
def list_backtests(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list(
        db.scalars(select(Backtest).order_by(Backtest.id.desc()).limit(50))
    )


@router.get("/backtests/{backtest_id}")
def backtest_detail(
    backtest_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    backtest = db.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest not found")
    trades = db.scalars(
        select(BacktestTrade).where(BacktestTrade.backtest_id == backtest_id).limit(500)
    ).all()
    return {
        "backtest": BacktestOut.model_validate(backtest).model_dump(),
        "equity_curve": backtest.equity_curve,
        "config": backtest.config,
        "trades": [
            {
                "entry_date": str(t.entry_date), "exit_date": str(t.exit_date),
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price) if t.exit_price else None,
                "net_pnl": float(t.net_pnl) if t.net_pnl is not None else None,
                "return_pct": t.return_pct, "holding_days": t.holding_days,
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ],
        "disclaimer": (
            "BACKTEST PERFORMANCE. Simulated results using historical data; they "
            "do not represent live trading and are not predictive of future returns."
        ),
    }


@router.post("/backtests", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
def run_backtest(
    payload: BacktestRequest,
    background: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if payload.start_date >= payload.end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="start_date must precede end_date"
        )
    model = get_production_model(db)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no production model is available to backtest",
        )
    record = Backtest(
        name=payload.name, model_version_id=model.id,
        start_date=payload.start_date, end_date=payload.end_date,
        initial_capital=payload.initial_capital,
        config=payload.model_dump(mode="json"),
        data_quality=DataQuality.HISTORICAL,
    )
    db.add(record)
    record_audit(
        db, actor=user.email, action="backtest.start", resource_type="backtest",
        resource_id=payload.name, user_id=user.id,
    )
    db.commit()
    background.add_task(_execute_backtest, record.id, payload)
    return {
        "backtest_id": record.id, "status": "running",
        "message": "backtest started; poll /backtests/{id} for results",
    }


def _execute_backtest(backtest_id: int, payload: BacktestRequest) -> None:
    """Run a backtest in the background using stored predictions."""
    from app.core.logging import get_logger

    log = get_logger(__name__)
    try:
        with session_scope() as db:
            record = db.get(Backtest, backtest_id)
            if record is None:
                return

            stmt = select(Security).where(
                Security.is_active.is_(True), Security.asset_type == AssetType.EQUITY
            )
            if payload.symbols:
                stmt = stmt.where(Security.symbol.in_([s.upper() for s in payload.symbols]))
            securities = list(db.scalars(stmt))

            prices, symbols, qualities = {}, {}, set()
            for security in securities:
                frame = load_price_frame(db, security.id)
                frame = frame[
                    (frame.index.date >= payload.start_date)
                    & (frame.index.date <= payload.end_date)
                ]
                if len(frame) > 30:
                    prices[security.id] = frame
                    symbols[security.id] = security.symbol

            rows = db.execute(
                select(Prediction.as_of_date, Prediction.security_id, Prediction.raw_probability)
                .where(
                    Prediction.as_of_date >= payload.start_date,
                    Prediction.as_of_date <= payload.end_date,
                    Prediction.raw_probability.isnot(None),
                )
            ).all()

            if not rows or not prices:
                record.completed_at = datetime.now(timezone.utc)
                record.equity_curve = []
                record.config = {
                    **(record.config or {}),
                    "error": (
                        "no stored predictions overlap this period. Backtests replay "
                        "recorded model output; run the prediction pipeline over the "
                        "period first."
                    ),
                }
                db.commit()
                return

            signals = pd.DataFrame(
                [{"date": d, "security_id": sid, "probability": float(p)} for d, sid, p in rows]
            )

            # If any input bar is synthetic, the whole run is flagged synthetic.
            from app.models.market import PriceData

            for security_id in prices:
                quality = db.scalar(
                    select(PriceData.quality)
                    .where(PriceData.security_id == security_id)
                    .limit(1)
                )
                if quality:
                    qualities.add(quality)

            config = BacktestConfig(
                initial_capital=payload.initial_capital,
                commission_bps=payload.commission_bps,
                slippage_bps=payload.slippage_bps,
                position_size_pct=payload.position_size_pct,
                max_positions=payload.max_positions,
                stop_loss_pct=payload.stop_loss_pct,
                take_profit_pct=payload.take_profit_pct,
                max_holding_days=payload.max_holding_days,
                entry_threshold=payload.entry_threshold,
                exit_threshold=payload.exit_threshold,
            )
            result = Backtester(config).run(prices, signals, symbols=symbols)

            metrics = result.metrics
            record.total_return = metrics.get("total_return")
            record.cagr = metrics.get("cagr")
            record.sharpe_ratio = metrics.get("sharpe_ratio")
            record.sortino_ratio = metrics.get("sortino_ratio")
            record.max_drawdown = metrics.get("max_drawdown")
            record.win_rate = metrics.get("win_rate")
            record.profit_factor = metrics.get("profit_factor")
            record.total_trades = metrics.get("total_trades")
            record.average_trade_return = metrics.get("average_trade_return")
            record.volatility = metrics.get("volatility")
            record.data_quality = (
                DataQuality.SYNTHETIC
                if DataQuality.SYNTHETIC in qualities
                else DataQuality.HISTORICAL
            )
            record.equity_curve = [
                {"date": day.date().isoformat(), "equity": round(float(value), 2)}
                for day, value in result.equity_curve.items()
            ]
            record.config = {**(record.config or {}), "warnings": result.warnings}
            record.completed_at = datetime.now(timezone.utc)

            for trade in result.trades[:1000]:
                db.add(
                    BacktestTrade(
                        backtest_id=record.id, security_id=trade.security_id,
                        entry_date=trade.entry_date, exit_date=trade.exit_date,
                        entry_price=trade.entry_price, exit_price=trade.exit_price,
                        quantity=trade.quantity, gross_pnl=trade.gross_pnl,
                        net_pnl=trade.net_pnl, costs=trade.costs,
                        return_pct=trade.return_pct, holding_days=trade.holding_days,
                        exit_reason=trade.exit_reason,
                    )
                )
            db.commit()
    except Exception as exc:
        log.error("backtest_failed", backtest_id=backtest_id, error=str(exc))


# ---------------------------------------------------------------- admin ops
@router.post("/system/kill-switch", response_model=Message)
def toggle_kill_switch(
    engage: bool = Query(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Emergency halt. Blocks every new order immediately, in paper and live.

    This changes the running process only; set KILL_SWITCH_ENGAGED in the
    environment to make it survive a restart.
    """
    settings.kill_switch_engaged = engage
    record_audit(
        db, actor=user.email, action="system.kill_switch",
        details={"engaged": engage}, user_id=user.id,
    )
    db.commit()
    return Message(
        message=(
            "KILL SWITCH ENGAGED -- all new orders are blocked"
            if engage else "kill switch released"
        ),
        detail={
            "engaged": engage,
            "persist_hint": "set KILL_SWITCH_ENGAGED=true in .env to survive restarts",
        },
    )


@router.get("/system/trading-status")
def trading_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.trading.brokers.live_guard import live_trading_status

    return {
        **live_trading_status(broker_connected=False).as_dict(),
        "kill_switch_engaged": settings.kill_switch_engaged,
        "paper_starting_cash": settings.paper_starting_cash,
        "commission_bps": settings.commission_bps,
        "slippage_bps": settings.slippage_bps,
    }


# ------------------------------------------------------- kite tick stream
@router.get("/system/stream")
def stream_status(user: User = Depends(get_current_user)):
    """Status of the pushed tick feed, if one is configured."""
    from app.market_data.tick_service import get_tick_service

    if not settings.kite_streaming_enabled:
        return {
            "enabled": False,
            "note": (
                "Streaming is off. Quotes are polled every "
                f"{settings.ingest_intraday_interval_minutes} minutes and are "
                "DELAYED, not real-time. Set KITE_STREAMING_ENABLED=true with "
                "Kite credentials for an exchange-licensed live feed."
            ),
        }
    return get_tick_service().status()


@router.get("/system/kite/login-url")
def kite_login_url(user: User = Depends(require_admin)):
    """The Zerodha login URL for today's access token.

    Kite access tokens expire each morning around 07:30 IST. Visiting this URL
    and completing the login returns a `request_token` on the redirect, which
    is then exchanged via POST /system/kite/session.
    """
    from app.market_data.providers.kite import login_url

    if not settings.kite_api_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="KITE_API_KEY is not configured"
        )
    return {
        "login_url": login_url(settings.kite_api_key),
        "instructions": (
            "Open the URL, sign in to Zerodha, and copy the request_token from "
            "the redirect query string. Then POST it to /system/kite/session."
        ),
        "token_expires": "daily, around 07:30 IST",
    }


@router.post("/system/kite/session", response_model=dict)
def kite_exchange_token(
    request_token: str = Query(..., min_length=6, max_length=128),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Exchange a request_token for today's access token.

    The resulting token is returned once and NOT persisted by the server. Put
    it in KITE_ACCESS_TOKEN and restart the worker; storing a broker
    credential in the application database is a decision for the operator, not
    a default this code makes.
    """
    from app.market_data.providers.kite import KiteAuthError, KiteConnectProvider

    if not (settings.kite_api_key and settings.kite_api_secret):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="KITE_API_KEY and KITE_API_SECRET must both be configured",
        )

    provider = KiteConnectProvider(api_key=settings.kite_api_key, access_token=None)
    try:
        session_data = provider.exchange_request_token(
            request_token, settings.kite_api_secret
        )
    except KiteAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    record_audit(
        db, actor=user.email, action="system.kite.session",
        details={"user_id": session_data.get("user_id")}, user_id=user.id,
    )
    db.commit()

    return {
        "access_token": session_data.get("access_token"),
        "kite_user_id": session_data.get("user_id"),
        "login_time": session_data.get("login_time"),
        "next_step": (
            "Set KITE_ACCESS_TOKEN to this value and restart the worker. "
            "The token is not stored server-side."
        ),
    }


@router.post("/system/pipeline/run", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
def trigger_pipeline(
    background: BackgroundTasks,
    horizon_days: int = Query(default=5, ge=1, le=60),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    record_audit(db, actor=user.email, action="system.pipeline.run", user_id=user.id)
    db.commit()
    background.add_task(_run_pipeline, horizon_days)
    return {"status": "started", "message": "pipeline cycle running in the background"}


def _run_pipeline(horizon_days: int) -> None:
    from app.core.logging import get_logger
    from app.services.pipeline import Pipeline

    log = get_logger(__name__)
    try:
        with session_scope() as db:
            Pipeline(db).run_full_cycle(horizon_days=horizon_days)
    except Exception as exc:
        log.error("pipeline_run_failed", error=str(exc))
