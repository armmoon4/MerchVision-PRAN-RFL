"""
app/schemas.py — Pydantic request/response models.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from app.models import ProcessingStatus


# ── Sub-models ────────────────────────────────────────────────────────────────


class DetectedProduct(BaseModel):
    """A single product identified on the rack shelf."""

    product_name: str
    quantity_visible: Optional[int] = None


# ── Response models ───────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"


class UploadResponse(BaseModel):
    """Returned immediately (202) after a successful upload."""

    upload_id: str
    status: ProcessingStatus
    message: str


class UploadResultResponse(BaseModel):
    """Full result returned by GET /uploads/{upload_id}/result and GET /uploads/{upload_id}."""

    model_config = ConfigDict(from_attributes=True)

    upload_id: str
    status: ProcessingStatus
    shop_id: Optional[str] = None
    merchandiser_id: Optional[str] = None
    image_url: str
    detected_products: Optional[List[DetectedProduct]] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class UploadListResponse(BaseModel):
    """Paginated list of rack upload analysis results."""

    total: int
    limit: int
    offset: int
    items: List[UploadResultResponse]


class TopProductItem(BaseModel):
    """Aggregated detection metrics for a specific product."""

    product_name: str
    total_quantity: int
    scan_appearances: int


class AnalysisSummaryResponse(BaseModel):
    """Overall analytics and summary of rack recognition results."""

    total_scans: int
    completed_scans: int
    processing_scans: int
    pending_scans: int
    failed_scans: int
    total_products_detected: int
    unique_products_count: int
    top_products: List[TopProductItem]
    recent_uploads: List[UploadResultResponse]


class DeleteResponse(BaseModel):
    """Response returned when an upload is deleted."""

    upload_id: str
    message: str

