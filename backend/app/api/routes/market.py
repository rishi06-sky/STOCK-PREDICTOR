"""Market data endpoints: securities, quotes, history, market status."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, rate_limit
from app.core.config import settings
from app.database.session import get_db
from app.market_data.market_hours import get_session_info
from app.models.enums import AssetType, DataQuality
from app.models.market import Exchange, Market, PriceData, Quote, Security
from app.models.platform import User
from app.schemas.common import BarOut, MarketStatusOut, Page, QuoteOut, SecurityOut

router = APIRouter(prefix="/market", tags=["market"])


def _security_out(security: Security) -> SecurityOut:
    return SecurityOut(
        id=security.id, symbol=security.symbol, name=security.name,
        exchange=security.exchange.code if security.exchange else None,
        sector=security.sector, industry=security.industry,
        currency=security.currency, asset_type=str(security.asset_type),
        is_active=security.is_active,
    )


@router.get("/securities", response_model=Page[SecurityOut])
def list_securities(
    q: str | None = Query(default=None, max_length=64, description="symbol or name search"),
    exchange: str | None = None,
    sector: str | None = None,
    asset_type: AssetType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    stmt = select(Security).join(Exchange, Exchange.id == Security.exchange_id)
    count_stmt = (
        select(func.count()).select_from(Security)
        .join(Exchange, Exchange.id == Security.exchange_id)
    )

    filters = [Security.is_active.is_(True)]
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(or_(Security.symbol.ilike(pattern), Security.name.ilike(pattern)))
    if exchange:
        filters.append(Exchange.code == exchange.upper())
    if sector:
        filters.append(Security.sector == sector)
    if asset_type:
        filters.append(Security.asset_type == asset_type)

    stmt = stmt.where(*filters).order_by(Security.symbol).limit(limit).offset(offset)
    total = db.scalar(count_stmt.where(*filters)) or 0
    return Page[SecurityOut](
        items=[_security_out(s) for s in db.scalars(stmt)],
        total=int(total), limit=limit, offset=offset,
    )


@router.get("/securities/{symbol}", response_model=SecurityOut)
def get_security(
    symbol: str, exchange: str | None = None, db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    return _security_out(_resolve(db, symbol, exchange))


def _resolve(db: Session, symbol: str, exchange: str | None = None) -> Security:
    stmt = (
        select(Security)
        .join(Exchange, Exchange.id == Security.exchange_id)
        .where(Security.symbol == symbol.upper())
    )
    if exchange:
        stmt = stmt.where(Exchange.code == exchange.upper())
    security = db.scalars(stmt.limit(1)).first()
    if security is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown symbol: {symbol}"
        )
    return security


@router.get("/quote/{symbol}", response_model=QuoteOut)
def get_quote(
    symbol: str, exchange: str | None = None, db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    security = _resolve(db, symbol, exchange)
    quote = db.get(Quote, security.id)
    if quote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no quote stored for {security.symbol}; it may not have been ingested yet",
        )

    age = (datetime.now(timezone.utc) - quote.source_timestamp).total_seconds()
    return QuoteOut(
        security_id=security.id, symbol=security.symbol,
        exchange=security.exchange.code if security.exchange else None,
        currency=security.currency,
        price=float(quote.price),
        previous_close=float(quote.previous_close) if quote.previous_close else None,
        change=float(quote.change) if quote.change else None,
        change_pct=quote.change_pct,
        day_open=float(quote.day_open) if quote.day_open else None,
        day_high=float(quote.day_high) if quote.day_high else None,
        day_low=float(quote.day_low) if quote.day_low else None,
        volume=quote.volume, provider=quote.provider, quality=str(quote.quality),
        source_timestamp=quote.source_timestamp, age_seconds=round(age, 1),
        is_stale=(
            quote.quality is not DataQuality.SYNTHETIC
            and age > settings.quote_staleness_seconds
        ),
    )


@router.get("/history/{symbol}", response_model=list[BarOut])
def get_history(
    symbol: str,
    exchange: str | None = None,
    days: int = Query(default=180, ge=1, le=2000),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    security = _resolve(db, symbol, exchange)
    start = date.today() - timedelta(days=days)
    rows = db.scalars(
        select(PriceData)
        .where(PriceData.security_id == security.id, PriceData.trade_date >= start)
        .order_by(PriceData.trade_date)
    ).all()
    return [
        BarOut(
            trade_date=r.trade_date, open=float(r.open), high=float(r.high),
            low=float(r.low), close=float(r.close), volume=r.volume,
            quality=str(r.quality),
        )
        for r in rows
    ]


@router.get("/status", response_model=list[MarketStatusOut])
def market_status(db: Session = Depends(get_db), _: None = Depends(rate_limit)):
    out = []
    for exchange in db.scalars(
        select(Exchange).join(Market, Market.id == Exchange.market_id)
        .where(Exchange.is_active.is_(True)).order_by(Exchange.code)
    ):
        info = get_session_info(
            exchange.code, exchange.market.timezone, exchange.open_time, exchange.close_time
        )
        out.append(
            MarketStatusOut(
                exchange=exchange.code, market=exchange.market.name,
                is_open=info.is_open, reason=info.reason, local_time=info.local_time,
                next_open=info.next_open, next_close=info.next_close,
                holidays_known=info.holidays_known,
            )
        )
    return out


@router.get("/movers", response_model=list[QuoteOut])
def market_movers(
    direction: str = Query(default="both", pattern="^(gainers|losers|both)$"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    stmt = (
        select(Quote, Security)
        .join(Security, Security.id == Quote.security_id)
        .where(Quote.change_pct.isnot(None), Security.asset_type == AssetType.EQUITY)
    )
    if direction == "gainers":
        stmt = stmt.order_by(Quote.change_pct.desc())
    elif direction == "losers":
        stmt = stmt.order_by(Quote.change_pct.asc())
    else:
        stmt = stmt.order_by(func.abs(Quote.change_pct).desc())

    now = datetime.now(timezone.utc)
    return [
        QuoteOut(
            security_id=s.id, symbol=s.symbol,
            exchange=s.exchange.code if s.exchange else None,
            currency=s.currency,
            price=float(q.price),
            previous_close=float(q.previous_close) if q.previous_close else None,
            change=float(q.change) if q.change else None, change_pct=q.change_pct,
            day_open=float(q.day_open) if q.day_open else None,
            day_high=float(q.day_high) if q.day_high else None,
            day_low=float(q.day_low) if q.day_low else None,
            volume=q.volume, provider=q.provider, quality=str(q.quality),
            source_timestamp=q.source_timestamp,
            age_seconds=round((now - q.source_timestamp).total_seconds(), 1),
            is_stale=(
                q.quality is not DataQuality.SYNTHETIC
                and (now - q.source_timestamp).total_seconds() > settings.quote_staleness_seconds
            ),
        )
        for q, s in db.execute(stmt.limit(limit)).all()
    ]


@router.get("/indices", response_model=list[QuoteOut])
def indices(db: Session = Depends(get_db), _: None = Depends(rate_limit)):
    rows = db.execute(
        select(Quote, Security)
        .join(Security, Security.id == Quote.security_id)
        .where(Security.asset_type == AssetType.INDEX)
    ).all()
    now = datetime.now(timezone.utc)
    return [
        QuoteOut(
            security_id=s.id, symbol=s.symbol,
            exchange=s.exchange.code if s.exchange else None,
            currency=s.currency,
            price=float(q.price),
            previous_close=float(q.previous_close) if q.previous_close else None,
            change=float(q.change) if q.change else None, change_pct=q.change_pct,
            day_open=None, day_high=None, day_low=None, volume=q.volume,
            provider=q.provider, quality=str(q.quality),
            source_timestamp=q.source_timestamp,
            age_seconds=round((now - q.source_timestamp).total_seconds(), 1),
            is_stale=False,
        )
        for q, s in rows
    ]
