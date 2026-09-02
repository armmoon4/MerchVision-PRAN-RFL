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


class TokenUsage(BaseModel):
    """Token metrics and estimated USD cost for an image AI analysis call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


# ── Request models ────────────────────────────────────────────────────────────


class UploadUrlRequest(BaseModel):
    """Request payload for initiating rack analysis from an image URL."""

    image_url: str
    shop_id: Optional[str] = None
    merchandiser_id: Optional[str] = None


# ── Response models ───────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"


class UploadResponse(BaseModel):
    """Returned immediately (202) after a successful async upload."""

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
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    token_usage: Optional[TokenUsage] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DirectAnalyzeResponse(BaseModel):
    """Immediate synchronous result from the unified single-call /analyze API."""

    model_config = ConfigDict(from_attributes=True)

    upload_id: str
    status: ProcessingStatus
    shop_id: Optional[str] = None
    merchandiser_id: Optional[str] = None
    image_url: str
    detected_products: List[DetectedProduct]
    token_usage: TokenUsage
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    error_message: Optional[str] = None
    created_at: datetime


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
    """Overall analytics, aggregate token usage, and summary of rack recognition results."""

    total_scans: int
    completed_scans: int
    processing_scans: int
    pending_scans: int
    failed_scans: int
    total_products_detected: int
    unique_products_count: int
    # Token analytics
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_estimated_cost_usd: float = 0.0
    avg_tokens_per_scan: float = 0.0
    avg_cost_per_scan_usd: float = 0.0
    top_products: List[TopProductItem]
    recent_uploads: List[UploadResultResponse]


class DeleteResponse(BaseModel):
    """Response returned when an upload is deleted."""

    upload_id: str
    message: str
