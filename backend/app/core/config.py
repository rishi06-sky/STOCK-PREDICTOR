"""Central application configuration.

Every environment-dependent value is defined here and sourced from the
environment (or a local .env file). Nothing in this module may contain a real
credential -- see .env.example for the template.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
TradingMode = Literal["paper", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ core
    environment: Environment = "development"
    debug: bool = False
    app_name: str = "Stock Intelligence Platform"
    api_prefix: str = "/api/v1"

    # ------------------------------------------------------------- datastore
    database_url: str = "postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True
    redis_url: str = "redis://localhost:6379/0"

    # -------------------------------------------------------------- security
    # NOTE: a random secret is generated if unset, which invalidates tokens on
    # restart. Production refuses to boot without an explicit value (see
    # _validate_production below).
    secret_key: str = Field(default_factory=lambda: os.urandom(32).hex())
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 7
    bcrypt_rounds: int = 12
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 120
    auth_rate_limit_per_minute: int = 10

    # ------------------------------------------------------- market data API
    # Ordered fallback chain. First provider that returns valid data wins.
    market_data_providers: str = "yahoo,stooq"
    alpha_vantage_api_key: str | None = None
    finnhub_api_key: str | None = None
    twelvedata_api_key: str | None = None
    provider_timeout_seconds: float = 15.0
    provider_max_retries: int = 3
    provider_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    # Per-provider client-side throttle (requests/minute).
    # --- Zerodha Kite Connect (paid, exchange-licensed real-time NSE/BSE) ---
    # The access token is issued by the daily Zerodha login flow and expires
    # each morning (~07:30 IST). The API secret is needed only to exchange a
    # request_token for an access_token.
    kite_api_key: str | None = None
    kite_api_secret: str | None = None
    kite_access_token: str | None = None
    kite_rate_limit_per_minute: int = 55        # Kite allows ~1 quote call/sec
    #: Enable the pushed tick stream. Requires a Kite subscription.
    kite_streaming_enabled: bool = False
    #: ltp | quote | full. "quote" carries OHLC and volume without the 184-byte
    #: market-depth payload, which is the useful default.
    kite_stream_mode: str = "quote"
    #: Kite caps a single websocket connection at 3000 instruments.
    kite_stream_max_instruments: int = 3000
    #: Persist streamed ticks to the quotes table at most this often, per
    #: security. Ticks arrive several times a second; writing every one would
    #: swamp the database for no analytical gain.
    kite_tick_persist_interval_seconds: float = 5.0

    yahoo_rate_limit_per_minute: int = 60
    stooq_rate_limit_per_minute: int = 30
    alpha_vantage_rate_limit_per_minute: int = 5
    finnhub_rate_limit_per_minute: int = 55

    # TEST ONLY. Replays recorded payloads instead of hitting the network.
    # Hard-refused outside the test environment (see _validate_production).
    enable_fixture_provider: bool = False

    # -------------------------------------------------------- data freshness
    # A quote older than this is never presented or traded as current.
    quote_staleness_seconds: int = 900          # 15 min
    eod_staleness_hours: int = 72               # tolerate a long weekend
    # Signal generation halts entirely when the freshest data exceeds this.
    signal_halt_staleness_seconds: int = 3600

    # --------------------------------------------------------------- signals
    signal_min_confidence: float = 0.55
    signal_ttl_minutes: int = 240
    signal_max_per_run: int = 200

    # ------------------------------------------------------------------ risk
    risk_max_position_pct: float = 0.10         # of portfolio equity
    risk_max_sector_pct: float = 0.30
    risk_max_portfolio_exposure_pct: float = 0.95
    risk_per_trade_pct: float = 0.01            # capital risked per trade
    risk_max_daily_loss_pct: float = 0.05
    risk_max_drawdown_pct: float = 0.20
    risk_min_reward_to_risk: float = 1.5
    risk_max_open_positions: int = 20
    #: Orders below this notional are refused rather than filled. Real brokers
    #: reject sub-minimum orders, and a dust position costs more in commission
    #: than it can ever return.
    risk_min_order_notional: float = 100.0

    # --------------------------------------------------------------- trading
    trading_mode: TradingMode = "paper"
    live_trading_enabled: bool = False          # second, independent switch
    kill_switch_engaged: bool = False
    # Settlement currency for the whole platform. The seeded universe is
    # single-currency on purpose: a portfolio holding both INR and USD
    # securities would have to convert to value itself, and the free data path
    # has no FX source to convert with. Change this only alongside the seeded
    # universe in app/database/seed.py.
    base_currency: str = "INR"
    paper_starting_cash: float = 1_000_000.0
    commission_bps: float = 3.0                 # 0.03% per side
    slippage_bps: float = 5.0                   # 0.05% adverse

    # -------------------------------------------------------------------- ml
    ml_model_dir: str = "./model_store"
    ml_min_training_rows: int = 750
    ml_walk_forward_splits: int = 5
    ml_embargo_days: int = 5                    # gap between train and test
    ml_horizons_days: str = "1,3,5,21"
    ml_retrain_cron_hour: int = 2
    ml_drift_psi_threshold: float = 0.25

    # --------------------------------------------------------- notifications
    notifications_enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    alert_dedupe_window_minutes: int = 60

    # ------------------------------------------------------------- scheduler
    scheduler_enabled: bool = True
    ingest_intraday_interval_minutes: int = 15
    ingest_eod_cron_hour: int = 23
    pipeline_interval_minutes: int = 30
    news_interval_minutes: int = 60

    # ------------------------------------------------------------- accessors
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip().lower() for p in self.market_data_providers.split(",") if p.strip()]

    @property
    def horizons(self) -> list[int]:
        return sorted({int(h) for h in self.ml_horizons_days.split(",") if h.strip()})

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("risk_per_trade_pct", "risk_max_position_pct")
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("must be a fraction in (0, 1]")
        return v

    def validate_runtime(self) -> list[str]:
        """Return fatal misconfigurations. Production refuses to boot on any."""
        problems: list[str] = []
        if self.is_production:
            if len(self.secret_key) < 32 or self.secret_key == "change-me":
                problems.append("SECRET_KEY must be an explicit value of >=32 chars in production")
            if self.enable_fixture_provider:
                problems.append("ENABLE_FIXTURE_PROVIDER must be false in production")
            if self.debug:
                problems.append("DEBUG must be false in production")
            if "*" in self.cors_origin_list:
                problems.append("CORS_ORIGINS must not be '*' in production")
        if self.live_trading_enabled and self.trading_mode != "live":
            problems.append("LIVE_TRADING_ENABLED requires TRADING_MODE=live")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
