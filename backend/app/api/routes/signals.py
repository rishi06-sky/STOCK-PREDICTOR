"""Signals, opportunities and per-security analysis."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import rate_limit
from app.database.session import get_db
from app.fundamentals.analysis import score_fundamentals
from app.market_data.market_hours import get_session_info
from app.ml.dataset import load_price_frame
from app.models.analysis import NewsArticle, NewsSentiment, Signal
from app.models.enums import RiskLevel, SignalStatus, SignalType
from app.models.market import Exchange, Quote, Security
from app.models.platform import ModelVersion
from app.news.pipeline import NewsPipeline, is_breaking
from app.schemas.common import Page, RationaleOut, SignalOut
from app.technical import indicators as ta
from app.technical.features import FeatureConfig, compute_features

router = APIRouter(tags=["signals"])


def _signal_out(signal: Signal, security: Security, model_version: str | None) -> SignalOut:
    return SignalOut(
        id=signal.id, security_id=security.id, symbol=security.symbol,
        name=security.name,
        exchange=security.exchange.code if security.exchange else None,
        sector=security.sector, signal=str(signal.signal), status=str(signal.status),
        confidence=float(signal.confidence),
        expected_return=float(signal.expected_return) if signal.expected_return is not None else None,
        risk_level=str(signal.risk_level), horizon_days=signal.horizon_days,
        reference_price=float(signal.reference_price),
        entry_low=float(signal.entry_low) if signal.entry_low else None,
        entry_high=float(signal.entry_high) if signal.entry_high else None,
        stop_loss=float(signal.stop_loss) if signal.stop_loss else None,
        take_profit=float(signal.take_profit) if signal.take_profit else None,
        reward_to_risk=float(signal.reward_to_risk) if signal.reward_to_risk else None,
        opportunity_score=(
            float(signal.opportunity_score) if signal.opportunity_score is not None else None
        ),
        regime=str(signal.regime) if signal.regime else None,
        data_quality=str(signal.data_quality), price_as_of=signal.price_as_of,
        generated_at=signal.generated_at, expires_at=signal.expires_at,
        model_version=model_version,
        rationale=[RationaleOut(**r) for r in (signal.rationale or [])],
    )


@router.get("/signals", response_model=Page[SignalOut])
def list_signals(
    signal: list[SignalType] | None = Query(default=None),
    exchange: str | None = None,
    sector: str | None = None,
    risk_level: RiskLevel | None = None,
    min_confidence: float = Query(default=0.0, ge=0, le=1),
    max_horizon_days: int | None = Query(default=None, ge=1),
    active_only: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    stmt = (
        select(Signal, Security, ModelVersion.version)
        .join(Security, Security.id == Signal.security_id)
        .join(Exchange, Exchange.id == Security.exchange_id)
        .outerjoin(ModelVersion, ModelVersion.id == Signal.model_version_id)
    )
    count_stmt = (
        select(func.count()).select_from(Signal)
        .join(Security, Security.id == Signal.security_id)
        .join(Exchange, Exchange.id == Security.exchange_id)
    )

    filters = [Signal.confidence >= min_confidence]
    if active_only:
        filters += [
            Signal.status == SignalStatus.ACTIVE,
            Signal.expires_at > datetime.now(timezone.utc),
        ]
    if signal:
        filters.append(Signal.signal.in_(signal))
    if exchange:
        filters.append(Exchange.code == exchange.upper())
    if sector:
        filters.append(Security.sector == sector)
    if risk_level:
        filters.append(Signal.risk_level == risk_level)
    if max_horizon_days:
        filters.append(Signal.horizon_days <= max_horizon_days)

    rows = db.execute(
        stmt.where(*filters)
        .order_by(Signal.generated_at.desc())
        .limit(limit).offset(offset)
    ).all()
    total = db.scalar(count_stmt.where(*filters)) or 0

    return Page[SignalOut](
        items=[_signal_out(s, sec, mv) for s, sec, mv in rows],
        total=int(total), limit=limit, offset=offset,
    )


@router.get("/opportunities", response_model=Page[SignalOut])
def opportunities(
    exchange: str | None = None,
    sector: str | None = None,
    risk_level: RiskLevel | None = None,
    min_confidence: float = Query(default=0.0, ge=0, le=1),
    min_expected_return: float | None = None,
    max_horizon_days: int | None = Query(default=None, ge=1),
    direction: str = Query(default="all", pattern="^(all|long|short)$"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    """Actionable signals ranked by risk-adjusted opportunity score."""
    long_types = [SignalType.BUY, SignalType.STRONG_BUY]
    short_types = [SignalType.SELL, SignalType.STRONG_SELL]
    wanted = (
        long_types if direction == "long"
        else short_types if direction == "short"
        else long_types + short_types
    )

    filters = [
        Signal.status == SignalStatus.ACTIVE,
        Signal.expires_at > datetime.now(timezone.utc),
        Signal.signal.in_(wanted),
        Signal.confidence >= min_confidence,
    ]
    if exchange:
        filters.append(Exchange.code == exchange.upper())
    if sector:
        filters.append(Security.sector == sector)
    if risk_level:
        filters.append(Signal.risk_level == risk_level)
    if max_horizon_days:
        filters.append(Signal.horizon_days <= max_horizon_days)
    if min_expected_return is not None:
        filters.append(Signal.expected_return >= min_expected_return)

    rows = db.execute(
        select(Signal, Security, ModelVersion.version)
        .join(Security, Security.id == Signal.security_id)
        .join(Exchange, Exchange.id == Security.exchange_id)
        .outerjoin(ModelVersion, ModelVersion.id == Signal.model_version_id)
        .where(*filters)
        .order_by(Signal.opportunity_score.desc().nullslast())
        .limit(limit).offset(offset)
    ).all()
    total = db.scalar(
        select(func.count()).select_from(Signal)
        .join(Security, Security.id == Signal.security_id)
        .join(Exchange, Exchange.id == Security.exchange_id)
        .where(*filters)
    ) or 0

    return Page[SignalOut](
        items=[_signal_out(s, sec, mv) for s, sec, mv in rows],
        total=int(total), limit=limit, offset=offset,
    )


@router.get("/securities/{symbol}/analysis")
def security_analysis(
    symbol: str, exchange: str | None = None,
    db: Session = Depends(get_db), _: None = Depends(rate_limit),
):
    """Everything the stock page needs in one call."""
    stmt = (
        select(Security).join(Exchange, Exchange.id == Security.exchange_id)
        .where(Security.symbol == symbol.upper())
    )
    if exchange:
        stmt = stmt.where(Exchange.code == exchange.upper())
    security = db.scalars(stmt.limit(1)).first()
    if security is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown symbol: {symbol}"
        )

    frame = load_price_frame(db, security.id)
    technical: dict = {}
    if len(frame) >= 60:
        features = compute_features(frame, FeatureConfig())
        latest = features.dropna()
        rsi_series = ta.rsi(frame["close"])
        macd_frame = ta.macd(frame["close"])
        bb = ta.bollinger_bands(frame["close"])
        adx_frame = ta.adx(frame["high"], frame["low"], frame["close"])
        atr_series = ta.atr(frame["high"], frame["low"], frame["close"])
        sr = ta.rolling_support_resistance(frame["high"], frame["low"], frame["close"])

        def _last(series):
            value = series.dropna()
            return round(float(value.iloc[-1]), 4) if len(value) else None

        technical = {
            "rsi": _last(rsi_series),
            "macd": _last(macd_frame["macd"]),
            "macd_signal": _last(macd_frame["macd_signal"]),
            "macd_hist": _last(macd_frame["macd_hist"]),
            "sma_20": _last(ta.sma(frame["close"], 20)),
            "sma_50": _last(ta.sma(frame["close"], 50)),
            "sma_200": _last(ta.sma(frame["close"], 200)),
            "ema_20": _last(ta.ema(frame["close"], 20)),
            "bb_upper": _last(bb["bb_upper"]), "bb_lower": _last(bb["bb_lower"]),
            "atr": _last(atr_series),
            "adx": _last(adx_frame["adx"]),
            "obv": _last(ta.obv(frame["close"], frame["volume"])),
            "vwap_20": _last(ta.vwap(frame["high"], frame["low"], frame["close"], frame["volume"], window=20)),
            "support": _last(sr["support"]), "resistance": _last(sr["resistance"]),
            "realized_volatility": _last(ta.realized_volatility(frame["close"], 20)),
            "feature_rows": int(len(latest)),
        }

    signal = db.scalars(
        select(Signal)
        .where(Signal.security_id == security.id, Signal.status == SignalStatus.ACTIVE)
        .order_by(Signal.generated_at.desc()).limit(1)
    ).first()
    model_version = (
        db.scalar(select(ModelVersion.version).where(ModelVersion.id == signal.model_version_id))
        if signal and signal.model_version_id else None
    )

    quote = db.get(Quote, security.id)
    news_pipeline = NewsPipeline(db)
    articles = news_pipeline.latest_articles(security.id, limit=8)
    fundamentals = score_fundamentals(db, security)
    session = get_session_info(
        security.exchange.code, security.exchange.market.timezone,
        security.exchange.open_time, security.exchange.close_time,
    )

    return {
        "security": {
            "id": security.id, "symbol": security.symbol, "name": security.name,
            "exchange": security.exchange.code, "sector": security.sector,
            "industry": security.industry, "currency": security.currency,
            "asset_type": str(security.asset_type),
        },
        "market_open": session.is_open,
        "quote": (
            {
                "price": float(quote.price),
                "change": float(quote.change) if quote.change else None,
                "change_pct": quote.change_pct,
                "quality": str(quote.quality),
                "source_timestamp": quote.source_timestamp.isoformat(),
                "provider": quote.provider,
            }
            if quote else None
        ),
        "technical": technical,
        "fundamentals": fundamentals.as_dict(),
        "signal": _signal_out(signal, security, model_version).model_dump() if signal else None,
        "sentiment": news_pipeline.recent_sentiment(security.id),
        "news": [
            {
                "headline": article.headline, "source": article.source,
                "url": article.url,
                "published_at": article.published_at.isoformat(),
                "retrieved_at": article.retrieved_at.isoformat(),
                "is_breaking": is_breaking(article),
                "sentiment": (
                    {
                        "label": str(sentiment.label),
                        "score": float(sentiment.score),
                        "confidence": float(sentiment.confidence),
                    }
                    if sentiment else None
                ),
            }
            for article, sentiment in articles
        ],
        "history_bars": int(len(frame)),
    }
