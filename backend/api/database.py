"""
Database session setup.

Points at Supabase Postgres via DATABASE_URL. Keep this as the single
place that knows about connection details — nothing else in the app
should import psycopg or build a connection string directly.
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/marketmaven",
)

# pool_pre_ping avoids handing out dead connections after Supabase
# recycles idle ones — cheap insurance for a job that runs once a day.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    """FastAPI dependency — read-only endpoints don't need the
    commit/rollback behavior get_session() provides for ingestion
    jobs, just a session that's guaranteed to close."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def get_session():
    """Use as: `with get_session() as db: ...` — commits on success,
    rolls back and re-raises on error, always closes."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
