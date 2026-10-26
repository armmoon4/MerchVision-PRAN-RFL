"""
app/schemas.py — Pydantic request/response models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from app.models import ProcessingStatus


# ── Brand & SKU Classification Enums (Image-Only Mode) ─────────────────────────


class BrandClass(str, Enum):
    PRAN = "pran"
    COMPETITOR = "competitor"
    UNKNOWN = "unknown"


class SkuStatus(str, Enum):
    MATCHED = "matched"            # high confidence + margin
    CANDIDATE = "candidate"        # plausible, show top-3
    UNKNOWN_PRAN = "unknown_pran"  # PRAN brand, not in catalogue
    NOT_APPLICABLE = "n/a"         # competitor


# ── Sub-models ────────────────────────────────────────────────────────────────


class CatalogueSuggestion(BaseModel):
    """One matched catalogue row from itemsdb.csv for a detected product."""

    sub_category_name: str = ""
    sub_category_code: str = ""
    category_name: str = ""
    category_code: str = ""
    item_name: str = ""    # exact Item Name from catalogue
    item_code: str = ""    # exact Item Code from catalogue
    item_image_url: str = ""  # exact Item Image URL from catalogue
    confidence: Optional[float] = None  # Match confidence score (0.0 to 1.0)

    @model_validator(mode="before")
    @classmethod
    def sync_image_urls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            url = data.get("item_image_url") or data.get("image_url") or data.get("Item Image URL") or ""
            if not data.get("item_image_url") and url:
                data["item_image_url"] = url
        return data




class DetectedProduct(BaseModel):
    """A single product identified on the rack shelf with catalogue suggestions."""

    product_name: str
    quantity_visible: Optional[int] = None

    # ── Catalogue enrichment (populated from itemsdb.csv lookup) ─────────────
    catalogue_suggestions: Optional[List["CatalogueSuggestion"]] = None
    matched: Optional[bool] = None  # True if ≥1 catalogue suggestion found


class TokenUsage(BaseModel):
    """Token metrics and estimated USD cost for an image AI analysis call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cached: Optional[bool] = False


