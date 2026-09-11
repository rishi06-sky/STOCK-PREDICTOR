"""Shared test fixtures.

Every test runs with ENVIRONMENT=test against a dedicated database, so a test
run can never touch development data. The environment is set before any
application module is imported, because settings are read at import time.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("MARKET_DATA_PROVIDERS", "fixture")
os.environ.setdefault("NOTIFICATIONS_ENABLED", "false")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-characters-long")

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.database.seed import seed_reference_data  # noqa: E402
from app.database.session import SessionLocal, engine  # noqa: E402
from app.models import *  # noqa: E402,F401,F403


def _database_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _database_available()
requires_db = pytest.mark.skipif(
    not DB_AVAILABLE, reason="PostgreSQL test database is not reachable"
)


@pytest.fixture(scope="session", autouse=True)
def _guard_environment():
    """Fail loudly rather than run destructive fixtures against a real database."""
    assert settings.environment == "test", "tests must run with ENVIRONMENT=test"
    assert "test" in settings.database_url, (
        f"refusing to run against {settings.database_url!r}: the URL must name a test database"
    )
    yield


@pytest.fixture(scope="session")
def db_schema(_guard_environment):
    if not DB_AVAILABLE:
        pytest.skip("no test database")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(db_schema):
    """A session whose writes are rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def seeded_db(db):
    seed_reference_data(db)
    return db


# ------------------------------------------------------------------ data fixtures
@pytest.fixture
def ohlcv() -> pd.DataFrame:
    """A deterministic OHLCV frame with realistic geometry."""
    rng = np.random.default_rng(20260911)
    n = 400
    index = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(250 * np.exp(np.cumsum(rng.normal(0.0003, 0.013, n))), index=index)
    return pd.DataFrame(
        {
            "open": close.shift(1).bfill() * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + np.abs(rng.normal(0, 0.007, n))),
            "low": close * (1 - np.abs(rng.normal(0, 0.007, n))),
            "close": close,
            "volume": rng.integers(200_000, 2_000_000, n).astype(float),
        },
        index=index,
    )


@pytest.fixture
def random_walk_panel():
    """Several independent random walks: the future cannot be predicted here."""
    rng = np.random.default_rng(7)
    panel = {}
    for i in range(6):
        n = 900
        index = pd.bdate_range("2021-01-01", periods=n)
        close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, n))), index=index)
        panel[i] = pd.DataFrame(
            {
                "open": close.shift(1).bfill(),
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": rng.integers(1e5, 1e6, n).astype(float),
            },
            index=index,
        )
    return panel


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(timezone.utc)
