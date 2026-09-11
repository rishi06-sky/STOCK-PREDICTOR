"""Engine and session management.

Synchronous SQLAlchemy throughout. FastAPI runs `def` endpoints in a worker
threadpool, so blocking DB calls never stall the event loop, and we avoid the
async/sync bridging that pandas and scikit-learn would otherwise force on us.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

engine: Engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=settings.db_pool_pre_ping,
    pool_recycle=1800,
    future=True,
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, class_=Session
)


@event.listens_for(engine, "connect")
def _set_session_defaults(dbapi_conn, _record):  # pragma: no cover - driver hook
    with dbapi_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency. Commit is the endpoint's responsibility."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.error("database_healthcheck_failed", error=str(exc))
        return False
