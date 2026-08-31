"""
app/database.py — SQLAlchemy engine, session factory, and Base.
Works with both SQLite (local dev) and PostgreSQL (Docker / production).
"""
from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


def _build_engine():
    settings = get_settings()
    url = settings.database_url

    connect_args = {}
    # SQLite requires check_same_thread=False when used with FastAPI's
    # thread-pool-based background tasks.
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    return create_engine(url, connect_args=connect_args)


engine = _build_engine()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
