"""Bridges the Kite tick stream into the platform.

Responsibilities:

* resolve the tradeable universe to Kite instrument tokens, once
* subscribe the stream to those instruments
* persist ticks into `quotes`, throttled per security
* broadcast ticks to connected dashboards

Ticks arrive several times a second per instrument. Writing each one would
swamp PostgreSQL for no analytical benefit -- the signal engine reads the
latest quote, not a tick history -- so writes are throttled per security while
the in-memory latest price stays exact.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.market_data.providers.kite import KiteAuthError, KiteConnectProvider
from app.market_data.providers.kite_ticker import KiteTickerStream
from app.market_data.types import QuoteData
from app.models.enums import AssetType, EventSeverity
from app.models.market import Exchange, Quote, Security
from app.models.platform import DataFreshness, SystemEvent

log = get_logger(__name__)


class TickService:
    """Owns the lifetime of the Kite stream inside the worker process."""

    def __init__(self, session_factory, *, stream: KiteTickerStream | None = None,
                 rest: KiteConnectProvider | None = None):
        self._session_factory = session_factory
        self.rest = rest or KiteConnectProvider()
        self.stream = stream or KiteTickerStream()
        self._latest: dict[str, QuoteData] = {}
        self._last_persist: dict[str, float] = {}
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------- mapping
    def build_instrument_map(self, db: Session) -> dict[int, str]:
        """Map Kite instrument tokens onto this platform's securities.

        Only Indian equities are streamed: Kite covers NSE, BSE and the
        derivative segments, not US listings.
        """
        securities = db.execute(
            select(Security, Exchange.code)
            .join(Exchange, Exchange.id == Security.exchange_id)
            .where(
                Security.is_active.is_(True),
                Security.asset_type == AssetType.EQUITY,
                Exchange.code.in_(["NSE", "BSE"]),
            )
        ).all()

        mapping: dict[int, str] = {}
        unresolved: list[str] = []
        for security, exchange_code in securities:
            try:
                token = self.rest.instrument_token(security.symbol, exchange_code)
            except Exception:
                unresolved.append(f"{exchange_code}:{security.symbol}")
                continue
            mapping[token] = security.symbol

        if unresolved:
            log.warning(
                "kite_instruments_unresolved",
                count=len(unresolved), sample=unresolved[:10],
            )
        log.info("kite_instrument_map_built", resolved=len(mapping))
        return mapping

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start streaming. Returns False (without raising) when unavailable."""
        if self._started:
            return True
        if not settings.kite_streaming_enabled:
            log.info("kite_streaming_disabled")
            return False
        if not self.stream.is_configured():
            log.warning(
                "kite_streaming_unconfigured",
                hint="set KITE_API_KEY and KITE_ACCESS_TOKEN",
            )
            return False

        try:
            with self._session_factory() as db:
                mapping = self.build_instrument_map(db)
        except KiteAuthError as exc:
            self._record_event(
                "kite_auth_failed", str(exc), EventSeverity.ERROR
            )
            return False
        except Exception as exc:
            log.error("kite_instrument_map_failed", error=str(exc))
            return False

        if not mapping:
            log.warning("kite_no_instruments_resolved")
            return False

        self.stream.register_instruments(mapping)
        self.stream.add_handler(self._on_tick)
        self.stream.start()
        self.stream.subscribe_tokens(list(mapping))
        self._started = True

        self._record_event(
            "kite_stream_started",
            f"streaming {len(mapping)} instruments in {self.stream.mode} mode",
            EventSeverity.INFO,
        )
        return True

    def stop(self) -> None:
        if not self._started:
            return
        self.stream.stop()
        self._started = False
        self._record_event("kite_stream_stopped", "tick stream stopped", EventSeverity.INFO)

    # ------------------------------------------------------------ tick path
    def _on_tick(self, quote: QuoteData) -> None:
        """Runs on the stream's reader thread. Must stay cheap."""
        with self._lock:
            self._latest[quote.symbol] = quote
            last = self._last_persist.get(quote.symbol, 0.0)
            now = time.monotonic()
            due = (now - last) >= settings.kite_tick_persist_interval_seconds
            if due:
                self._last_persist[quote.symbol] = now

        if due:
            self._persist(quote)
            self._broadcast(quote)

    def latest(self, symbol: str) -> QuoteData | None:
        """Most recent tick held in memory, unthrottled."""
        with self._lock:
            return self._latest.get(symbol)

    def _persist(self, quote: QuoteData) -> None:
        try:
            with self._session_factory() as db:
                security_id = db.scalar(
                    select(Security.id).where(Security.symbol == quote.symbol).limit(1)
                )
                if security_id is None:
                    return
                now = datetime.now(timezone.utc)
                stmt = pg_insert(Quote).values(
                    security_id=security_id,
                    price=quote.price,
                    previous_close=quote.previous_close,
                    change=quote.change,
                    change_pct=quote.change_pct,
                    day_open=quote.day_open,
                    day_high=quote.day_high,
                    day_low=quote.day_low,
                    volume=quote.volume,
                    provider=quote.provider,
                    quality=quote.quality,
                    source_timestamp=quote.source_timestamp,
                    ingested_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Quote.security_id],
                    set_={
                        c: getattr(stmt.excluded, c)
                        for c in (
                            "price", "previous_close", "change", "change_pct",
                            "day_open", "day_high", "day_low", "volume",
                            "provider", "quality", "source_timestamp", "ingested_at",
                        )
                    },
                )
                db.execute(stmt)

                freshness = pg_insert(DataFreshness).values(
                    provider=quote.provider, dataset="ticks",
                    last_success_at=now, consecutive_failures=0,
                    records_ingested=1, updated_at=now,
                )
                freshness = freshness.on_conflict_do_update(
                    constraint="uq_data_freshness_provider_dataset",
                    set_={
                        "last_success_at": now,
                        "consecutive_failures": 0,
                        "records_ingested": DataFreshness.records_ingested + 1,
                        "updated_at": now,
                    },
                )
                db.execute(freshness)
                db.commit()
        except Exception as exc:
            log.warning("kite_tick_persist_failed", symbol=quote.symbol, error=str(exc)[:200])

    def _broadcast(self, quote: QuoteData) -> None:
        try:
            from app.api.websocket import broadcast_sync

            broadcast_sync(
                {
                    "type": "quote",
                    "symbol": quote.symbol,
                    "price": float(quote.price),
                    "change_pct": quote.change_pct,
                    "quality": str(quote.quality),
                    "source_timestamp": quote.source_timestamp.isoformat(),
                }
            )
        except Exception as exc:
            log.debug("kite_tick_broadcast_failed", error=str(exc)[:120])

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "enabled": settings.kite_streaming_enabled,
            "configured": self.stream.is_configured(),
            "started": self._started,
            "mode": self.stream.mode,
            "symbols_in_memory": len(self._latest),
            **self.stream.stats.as_dict(),
        }

    def _record_event(self, event_type: str, message: str, severity) -> None:
        try:
            with self._session_factory() as db:
                db.add(
                    SystemEvent(
                        component="kite_stream", event_type=event_type,
                        severity=severity, message=message[:2000],
                        created_at=datetime.now(timezone.utc),
                    )
                )
                db.commit()
        except Exception as exc:
            log.warning("kite_event_record_failed", error=str(exc)[:150])


_service: TickService | None = None
_service_lock = threading.Lock()


def get_tick_service() -> TickService:
    """Process-wide tick service. Only the worker should start it."""
    global _service
    with _service_lock:
        if _service is None:
            from app.database.session import session_scope

            _service = TickService(session_scope)
        return _service


def reset_tick_service() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.stop()
        _service = None
