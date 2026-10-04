"""
app/routers/token_router.py — Dedicated Token Router API for PRAN-RFL MerchVision.

Provides endpoints for:
  - Model capabilities & specifications (Gemini 3.7 Flash)
  - Live token pricing & cost formulas
  - System-wide token consumption analytics & statistics
  - Token estimation prior to inference (including Lanczos downscaled vision tokens)
  - Live token counting (via Google Gemini SDK & offline estimator)
  - Intelligent token routing & validation against model limits (1,048,576 tokens)
  - Token breakdown logs per upload
"""
from __future__ import annotations

import logging
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import ProcessingStatus, RackUpload
from app.schemas import (
    ModelCapabilities,
    ModelInfoResponse,
    OpenRouterChatRequest,
    OpenRouterChatResponse,
    TokenCountRequest,
    TokenCountResponse,
    TokenEstimateRequest,
    TokenEstimateResponse,
    TokenPricingResponse,
    TokenRouteRequest,
    TokenRouteResponse,
    TokenStatsResponse,
    TokenUsage,
    UploadResultResponse,
)
from app.services.ai_service import (
    AIServiceError,
    calculate_token_cost,
    count_tokens_direct,
    estimate_tokens,
    openrouter_chat_completion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tokens", tags=["Token Router & OpenRouter API"])


# ── 1. Model Specifications & Capabilities ────────────────────────────────────


@router.get(
    "/models",
    response_model=ModelInfoResponse,
    summary="Get active OpenRouter / Gemini model specifications and token limits",
)
def get_model_info() -> ModelInfoResponse:
    """
    Returns full capabilities, token limits, and configurations for the active **OpenRouter / Gemini 3.7 Flash** model:
    - **Provider:** `openrouter` (or `gemini`)
    - **OpenRouter Model Code:** `google/gemini-3.7-flash`
    - **OpenRouter Endpoint:** `https://openrouter.ai/api/v1/chat/completions`
    - **Input Token Limit:** 1,048,576 tokens
    - **Output Token Limit:** 65,536 tokens
    - **Supported Inputs:** Text, Image, Video, Audio, PDF
    - **Supported Outputs:** Text
    - **Thinking Modes:** Supported (low, medium, high / budget)
    - **OpenRouter Pricing:** $0.75 / 1M prompt, $3.75 / 1M completion
    """
    settings = get_settings()
    provider = getattr(settings, "ai_provider", "openrouter") or "openrouter"
    model_code = settings.openrouter_model if provider == "openrouter" else settings.gemini_model

    return ModelInfoResponse(
        provider=provider,
        model_code=model_code,
        openrouter_endpoint="https://openrouter.ai/api/v1/chat/completions",
        version="Stable: gemini-3.7-flash (OpenRouter: google/gemini-3.7-flash)",
        latest_update="August 2026",
        input_token_limit=getattr(settings, "max_input_tokens", 1_048_576),
        output_token_limit=getattr(settings, "max_output_tokens", 65_536),
        supported_inputs=["Text", "Image", "Video", "Audio", "PDF"],
        supported_outputs=["Text"],
        capabilities=ModelCapabilities(),
        active_thinking_budget=int(getattr(settings, "gemini_thinking_budget", 128) or 128),
        active_thinking_level=str(getattr(settings, "gemini_thinking_level", "low") or "low"),
        pricing_input_per_million=float(settings.token_cost_input_per_million),
        pricing_output_per_million=float(settings.token_cost_output_per_million),
    )


# ── 2. Token Pricing & Cost Scheme ────────────────────────────────────────────


@router.get(
    "/pricing",
    response_model=TokenPricingResponse,
    summary="Get current token pricing rates and cost calculation formula",
)
def get_token_pricing() -> TokenPricingResponse:
    """
    Returns the configured rates per 1,000,000 tokens and 1,000 tokens for google/gemini-3.7-flash on OpenRouter.
    """
    settings = get_settings()
    provider = getattr(settings, "ai_provider", "openrouter") or "openrouter"
    model_code = settings.openrouter_model if provider == "openrouter" else settings.gemini_model
    in_per_m = float(settings.token_cost_input_per_million)
    out_per_m = float(settings.token_cost_output_per_million)

    return TokenPricingResponse(
        provider=provider,
        model=model_code,
        currency="USD",
        input_cost_per_million=in_per_m,
        output_cost_per_million=out_per_m,
        input_cost_per_1k=round(in_per_m / 1000.0, 6),
        output_cost_per_1k=round(out_per_m / 1000.0, 6),
        formula="cost = (input_tokens / 1,000,000 * input_rate) + (output_tokens / 1,000,000 * output_rate)",
    )


# ── 3. OpenRouter Direct Chat Completion ──────────────────────────────────────


@router.post(
    "/chat",
    response_model=OpenRouterChatResponse,
    summary="Direct text chat completion with google/gemini-3.7-flash via OpenRouter",
)
@router.post(
    "/openrouter/chat",
    response_model=OpenRouterChatResponse,
    include_in_schema=False,
)
def chat_with_openrouter(payload: OpenRouterChatRequest) -> OpenRouterChatResponse:
    """
    Execute chat completions with `google/gemini-3.7-flash` through OpenRouter (`https://openrouter.ai/api/v1`).
    """
    settings = get_settings()
    messages_payload = [{"role": m.role, "content": m.content} for m in payload.messages]

    try:
        response = openrouter_chat_completion(
            messages=messages_payload,
            model=payload.model or settings.openrouter_model or "google/gemini-3.7-flash",
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else (prompt_tokens + output_tokens)
        direct_cost = getattr(usage, "cost", None)
        if direct_cost is not None and isinstance(direct_cost, (int, float)) and direct_cost >= 0:
            cost = round(float(direct_cost), 6)
        else:
            cost = calculate_token_cost(prompt_tokens, output_tokens)

        return OpenRouterChatResponse(
            provider="openrouter",
            model=payload.model or settings.openrouter_model or "google/gemini-3.7-flash",
            content=content,
            token_usage=TokenUsage(
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=cost,
            ),
            status="success",
        )
    except AIServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OpenRouter call failed: {exc}",
        )


# ── 4. Token Consumption Statistics & Aggregations ───────────────────────────


@router.get(
    "/summary",
    response_model=TokenStatsResponse,
    summary="Get aggregated token metrics and total cost across all shelf scans",
)
@router.get(
    "/stats",
    response_model=TokenStatsResponse,
    include_in_schema=False,
)
def get_token_stats(db: Session = Depends(get_db)) -> TokenStatsResponse:
    """
    Aggregates global token consumption, USD expenditure, and scan averages.
    """
    settings = get_settings()
    total_scans = db.query(RackUpload).count()
    completed_scans = (
        db.query(RackUpload)
        .filter(RackUpload.status == ProcessingStatus.COMPLETED)
        .count()
    )

    completed_rows = (
        db.query(RackUpload)
        .filter(RackUpload.status == ProcessingStatus.COMPLETED)
        .all()
    )

    total_in = sum((r.input_tokens or 0) for r in completed_rows)
    total_out = sum((r.output_tokens or 0) for r in completed_rows)
    total_tok = sum((r.total_tokens or 0) for r in completed_rows)
    total_cost = sum((r.estimated_cost_usd or 0.0) for r in completed_rows)

    avg_tokens = round(total_tok / completed_scans, 2) if completed_scans > 0 else 0.0
    avg_cost = round(total_cost / completed_scans, 6) if completed_scans > 0 else 0.0

    pricing = get_token_pricing()

    return TokenStatsResponse(
        total_scans=total_scans,
        completed_scans=completed_scans,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_tokens=total_tok,
        total_estimated_cost_usd=round(total_cost, 6),
        avg_tokens_per_scan=avg_tokens,
        avg_cost_per_scan_usd=avg_cost,
        pricing_rates=pricing,
    )


# ── 4. Token Usage Estimator ──────────────────────────────────────────────────


@router.post(
    "/estimate",
    response_model=TokenEstimateResponse,
    summary="Estimate input tokens, vision patch tokens, output tokens, and USD cost",
)
def estimate_token_usage(payload: TokenEstimateRequest) -> TokenEstimateResponse:
    """
    Calculates estimated Gemini 3.7 Flash token usage and USD cost for a given image resolution,
    Lanczos downscaling profile, system instruction, user prompt, and thinking budget.
    """
    res = estimate_tokens(
        image_input=payload.image_url,
        image_width=payload.image_width,
        image_height=payload.image_height,
        custom_prompt=payload.custom_prompt,
        thinking_budget=payload.thinking_budget,
    )

    return TokenEstimateResponse(
        model=res["model"],
        estimated_vision_tokens=res["estimated_vision_tokens"],
        estimated_system_tokens=res["estimated_system_tokens"],
        estimated_prompt_tokens=res["estimated_prompt_tokens"],
        estimated_output_tokens=res["estimated_output_tokens"],
        estimated_thinking_tokens=res["estimated_thinking_tokens"],
        estimated_total_tokens=res["estimated_total_tokens"],
        estimated_cost_usd=res["estimated_cost_usd"],
        dimensions_analyzed=res.get("dimensions_analyzed"),
        optimization_applied=res.get("optimization_applied", "Lanczos downscaling"),
    )


# ── 5. Token Counter ──────────────────────────────────────────────────────────


@router.post(
    "/count",
    response_model=TokenCountResponse,
    summary="Count exact tokens for text or image payloads against Gemini 3.7 Flash",
)
def count_tokens_endpoint(payload: TokenCountRequest) -> TokenCountResponse:
    """
    Performs token counting against Gemini 3.7 Flash limits using the SDK token counting engine.
    """
    res = count_tokens_direct(
        text=payload.text,
        image_input=payload.image_url,
    )

    return TokenCountResponse(
        model=res["model"],
        total_tokens=res["total_tokens"],
        input_token_limit=res["input_token_limit"],
        is_within_limit=res["is_within_limit"],
        remaining_tokens_available=res["remaining_tokens_available"],
    )


# ── 6. Token Router & Payload Dispatcher ──────────────────────────────────────


@router.post(
    "/route",
    response_model=TokenRouteResponse,
    summary="Analyze token volume, validate limits, and recommend execution route",
)
def route_token_request(payload: TokenRouteRequest) -> TokenRouteResponse:
    """
    Intelligent router that:
    1. Estimates/counts token volume.
    2. Validates against Gemini 3.7 Flash 1,048,576 token limit.
    3. Recommends synchronous direct vs asynchronous queue vs batch processing.
    4. Advises optimal thinking budget/level.
    """
    settings = get_settings()
    count_res = count_tokens_direct(
        text=payload.prompt,
        image_input=payload.image_url,
    )

    est_tokens = count_res["total_tokens"]
    max_input = getattr(settings, "max_input_tokens", 1_048_576)
    max_output = getattr(settings, "max_output_tokens", 65_536)

    if est_tokens > max_input:
        return TokenRouteResponse(
            model=settings.gemini_model,
            route="rejected",
            estimated_input_tokens=est_tokens,
            configured_thinking_budget=0,
            configured_thinking_level="none",
            max_input_limit=max_input,
            max_output_limit=max_output,
            is_payload_valid=False,
            recommended_batch_mode=False,
            message=f"Payload exceeds maximum context window of {max_input:,} tokens.",
        )

    # Route recommendation
    priority = (payload.priority or "standard").lower()
    thinking_level = payload.thinking_level or getattr(settings, "gemini_thinking_level", "low")
    budget = int(getattr(settings, "gemini_thinking_budget", 128) or 128)

    if priority == "batch":
        route = "batch_api"
        is_batch = True
        msg = "Routed to Gemini 3.7 Flash Batch API for high-throughput 50% discount processing."
    elif est_tokens > 200_000:
        route = "async_queue"
        is_batch = False
        msg = "Large context payload: routed to async background processing queue."
    else:
        route = "synchronous_direct"
        is_batch = False
        msg = "Standard payload: routed to immediate synchronous zero-disk vision analysis."

    return TokenRouteResponse(
        model=settings.gemini_model,
        route=route,
        estimated_input_tokens=est_tokens,
        configured_thinking_budget=budget,
        configured_thinking_level=thinking_level,
        max_input_limit=max_input,
        max_output_limit=max_output,
        is_payload_valid=True,
        recommended_batch_mode=is_batch,
        message=msg,
    )


# ── 7. Token Usage History ────────────────────────────────────────────────────


@router.get(
    "/history",
    summary="Get recent scans with detailed token breakdowns",
)
def get_token_history(
    limit: int = Query(20, ge=1, le=100, description="Number of recent scans to return"),
    db: Session = Depends(get_db),
) -> List[dict[str, Any]]:
    """
    Returns recent scans with granular token breakdown (input, output, total, USD cost).
    """
    rows = (
        db.query(RackUpload)
        .order_by(RackUpload.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "upload_id": r.id,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "estimated_cost_usd": r.estimated_cost_usd or 0.0,
            "product_count": len(r.detected_products) if isinstance(r.detected_products, list) else 0,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
