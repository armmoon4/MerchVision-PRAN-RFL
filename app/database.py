"""
app/database.py — SQLAlchemy engine, session factory, and Base.
Works with both SQLite (local dev) and PostgreSQL (Docker / production).
"""
from __future__ import annotations
import logging
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)


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


def init_db() -> None:
    """Create all DB tables and automatically migrate missing columns."""
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _auto_migrate()


def _auto_migrate() -> None:
    """Safely adds missing columns to existing tables (e.g. rack_uploads)."""
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        if "rack_uploads" not in tables:
            return

        existing_cols = {col["name"] for col in inspector.get_columns("rack_uploads")}
        is_postgres = engine.dialect.name == "postgresql"

        column_defs = [
            ("image_hash", "VARCHAR(64)"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
            ("estimated_cost_usd", "DOUBLE PRECISION" if is_postgres else "FLOAT"),
        ]

        with engine.begin() as conn:
            for col_name, col_type in column_defs:
                if col_name not in existing_cols:
                    logger.info("Adding missing column '%s' to 'rack_uploads' table...", col_name)
                    conn.execute(text(f"ALTER TABLE rack_uploads ADD COLUMN {col_name} {col_type}"))
                    logger.info("Column '%s' successfully added.", col_name)
    except Exception as exc:
        logger.warning("Auto-migration check encountered an error: %s", exc)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
