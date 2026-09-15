"""Ingestion: fetch -> validate -> upsert -> record freshness.

Writes use PostgreSQL upserts keyed on the natural business key, so re-running
an ingestion is idempotent and a provider restating a bar corrects the stored
row instead of duplicating it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AllProvidersFailed
from app.core.logging import get_logger
from app.market_data.registry import ProviderChain, get_chain
from app.market_data.types import Bar, IntradayBar, QuoteData
from app.market_data.validation import (
    detect_gaps, validate_bars, validate_intraday_bar, validate_quote,
)
from app.models.enums import DataQuality, EventSeverity
from app.models.market import IntradayData, PriceData, Quote, Security
from app.models.platform import DataFreshness, SystemEvent

log = get_logger(__name__)


@dataclass(slots=True)
class IngestReport:
    symbol: str
    dataset: str
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    provider: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "dataset": self.dataset, "provider": self.provider,
            "written": self.written, "rejected": self.rejected,
            "errors": self.errors, "warnings": self.warnings, "ok": self.ok,
        }


class IngestionService:
    def __init__(self, db: Session, chain: ProviderChain | None = None):
        self.db = db
        self.chain = chain or get_chain()

    # ---------------------------------------------------------------- daily
    def ingest_daily(
        self, security: Security, start: date, end: date, *, commit: bool = True
    ) -> IngestReport:
        symbol = security.symbol
        report = IngestReport(symbol=symbol, dataset="daily")
        provider_symbol = None

        try:
            # Ask each provider using its own symbol convention.
            bars, provider_name = self._fetch_daily_with_symbols(security, start, end)
            report.provider = provider_name
        except AllProvidersFailed as exc:
            report.errors.append(str(exc))
            self._record_failure("daily", str(exc), providers=list(exc.errors))
            if commit:
                self.db.commit()
            return report

        previous_close = self._last_close_before(security.id, start)
        result = validate_bars(bars, previous_close=previous_close)
        report.rejected = result.rejected_count
        report.warnings.extend(result.warnings)
        for bar, reason in result.rejected:
            log.warning(
                "bar_rejected", symbol=symbol, trade_date=str(bar.trade_date), reason=reason
            )
        for gap_start, gap_end, days in detect_gaps(result.valid):
            report.warnings.append(f"data gap {gap_start}..{gap_end} ({days}d)")

        if result.valid:
            written = self._upsert_daily(security.id, result.valid)
            report.inserted = written
        self._record_success("daily", report.provider, report.written)

        if commit:
            self.db.commit()
        return report

    def _fetch_daily_with_symbols(
        self, security: Security, start: date, end: date
    ) -> tuple[list[Bar], str]:
        """Try each provider with the symbol spelling that provider expects."""
        errors: dict[str, str] = {}
        for provider in self.chain.providers:
            if "daily" not in provider.capabilities or not provider.is_configured():
                continue
            psym = security.provider_symbol(provider.name)
            if not provider.supports_symbol(psym):
                errors[provider.name] = f"does not cover {psym}"
                continue
            try:
                bars = provider.fetch_daily_bars(psym, start, end)
            except Exception as exc:
                errors[provider.name] = str(exc)
                continue
            if bars:
                return bars, provider.name
            errors[provider.name] = "returned no bars"
        raise AllProvidersFailed(security.symbol, errors)

    def _upsert_daily(self, security_id: int, bars: list[Bar]) -> int:
        now = datetime.now(timezone.utc)
        rows = [
            {
                "security_id": security_id,
                "trade_date": b.trade_date,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "adj_close": b.adj_close, "volume": int(b.volume or 0),
                "provider": b.provider,
                "quality": b.quality,
                "source_timestamp": b.source_timestamp or now,
                "ingested_at": now,
            }
            for b in bars
        ]
        stmt = pg_insert(PriceData).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_price_data_sec_date",
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high,
                "low": stmt.excluded.low, "close": stmt.excluded.close,
                "adj_close": stmt.excluded.adj_close, "volume": stmt.excluded.volume,
                "provider": stmt.excluded.provider, "quality": stmt.excluded.quality,
                "source_timestamp": stmt.excluded.source_timestamp,
                "ingested_at": stmt.excluded.ingested_at,
            },
        )
        self.db.execute(stmt)
        return len(rows)

    def _last_close_before(self, security_id: int, day: date):
        return self.db.scalar(
            select(PriceData.close)
            .where(PriceData.security_id == security_id, PriceData.trade_date < day)
            .order_by(PriceData.trade_date.desc())
            .limit(1)
        )

    # ---------------------------------------------------------------- quotes
    def ingest_quote(self, security: Security, *, commit: bool = True) -> IngestReport:
        report = IngestReport(symbol=security.symbol, dataset="quotes")
        try:
            quote, provider_name = self._fetch_quote_with_symbols(security)
            report.provider = provider_name
        except AllProvidersFailed as exc:
            report.errors.append(str(exc))
            self._record_failure("quotes", str(exc), providers=list(exc.errors))
            if commit:
                self.db.commit()
            return report

        reason = validate_quote(quote, max_age_seconds=settings.quote_staleness_seconds)
        if reason:
            # A stale quote is stored -- the freshness view needs to see it --
            # but it is never promoted to a tradeable price by the guards that
            # read `source_timestamp`.
            report.warnings.append(reason)
            log.warning("quote_quality_warning", symbol=security.symbol, reason=reason)
            if "non-positive" in reason or "future" in reason:
                report.errors.append(reason)
                report.rejected = 1
                if commit:
                    self.db.commit()
                return report

        self._upsert_quote(security.id, quote)
        report.inserted = 1
        self._record_success("quotes", report.provider, 1)
        if commit:
            self.db.commit()
        return report

    def _fetch_quote_with_symbols(self, security: Security) -> tuple[QuoteData, str]:
        errors: dict[str, str] = {}
        for provider in self.chain.providers:
            if "quote" not in provider.capabilities or not provider.is_configured():
                continue
            psym = security.provider_symbol(provider.name)
            if not provider.supports_symbol(psym):
                errors[provider.name] = f"does not cover {psym}"
                continue
            try:
                quote = provider.fetch_quote(psym)
            except Exception as exc:
                errors[provider.name] = str(exc)
                continue
            if quote:
                return quote, provider.name
        raise AllProvidersFailed(security.symbol, errors)

    def _upsert_quote(self, security_id: int, quote: QuoteData) -> None:
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
                    "price", "previous_close", "change", "change_pct", "day_open",
                    "day_high", "day_low", "volume", "provider", "quality",
                    "source_timestamp", "ingested_at",
                )
            },
        )
        self.db.execute(stmt)

    # -------------------------------------------------------------- intraday
    def ingest_intraday(
        self, security: Security, interval: str = "5m", lookback_days: int = 1,
        *, commit: bool = True,
    ) -> IngestReport:
        report = IngestReport(symbol=security.symbol, dataset="intraday")
        errors: dict[str, str] = {}
        bars: list[IntradayBar] = []
        for provider in self.chain.providers:
            if "intraday" not in provider.capabilities or not provider.is_configured():
                continue
            psym = security.provider_symbol(provider.name)
            if not provider.supports_symbol(psym):
                errors[provider.name] = f"does not cover {psym}"
                continue
            try:
                bars = provider.fetch_intraday_bars(psym, interval, lookback_days)
            except Exception as exc:
                errors[provider.name] = str(exc)
                continue
            if bars:
                report.provider = provider.name
                break

        if not bars:
            message = str(AllProvidersFailed(security.symbol, errors))
            report.errors.append(message)
            self._record_failure("intraday", message, providers=list(errors))
            if commit:
                self.db.commit()
            return report

        good: list[IntradayBar] = []
        for bar in bars:
            reason = validate_intraday_bar(bar)
            if reason:
                report.rejected += 1
            else:
                good.append(bar)

        if good:
            now = datetime.now(timezone.utc)
            rows = [
                {
                    "security_id": security.id, "bar_timestamp": b.bar_timestamp,
                    "interval": b.interval, "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close, "volume": int(b.volume or 0),
                    "provider": b.provider, "quality": b.quality, "ingested_at": now,
                }
                for b in good
            ]
            stmt = pg_insert(IntradayData).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_intraday_data_sec_ts_interval",
                set_={
                    "open": stmt.excluded.open, "high": stmt.excluded.high,
                    "low": stmt.excluded.low, "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume, "ingested_at": stmt.excluded.ingested_at,
                },
            )
            self.db.execute(stmt)
            report.inserted = len(rows)
            self._record_success("intraday", report.provider, len(rows))

        if commit:
            self.db.commit()
        return report

    # ------------------------------------------------------------- freshness
    def _record_success(self, dataset: str, provider: str | None, count: int) -> None:
        now = datetime.now(timezone.utc)
        stmt = pg_insert(DataFreshness).values(
            provider=provider or "unknown", dataset=dataset,
            last_success_at=now, consecutive_failures=0,
            records_ingested=count, updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_data_freshness_provider_dataset",
            set_={
                "last_success_at": now,
                "consecutive_failures": 0,
                "records_ingested": DataFreshness.records_ingested + count,
                "updated_at": now,
            },
        )
        self.db.execute(stmt)

    def _record_failure(self, dataset: str, error: str, *, providers: list[str]) -> None:
        now = datetime.now(timezone.utc)
        for provider in providers or ["unknown"]:
            stmt = pg_insert(DataFreshness).values(
                provider=provider, dataset=dataset, last_failure_at=now,
                last_error=error[:1000], consecutive_failures=1, updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_data_freshness_provider_dataset",
                set_={
                    "last_failure_at": now,
                    "last_error": error[:1000],
                    "consecutive_failures": DataFreshness.consecutive_failures + 1,
                    "updated_at": now,
                },
            )
            self.db.execute(stmt)
        self.db.add(
            SystemEvent(
                component="ingestion", event_type=f"{dataset}_failed",
                severity=EventSeverity.ERROR, message=error[:2000],
                details={"dataset": dataset, "providers": providers}, created_at=now,
            )
        )

    # ----------------------------------------------------------------- batch
    def ingest_universe_daily(
        self, securities: list[Security], start: date, end: date
    ) -> list[IngestReport]:
        reports = []
        for security in securities:
            try:
                reports.append(self.ingest_daily(security, start, end, commit=False))
            except Exception as exc:  # one bad symbol must not sink the batch
                log.error("ingest_symbol_failed", symbol=security.symbol, error=str(exc))
                reports.append(
                    IngestReport(symbol=security.symbol, dataset="daily", errors=[str(exc)])
                )
                self.db.rollback()
        self.db.commit()
        return reports
