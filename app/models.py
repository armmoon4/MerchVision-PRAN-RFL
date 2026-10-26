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


class AuditStage(str, enum.Enum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    STANDALONE = "STANDALONE"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Outlet(Base):
    """Retail outlet / store master data."""

    __tablename__ = "outlets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outlet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    outlet_type: Mapped[str] = mapped_column(String(64), default="General Store")
    territory: Mapped[str] = mapped_column(String(128), default="Dhaka Central", index=True)
    distributor_name: Mapped[str] = mapped_column(String(255), default="PRAN Regional Distribution")
    tier: Mapped[str] = mapped_column(String(32), default="Tier 2 - Gold")
    target_pran_sos_pct: Mapped[float] = mapped_column(Float, default=45.0)
    expected_skus: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

    def __repr__(self) -> str:
        return f"<Outlet id={self.id} name={self.outlet_name}>"


class Visit(Base):
    """Field merchandiser visit tracking."""

    __tablename__ = "merchandiser_visits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    outlet_id: Mapped[str] = mapped_column(String(64), index=True)
    merchandiser_id: Mapped[str] = mapped_column(String(64), default="M-1001", index=True)
    merchandiser_name: Mapped[str] = mapped_column(String(128), default="Merchandiser", index=True)
    visit_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    status: Mapped[str] = mapped_column(String(32), default="IN_PROGRESS")  # IN_PROGRESS, COMPLETED, FLAGGED
    initial_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, index=True)

    def __repr__(self) -> str:
        return f"<Visit id={self.id} outlet={self.outlet_id} status={self.status}>"


class ShelfAudit(Base):
    """Individual shelf photo audit (Before, After, or Standalone) with full business insights."""

    __tablename__ = "shelf_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    visit_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    outlet_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    audit_stage: Mapped[str] = mapped_column(String(32), default=AuditStage.STANDALONE)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, default="")
    status: Mapped[str] = mapped_column(String(32), default="needs_attention")
    res_score: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)

    # Core shelf metrics
    pran_shelf_share_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    competitor_shelf_share_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    empty_shelf_share_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pran_facing_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    competitor_facing_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    arrangement_score: Mapped[float | None] = mapped_column(Float, nullable=True, default=70.0)
    missing_skus_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    visible_gaps_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    issues_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # Full structured payload
    full_analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Token and cost tracking
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, onupdate=_now_utc)

    def __repr__(self) -> str:
        return f"<ShelfAudit id={self.id} stage={self.audit_stage} score={self.res_score}>"


class AuditIssueRecord(Base):
    """Specific flagged merchandising issue for tracking and resolution."""

    __tablename__ = "audit_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    audit_id: Mapped[str] = mapped_column(String(36), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")  # HIGH, MEDIUM, LOW
    issue: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_resolved: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, index=True)


class RackUpload(Base):
    """
    Legacy/compat single table that stores every rack photo upload and its AI analysis result.

    Status lifecycle:
        PENDING → PROCESSING → COMPLETED
                             → FAILED
    """

    __tablename__ = "rack_uploads"

    # ── Primary key ───────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ── Storage / Reference ───────────────────────────────────────────────────
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, default="")
    image_key: Mapped[str | None] = mapped_column(String(1024), nullable=True, default="")

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

    def __repr__(self) -> str:
        return f"<RackUpload id={self.id} status={self.status}>"