# ── Request models ────────────────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    """
    Request payload for rack analysis.
    Send an S3 presigned URL, HTTP/HTTPS URL, Data URI, or raw Base64 image string.
    """

    image_url: Optional[str] = None  # S3 presigned URL, HTTP/HTTPS URL, Data URI, or Base64 string
    image: Optional[str] = None  # Backward-compatible alias for image_url


# Backward compatibility alias
AnalyzeImageRequest = AnalyzeRequest


class UploadUrlRequest(BaseModel):
    """Request payload for initiating async rack analysis from an image URL or S3 URL."""

    image_url: Optional[str] = None
    image: Optional[str] = None


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
    image_url: Optional[str] = None
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
    image_url: Optional[str] = None
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


# ── Token Router API Schemas ──────────────────────────────────────────────────


class ModelCapabilities(BaseModel):
    """Capabilities supported by the active Gemini model."""

    audio_generation: bool = False
    caching: bool = True
    code_execution: bool = True
    computer_use: str = "Supported (Preview)"
    file_search: bool = True
    function_calling: bool = True
    grounding_with_google_maps: bool = True
    image_generation: bool = False
    live_api: bool = False
    search_grounding: bool = True
    structured_outputs: bool = True
    thinking: str = "Supported (low, medium, high)"
    url_context: bool = True
    batch_api: bool = True
    flex_inference: bool = True
    priority_inference: bool = True


class ModelInfoResponse(BaseModel):
    """Metadata and token capacity specifications for the active model."""

    provider: str = "openrouter"  # "openrouter" or "gemini"
    model_code: str = "google/gemini-3.7-flash"
    openrouter_endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    version: str = "Stable: gemini-3.7-flash (OpenRouter: google/gemini-3.7-flash)"
    latest_update: str = "August 2026"
    input_token_limit: int = 1_048_576
    output_token_limit: int = 20_000
    supported_inputs: List[str] = ["Text", "Image", "Video", "Audio", "PDF"]
    supported_outputs: List[str] = ["Text"]
    capabilities: ModelCapabilities = ModelCapabilities()
    active_thinking_budget: int = 1024
    active_thinking_level: str = "medium"
    pricing_input_per_million: float = 0.75
    pricing_output_per_million: float = 3.75


class TokenPricingResponse(BaseModel):
    """Current token pricing rates and calculation scheme."""

    provider: str = "openrouter"
    model: str = "google/gemini-3.7-flash"
    currency: str = "USD"
    input_cost_per_million: float = 0.75
    output_cost_per_million: float = 3.75
    input_cost_per_1k: float = 0.00075
    output_cost_per_1k: float = 0.00375
    formula: str = "cost = (input_tokens / 1,000,000 * input_rate) + (output_tokens / 1,000,000 * output_rate)"


class OpenRouterChatMessage(BaseModel):
    """A message in an OpenRouter chat completion conversation."""

    role: str  # "system", "user", "assistant"
    content: str


class OpenRouterChatRequest(BaseModel):
    """Request payload to interact with google/gemini-3.7-flash via OpenRouter."""

    messages: List[OpenRouterChatMessage]
    model: Optional[str] = "google/gemini-3.7-flash"
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048


class OpenRouterChatResponse(BaseModel):
    """Response from OpenRouter chat completion."""

    provider: str = "openrouter"
    model: str = "google/gemini-3.7-flash"
    content: str
    token_usage: TokenUsage
    status: str = "success"



class TokenEstimateRequest(BaseModel):
    """Request payload to estimate token usage before model invocation."""

    image_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    custom_prompt: Optional[str] = None
    thinking_budget: Optional[int] = None


class TokenEstimateResponse(BaseModel):
    """Estimated token breakdown and USD cost."""

    model: str = "gemini-3.7-flash"
    estimated_vision_tokens: int
    estimated_system_tokens: int
    estimated_prompt_tokens: int
    estimated_output_tokens: int
    estimated_thinking_tokens: int
    estimated_total_tokens: int
    estimated_cost_usd: float
    dimensions_analyzed: Optional[str] = None
    optimization_applied: str = "Lanczos downscaling (max 1600px)"


class TokenCountRequest(BaseModel):
    """Request payload for live token counting."""

    text: Optional[str] = None
    image_url: Optional[str] = None


class TokenCountResponse(BaseModel):
    """Accurate token count and capacity check."""

    model: str = "gemini-3.7-flash"
    total_tokens: int
    input_token_limit: int = 1_048_576
    is_within_limit: bool = True
    remaining_tokens_available: int


class TokenRouteRequest(BaseModel):
    """Request to route a prompt or image task through the token router."""

    image_url: Optional[str] = None
    prompt: Optional[str] = None
    thinking_level: Optional[str] = None  # low, medium, high
    priority: Optional[str] = "standard"  # standard, batch, flex, priority


class TokenRouteResponse(BaseModel):
    """Routing advice and validation from the Token Router API."""

    model: str = "gemini-3.7-flash"
    route: str  # "synchronous_direct", "async_queue", "batch_api"
    estimated_input_tokens: int
    configured_thinking_budget: int
    configured_thinking_level: str
    max_input_limit: int = 1_048_576
    max_output_limit: int = 20_000
    is_payload_valid: bool = True
    recommended_batch_mode: bool = False
    message: str


class TokenStatsResponse(BaseModel):
    """Aggregated global token metrics across all rack recognition calls."""

    total_scans: int
    completed_scans: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_estimated_cost_usd: float
    avg_tokens_per_scan: float
    avg_cost_per_scan_usd: float
    pricing_rates: TokenPricingResponse


# ── Enterprise Merchandising AI Schemas ──────────────────────────────────────────


class AuditSummary(BaseModel):
    status: str = "needs_attention"  # needs_attention | acceptable | unable_to_determine
    image_quality: str = "good"      # good | fair | poor
    key_findings: List[str] = []


class ShelfMetrics(BaseModel):
    pran_shelf_share_pct: Optional[float] = None
    competitor_shelf_share_pct: Optional[float] = None
    empty_shelf_share_pct: Optional[float] = None
    pran_facing_count: Optional[int] = None
    competitor_facing_count: Optional[int] = None
    arrangement_score: Optional[float] = None
    measurement_type: str = "measured"  # measured | estimated | unavailable


class DetectedProductDetail(BaseModel):
    brand: Optional[str] = "PRAN"
    brand_class: BrandClass = BrandClass.UNKNOWN
    brand_evidence: str = ""
    brand_confidence: float = 0.0
    product_name: Optional[str] = None
    variant: Optional[str] = None
    sku: Optional[str] = None
    sku_code: Optional[str] = None
    sku_status: SkuStatus = SkuStatus.UNKNOWN_PRAN
    facing_count: Optional[int] = 1
    shelf_level: Optional[str] = "eye_level"  # eye_level | top | bottom | unknown
    bounding_box_normalized: Optional[List[float]] = None
    confidence: Optional[str] = "high"  # high | medium | low
    catalogue_suggestions: Optional[List[CatalogueSuggestion]] = None
    candidates: Optional[List[CatalogueSuggestion]] = None
    matched: Optional[bool] = None


# Alias for clean parity with design specification
Detection = DetectedProductDetail


class IssueDetected(BaseModel):
    id: Optional[str] = None
    issue: str
    evidence: str = ""
    severity: str = "MEDIUM"  # HIGH | MEDIUM | LOW
    confidence: str = "high"  # high | medium | low
    recommended_action: str = ""
    is_resolved: bool = False


class VisibleGap(BaseModel):
    shelf_level: Optional[str] = "eye_level"
    relative_position: Optional[str] = "middle"
    estimated_empty_facings: Optional[int] = 1


class AvailabilityAlerts(BaseModel):
    visible_gaps: List[Any] = []
    missing_expected_skus: List[str] = []
    inventory_status: str = "unverified"


class PricingAndPromotions(BaseModel):
    product: Optional[str] = None
    visible_price: Optional[float] = None
    promotion_detected: bool = False
    pricing_violation: str = "not_evaluated"  # confirmed | suspected | not_detected | not_evaluated
    confidence: str = "high"


# ── Image-Only Structured Sub-Models ──────────────────────────────────────────


class ImageQualityDetail(BaseModel):
    score: float = 0.85
    issues: List[str] = []


class SkuBreakdownItem(BaseModel):
    product_name: str
    sku_status: SkuStatus = SkuStatus.UNKNOWN_PRAN
    sku_code: Optional[str] = None
    facings: int = 1
    levels: List[str] = []


class PranSummary(BaseModel):
    pran_detected: bool = False
    distinct_products: int = 0
    total_facings: int = 0
    sos_pct: float = 0.0
    eye_level_share_pct: float = 0.0
    sku_breakdown: List[SkuBreakdownItem] = []


class CatalogueCoverage(BaseModel):
    pran_facings_total: int = 0
    matched: int = 0
    candidate: int = 0
    unknown_pran: int = 0


class CompetitorSummary(BaseModel):
    brand: str
    facings: int = 1


class RetailExecutionScore(BaseModel):
    score: Optional[float] = None
    grade: str = "Needs Attention"  # Excellent, Good, Needs Attention, Critical Action Required, No PRAN Detected
    mode: str = "image_only"
    pillars: Dict[str, float] = {}
    availability_score: Optional[float] = None
    planogram_score: Optional[float] = None
    sos_score: Optional[float] = None
    pricing_score: Optional[float] = None
    promotion_score: Optional[float] = None
    deductions: List[str] = []


class ShelfAuditReport(BaseModel):
    """Complete structured Shelf Audit Report adhering to the PRAN Merchandising AI specification."""

    model_config = ConfigDict(from_attributes=True)

    audit_id: str
    input_context: str = "image_only"
    image_quality: Optional[ImageQualityDetail] = None
    fixture_category: str = "general"
    pran_summary: Optional[PranSummary] = None
    competitors: List[CompetitorSummary] = []
    catalogue_coverage: Optional[CatalogueCoverage] = None
    visit_id: Optional[str] = None
    outlet_id: Optional[str] = None
    outlet_name: Optional[str] = None
    audit_stage: str = "STANDALONE"  # BEFORE | AFTER | STANDALONE
    audit_summary: AuditSummary
    retail_execution_score: RetailExecutionScore
    shelf_metrics: ShelfMetrics
    detected_products: List[DetectedProductDetail] = []
    issues_detected: List[IssueDetected] = []
    availability_alerts: AvailabilityAlerts
    pricing_and_promotions: List[PricingAndPromotions] = []
    recommended_actions: List[str] = []
    management_insights: List[str] = []
    limitations: List[str] = []
    human_verification_required: bool = False
    token_usage: Optional[TokenUsage] = None
    image_url: Optional[str] = None
    created_at: Optional[datetime] = None


class AuditAnalyzeRequest(BaseModel):
    """Request payload for running a full retail shelf audit."""

    image_url: Optional[str] = None
    image: Optional[str] = None
    outlet_id: Optional[str] = None
    visit_id: Optional[str] = None
    audit_stage: Optional[str] = "STANDALONE"  # BEFORE | AFTER | STANDALONE
    merchandiser_id: Optional[str] = None
    merchandiser_name: Optional[str] = None
    expected_skus: Optional[List[str]] = None
    planogram_rules: Optional[Dict[str, Any]] = None
    approved_prices: Optional[Dict[str, float]] = None


class AuditVerifyRequest(BaseModel):
    """Request payload to verify shelf improvements after merchandiser fixes."""

    audit_id: str
    after_image_url: Optional[str] = None
    after_image: Optional[str] = None
    completed_actions: Optional[List[str]] = None


class AuditVerifyResponse(BaseModel):
    """Verification results comparing BEFORE and AFTER audits."""

    visit_id: str
    before_audit_id: str
    after_audit_id: str
    before_score: float
    after_score: float
    score_improvement: float
    is_audit_passed: bool
    resolved_issues: List[str] = []
    remaining_issues: List[str] = []
    pran_sos_delta_pct: float
    after_report: ShelfAuditReport


# ── Management Dashboard Sub-Models ──────────────────────────────────────────


class ExecutiveOverviewModule(BaseModel):
    total_audits: int
    outlets_covered: int
    avg_pran_sos_pct: float
    avg_execution_score: float
    issue_resolution_rate_pct: float
    failed_audits_count: int


class ShelfIntelligenceModule(BaseModel):
    pran_overall_sos_pct: float
    competitor_overall_sos_pct: float
    golden_shelf_eye_level_pran_pct: float
    top_competitors: List[Dict[str, Any]] = []
    category_shares: List[Dict[str, Any]] = []


class ProductAvailabilityModule(BaseModel):
    overall_osa_pct: float
    total_gaps_flagged: int
    top_missing_skus: List[Dict[str, Any]] = []
    chronic_stockout_outlets_count: int


class FieldForcePerformanceModule(BaseModel):
    total_visits_planned: int
    total_visits_completed: int
    completion_rate_pct: float
    avg_visit_duration_mins: float
    first_time_fix_rate_pct: float
    top_merchandisers: List[Dict[str, Any]] = []


class TrendsAndAlertsModule(BaseModel):
    territory_rankings: List[Dict[str, Any]] = []
    critical_alerts_count: int
    week_over_week_sos_gain_pct: float


class ExceptionOutletItem(BaseModel):
    outlet_id: str
    outlet_name: str
    territory: str
    distributor_name: str
    last_score: float
    urgency_index: float
    missing_skus_count: int
    competitor_share_pct: float
    primary_issue: str


class ManagementDashboardResponse(BaseModel):
    executive_overview: ExecutiveOverviewModule
    shelf_intelligence: ShelfIntelligenceModule
    product_availability: ProductAvailabilityModule
    field_force_performance: FieldForcePerformanceModule
    trends_and_alerts: TrendsAndAlertsModule
    exception_outlets: List[ExceptionOutletItem]


