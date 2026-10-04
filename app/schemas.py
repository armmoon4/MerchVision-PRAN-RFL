"""
app/schemas.py — Pydantic request/response models.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from app.models import ProcessingStatus


# ── Sub-models ────────────────────────────────────────────────────────────────


class CatalogueSuggestion(BaseModel):
    """One matched catalogue row from itemsdb.csv for a detected product."""

    sub_category_name: str = ""
    sub_category_code: str = ""
    category_name: str = ""
    category_code: str = ""
    item_name: str = ""    # exact Item Name from catalogue
    item_code: str = ""    # exact Item Code from catalogue
    confidence: Optional[float] = None  # Match confidence score (0.0 to 1.0)



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
    output_token_limit: int = 65_536
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
    max_output_limit: int = 65_536
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

