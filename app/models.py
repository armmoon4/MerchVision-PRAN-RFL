"""
app/models.py — ORM models for the PRAN-RFL rack recognition system.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base


class ProcessingStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RackUpload(Base):
    """
    Single table that stores every rack photo upload and its AI analysis result.

    Status lifecycle:
        PENDING → PROCESSING → COMPLETED
                             → FAILED
    """

    __tablename__ = "rack_uploads"

    # ── Primary key ───────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ── Client-supplied identifiers ───────────────────────────────────────────
    shop_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    merchandiser_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )

    # ── Storage ───────────────────────────────────────────────────────────────
    image_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    image_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    # ── Processing state ──────────────────────────────────────────────────────
    status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, name="processing_status"),
        nullable=False,
        default=ProcessingStatus.PENDING,
        index=True,
    )

    # ── AI output ─────────────────────────────────────────────────────────────
    ai_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Array of {product_name, quantity_visible} dicts stored as JSON
    detected_products: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Token metrics & cost ──────────────────────────────────────────────────
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
        onupdate=_now_utc,
    )

    # ── Composite indexes (useful for future dashboard queries) ───────────────
    __table_args__ = (
        Index("ix_rack_uploads_shop_status", "shop_id", "status"),
        Index("ix_rack_uploads_merchandiser_status", "merchandiser_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<RackUpload id={self.id} status={self.status}>"
