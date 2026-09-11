"""Portfolio, trades and alert endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_writable_user, rate_limit, record_audit
from app.database.session import get_db
from app.models.enums import AlertStatus, PositionStatus
from app.models.market import Security
from app.models.platform import Alert, User
from app.models.trading import Holding, Portfolio, Trade
from app.portfolio.engine import PortfolioEngine
from app.schemas.common import AlertOut, Message, Page, PortfolioOut, TradeOut
from app.trading.paper_engine import PaperTradingEngine, get_or_create_paper_portfolio

router = APIRouter(tags=["portfolio"])


def _resolve_portfolio(db: Session, user: User, portfolio_id: int | None) -> Portfolio:
    if portfolio_id is not None:
        portfolio = db.get(Portfolio, portfolio_id)
        if portfolio is None or portfolio.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="portfolio not found"
            )
        return portfolio
    return get_or_create_paper_portfolio(db, user.id)


@router.get("/portfolio", response_model=PortfolioOut)
def get_portfolio(
    portfolio_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    portfolio = _resolve_portfolio(db, user, portfolio_id)
    return PortfolioOut(**PortfolioEngine(db).value(portfolio).as_dict())


@router.get("/portfolio/performance")
def portfolio_performance(
    portfolio_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    """Realised performance. Labelled LIVE to keep it distinct from backtests."""
    portfolio = _resolve_portfolio(db, user, portfolio_id)
    engine = PortfolioEngine(db)
    curve = engine.equity_curve(portfolio.id)
    return {
        "performance": engine.live_performance(portfolio),
        "equity_curve": [
            {"date": day.date().isoformat(), "equity": round(float(value), 2)}
            for day, value in curve.items()
        ],
        "correlation": engine.correlation_matrix(portfolio),
    }


@router.get("/portfolio/trades", response_model=Page[TradeOut])
def list_trades(
    portfolio_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    portfolio = _resolve_portfolio(db, user, portfolio_id)
    rows = db.execute(
        select(Trade, Security)
        .join(Security, Security.id == Trade.security_id)
        .where(Trade.portfolio_id == portfolio.id)
        .order_by(Trade.executed_at.desc())
        .limit(limit).offset(offset)
    ).all()
    total = db.scalar(
        select(func.count()).select_from(Trade).where(Trade.portfolio_id == portfolio.id)
    ) or 0

    return Page[TradeOut](
        items=[
            TradeOut(
                id=t.id, symbol=s.symbol, side=str(t.side), quantity=float(t.quantity),
                price=float(t.price), reference_price=float(t.reference_price),
                commission=float(t.commission), slippage_cost=float(t.slippage_cost),
                realized_pnl=float(t.realized_pnl) if t.realized_pnl is not None else None,
                exit_reason=str(t.exit_reason) if t.exit_reason else None,
                mode=str(t.mode), executed_at=t.executed_at,
            )
            for t, s in rows
        ],
        total=int(total), limit=limit, offset=offset,
    )


@router.post("/portfolio/paper/run", response_model=dict)
def run_paper_cycle(
    portfolio_id: int | None = None,
    max_new_positions: int = Query(default=5, ge=0, le=20),
    user: User = Depends(get_writable_user),
    db: Session = Depends(get_db),
):
    """Trigger one paper-trading cycle manually. Uses the production signals."""
    portfolio = _resolve_portfolio(db, user, portfolio_id)
    report = PaperTradingEngine(db, portfolio).run_cycle(max_new_positions=max_new_positions)
    record_audit(
        db, actor=user.email, action="portfolio.paper.run",
        resource_type="portfolio", resource_id=str(portfolio.id), user_id=user.id,
        details={"opened": len(report.opened), "closed": len(report.closed)},
    )
    db.commit()
    return report.as_dict()


@router.post("/portfolio/positions/{holding_id}/close", response_model=dict)
def close_position(
    holding_id: int,
    user: User = Depends(get_writable_user),
    db: Session = Depends(get_db),
):
    from app.core.exceptions import DuplicateOrderError, KillSwitchEngaged, UnknownPriceError
    from app.models.enums import ExitReason
    from app.trading.order_manager import OrderManager

    holding = db.get(Holding, holding_id)
    if holding is None or holding.status is not PositionStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="open position not found"
        )
    portfolio = db.get(Portfolio, holding.portfolio_id)
    if portfolio is None or portfolio.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your position")

    try:
        report = OrderManager(db).close_position(
            portfolio, holding, reason=ExitReason.MANUAL
        )
    except (UnknownPriceError, KillSwitchEngaged) as exc:
        # Refusing to trade on an unknown price is correct behaviour, so it is
        # reported as a precondition failure rather than a server error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except DuplicateOrderError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    record_audit(
        db, actor=user.email, action="portfolio.position.close",
        resource_type="holding", resource_id=str(holding_id), user_id=user.id,
    )
    db.commit()
    return report.as_dict()


# --------------------------------------------------------------------- alerts
@router.get("/alerts", response_model=Page[AlertOut])
def list_alerts(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(rate_limit),
):
    filters = []
    if unread_only:
        filters.append(Alert.read_at.is_(None))
    rows = db.scalars(
        select(Alert).where(*filters)
        .order_by(Alert.created_at.desc()).limit(limit).offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Alert).where(*filters)) or 0
    return Page[AlertOut](
        items=[
            AlertOut(
                id=a.id, alert_type=str(a.alert_type), severity=str(a.severity),
                status=str(a.status), title=a.title, body=a.body, payload=a.payload,
                security_id=a.security_id, created_at=a.created_at,
                sent_at=a.sent_at, read_at=a.read_at,
            )
            for a in rows
        ],
        total=int(total), limit=limit, offset=offset,
    )


@router.post("/alerts/{alert_id}/read", response_model=Message)
def mark_alert_read(
    alert_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    alert.read_at = datetime.now(timezone.utc)
    db.commit()
    return Message(message="alert marked as read")
