"""Shared API schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class Message(BaseModel):
    message: str
    detail: dict | None = None


# ----------------------------------------------------------------------- auth
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    #: Returned exactly once, at creation. Never retrievable afterwards.
    key: str | None = None


# --------------------------------------------------------------------- market
class SecurityOut(ORMModel):
    id: int
    symbol: str
    name: str
    exchange: str | None = None
    sector: str | None
    industry: str | None
    currency: str
    asset_type: str
    is_active: bool


class QuoteOut(BaseModel):
    security_id: int
    symbol: str
    price: float
    previous_close: float | None
    change: float | None
    change_pct: float | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    volume: int | None
    provider: str
    #: LIVE / DELAYED / EOD / SYNTHETIC -- never conflate these.
    quality: str
    source_timestamp: datetime
    age_seconds: float
    is_stale: bool


class BarOut(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    quality: str


class MarketStatusOut(BaseModel):
    exchange: str
    market: str
    is_open: bool
    reason: str
    local_time: datetime
    next_open: datetime | None
    next_close: datetime | None
    holidays_known: bool


# -------------------------------------------------------------------- signals
class RationaleOut(BaseModel):
    factor: str
    detail: str
    contribution: str


class SignalOut(BaseModel):
    id: int
    security_id: int
    symbol: str
    name: str
    exchange: str | None
    sector: str | None
    signal: str
    status: str
    confidence: float
    expected_return: float | None
    risk_level: str
    horizon_days: int
    reference_price: float
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    take_profit: float | None
    reward_to_risk: float | None
    opportunity_score: float | None
    regime: str | None
    data_quality: str
    price_as_of: datetime
    generated_at: datetime
    expires_at: datetime
    model_version: str | None = None
    rationale: list[RationaleOut] = Field(default_factory=list)


class PredictionOut(BaseModel):
    security_id: int
    symbol: str
    as_of_date: date
    horizon_days: int
    probability: float
    expected_return: float | None
    reference_price: float
    model_version: str
    calibrated: bool
    data_quality: str


# ------------------------------------------------------------------ portfolio
class PositionOut(BaseModel):
    security_id: int
    symbol: str
    name: str
    sector: str | None
    quantity: float
    average_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    weight: float
    stop_loss: float | None
    take_profit: float | None
    price_is_stale: bool
    opened_at: datetime


class PortfolioOut(BaseModel):
    portfolio_id: int
    name: str
    mode: str
    currency: str
    cash: float
    positions_value: float
    equity: float
    starting_cash: float
    total_pnl: float
    total_pnl_pct: float
    unrealized_pnl: float
    realized_pnl: float
    exposure_pct: float
    drawdown_pct: float
    peak_equity: float
    open_positions: int
    positions: list[PositionOut]
    sector_allocation: dict[str, float]
    warnings: list[str]


class TradeOut(BaseModel):
    id: int
    symbol: str
    side: str
    quantity: float
    price: float
    reference_price: float
    commission: float
    slippage_cost: float
    realized_pnl: float | None
    exit_reason: str | None
    mode: str
    executed_at: datetime


# --------------------------------------------------------------------- alerts
class AlertOut(ORMModel):
    id: int
    alert_type: str
    severity: str
    status: str
    title: str
    body: str
    payload: dict | None
    security_id: int | None
    created_at: datetime
    sent_at: datetime | None
    read_at: datetime | None


# ----------------------------------------------------------------- ml/backtest
class ModelMetricOut(BaseModel):
    split: str
    metric_name: str
    metric_value: float
    sample_size: int | None


class ModelVersionOut(ORMModel):
    id: int
    name: str
    version: str
    algorithm: str
    target: str
    horizon_days: int
    status: str
    feature_set_version: str
    training_rows: int | None
    trained_at: datetime | None
    promoted_at: datetime | None
    notes: str | None


class BacktestRequest(BaseModel):
    name: str = Field(default="ad-hoc backtest", max_length=128)
    start_date: date
    end_date: date
    initial_capital: float = Field(default=1_000_000, gt=0)
    entry_threshold: float = Field(default=0.58, ge=0.5, le=0.99)
    exit_threshold: float = Field(default=0.45, ge=0.01, le=0.6)
    position_size_pct: float = Field(default=0.10, gt=0, le=1)
    max_positions: int = Field(default=10, ge=1, le=50)
    stop_loss_pct: float | None = Field(default=0.08, gt=0, le=0.5)
    take_profit_pct: float | None = Field(default=0.15, gt=0, le=2)
    max_holding_days: int = Field(default=21, ge=1, le=250)
    commission_bps: float = Field(default=3.0, ge=0, le=100)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    symbols: list[str] | None = None


class BacktestOut(ORMModel):
    id: int
    name: str
    start_date: date
    end_date: date
    initial_capital: float
    #: SYNTHETIC here means the run used test fixtures, not real market data.
    data_quality: str
    total_return: float | None
    cagr: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    max_drawdown: float | None
    win_rate: float | None
    profit_factor: float | None
    total_trades: int | None
    volatility: float | None
    benchmark_return: float | None
    completed_at: datetime | None
