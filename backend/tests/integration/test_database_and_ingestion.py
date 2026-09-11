"""Database constraints, ingestion idempotency and provider failover."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AllProvidersFailed, ProviderUnavailable
from app.market_data.ingest import IngestionService
from app.market_data.registry import ProviderChain
from app.market_data.types import Bar
from app.models.enums import DataQuality
from app.models.market import PriceData, Quote, Security
from app.models.platform import DataFreshness, SystemEvent
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def _security(db) -> Security:
    return db.scalars(select(Security).where(Security.symbol == "RELIANCE").limit(1)).first()


class TestSchemaConstraints:
    def test_ohlc_integrity_is_enforced_by_the_database(self, seeded_db):
        security = _security(seeded_db)
        savepoint = seeded_db.begin_nested()
        seeded_db.add(
            PriceData(
                security_id=security.id, trade_date=date(2026, 1, 5),
                open=100, high=90, low=95, close=98, volume=1,   # high < low
                provider="t", quality=DataQuality.EOD,
                source_timestamp=datetime.now(timezone.utc),
                ingested_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            seeded_db.flush()
        savepoint.rollback()

    def test_negative_prices_are_rejected(self, seeded_db):
        security = _security(seeded_db)
        savepoint = seeded_db.begin_nested()
        seeded_db.add(
            PriceData(
                security_id=security.id, trade_date=date(2026, 1, 6),
                open=-1, high=10, low=-5, close=5, volume=1,
                provider="t", quality=DataQuality.EOD,
                source_timestamp=datetime.now(timezone.utc),
                ingested_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            seeded_db.flush()
        savepoint.rollback()

    def test_one_bar_per_security_per_session(self, seeded_db):
        security = _security(seeded_db)
        savepoint = seeded_db.begin_nested()
        for _ in range(2):
            seeded_db.add(
                PriceData(
                    security_id=security.id, trade_date=date(2026, 1, 7),
                    open=100, high=105, low=99, close=104, volume=1,
                    provider="t", quality=DataQuality.EOD,
                    source_timestamp=datetime.now(timezone.utc),
                    ingested_at=datetime.now(timezone.utc),
                )
            )
        with pytest.raises(IntegrityError):
            seeded_db.flush()
        savepoint.rollback()

    def test_seed_is_idempotent(self, seeded_db):
        from app.database.seed import seed_reference_data

        before = seeded_db.scalar(select(func.count()).select_from(Security))
        seed_reference_data(seeded_db)
        assert seeded_db.scalar(select(func.count()).select_from(Security)) == before


class TestIngestion:
    def test_daily_ingestion_writes_bars(self, seeded_db):
        security = _security(seeded_db)
        report = IngestionService(seeded_db).ingest_daily(
            security, date(2025, 1, 1), date(2026, 1, 1), commit=False
        )
        assert report.ok
        assert report.written > 150
        stored = seeded_db.scalar(
            select(func.count()).select_from(PriceData).where(
                PriceData.security_id == security.id
            )
        )
        assert stored == report.written

    def test_reingestion_is_idempotent(self, seeded_db):
        security = _security(seeded_db)
        service = IngestionService(seeded_db)
        service.ingest_daily(security, date(2025, 1, 1), date(2025, 6, 1), commit=False)
        first = seeded_db.scalar(select(func.count()).select_from(PriceData))
        service.ingest_daily(security, date(2025, 1, 1), date(2025, 6, 1), commit=False)
        assert seeded_db.scalar(select(func.count()).select_from(PriceData)) == first

    def test_quote_ingestion_records_freshness(self, seeded_db):
        security = _security(seeded_db)
        report = IngestionService(seeded_db).ingest_quote(security, commit=False)
        assert report.ok
        quote = seeded_db.get(Quote, security.id)
        assert quote is not None and float(quote.price) > 0
        freshness = seeded_db.scalars(
            select(DataFreshness).where(DataFreshness.dataset == "quotes")
        ).first()
        assert freshness is not None and freshness.last_success_at is not None

    def test_synthetic_data_is_labelled_as_such(self, seeded_db):
        security = _security(seeded_db)
        IngestionService(seeded_db).ingest_daily(
            security, date(2025, 6, 1), date(2025, 9, 1), commit=False
        )
        qualities = set(
            seeded_db.scalars(
                select(PriceData.quality).where(PriceData.security_id == security.id)
            )
        )
        assert qualities == {DataQuality.SYNTHETIC}


class TestProviderFailure:
    """When providers fail, nothing is invented."""

    def test_total_failure_writes_no_data_and_records_the_error(self, seeded_db):
        class BrokenProvider:
            name = "broken"
            requires_key = False
            default_quality = DataQuality.EOD
            capabilities = {"daily", "quote"}

            def is_configured(self):
                return True

            def fetch_daily_bars(self, symbol, start, end):
                raise ProviderUnavailable("broken", "simulated outage")

            def fetch_quote(self, symbol):
                raise ProviderUnavailable("broken", "simulated outage")

            def close(self):
                pass

        security = _security(seeded_db)
        service = IngestionService(seeded_db, chain=ProviderChain([BrokenProvider()]))
        report = service.ingest_daily(security, date(2026, 1, 1), date(2026, 2, 1), commit=False)

        assert not report.ok
        assert report.written == 0
        assert seeded_db.scalar(select(func.count()).select_from(PriceData)) == 0

        # autoflush is off on this session, so make the pending rows visible.
        seeded_db.flush()
        event = seeded_db.scalars(
            select(SystemEvent).where(SystemEvent.event_type == "daily_failed")
        ).first()
        assert event is not None
        freshness = seeded_db.scalars(
            select(DataFreshness).where(DataFreshness.provider == "broken")
        ).first()
        assert freshness.consecutive_failures >= 1

    def test_chain_falls_through_to_a_working_provider(self, seeded_db):
        class Failing:
            name = "failing"
            requires_key = False
            capabilities = {"daily"}

            def is_configured(self):
                return True

            def fetch_daily_bars(self, symbol, start, end):
                raise ProviderUnavailable("failing", "down")

            def close(self):
                pass

        class Working:
            name = "working"
            requires_key = False
            capabilities = {"daily"}

            def is_configured(self):
                return True

            def fetch_daily_bars(self, symbol, start, end):
                return [
                    Bar(
                        symbol=symbol, trade_date=date(2026, 1, 5),
                        open=100, high=105, low=99, close=104, volume=1000,
                        provider="working", quality=DataQuality.EOD,
                        source_timestamp=datetime.now(timezone.utc),
                    )
                ]

            def close(self):
                pass

        security = _security(seeded_db)
        service = IngestionService(seeded_db, chain=ProviderChain([Failing(), Working()]))
        report = service.ingest_daily(security, date(2026, 1, 1), date(2026, 1, 10), commit=False)
        assert report.ok
        assert report.provider == "working"
        assert report.written == 1
