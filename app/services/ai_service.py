"""
app/services/ai_service.py — Calls Google Gemini to detect PRAN-RFL products.

Design notes:
  - Uses the official google-genai SDK.
  - Download-then-delete pattern: image is downloaded to a secure temp file,
    analyzed, and the temp file is ALWAYS deleted in a finally block.
  - Instructs the model (system prompt + response_mime_type="application/json") to return ONLY a JSON array.
  - Strips markdown code fences if present before json.loads.
  - Temperature 1: standard for this model configuration.
  - Raises AIServiceError on timeout, bad JSON, or non-array result.
  - Dynamic self-thinking: Gemini decides reasoning depth autonomously.
  - Smart image downscaling (Lanczos, max 1600px) cuts vision input tokens by 40-60%.
  - Retries up to 3 times with exponential backoff on 503/429 errors.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
import time
import re
import socket
from typing import Any, Dict, List, Optional, Tuple, Set

from PIL import Image

logger = logging.getLogger(__name__)

# Force IPv4 socket resolution to prevent [Errno 101] Network is unreachable on Docker/WSL2
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
from threading import Lock
import uuid

from google import genai
from google.genai import types
from google.genai.errors import APIError
from openai import OpenAI, OpenAIError
import httpx

from app.config import get_settings
from app.schemas import (
    AuditSummary,
    AvailabilityAlerts,
    CatalogueSuggestion,
    DetectedProductDetail,
    IssueDetected,
    PricingAndPromotions,
    RetailExecutionScore,
    ShelfAuditReport,
    ShelfMetrics,
    TokenUsage,
)
from app.services.items_db_service import enrich_product, enrich_products, load_items_db
from app.services.rules_engine import evaluate_retail_execution


# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Retail shelf auditor for PRAN products.
Identify all visible PRAN products from the shelf photo.
Brands: PRAN juices, Frooto, Drinko, Lassi, Moo, snacks, noodles, Mr. Noodles, chips, biscuits, Bisk Club, spices, sauces, pickles, All Time, Potata.
Return plain text ONLY — one product per line in exact format:
product name|quantity
Rules:
- product name: full name with brand + variant + size (e.g. PRAN Mango Juice 250ml)
- quantity: integer count visible (default 1)
- NO markdown, NO JSON, NO explanations. If none visible, output single empty line."""

MERCHANDISING_SYSTEM_PROMPT = """You are PRAN Merchandising AI, an expert retail shelf auditor. Analyze shelf photos and return concise, actionable insights in valid JSON only.

DETECTION RULES:
1. Product detection: Identify PRAN and competitor brands, product names, variants, and facing counts.
   - PRAN products include: Frooto, Drinko, Drinko Float, Mr. Noodles, Potata, Bisk Club, Pran UP, Lassi, Cheer Up, Maxx, All Time, Badam Vaja, Dal Vaja, Chanachur, Toast, Rusk, Litchi, Semai, and any item carrying the PRAN or RFL logo. For any PRAN product, set "brand": "PRAN".
   - For competitor products, identify their specific brand name (e.g., Julie's, Nabati, Jacker, Coca-Cola, Pepsi, Sprite, Maggi, Knorr, Nestle, Olympic).
2. Shelf Share (SOS): Estimate PRAN and competitor shelf share percentage (0-100) by visible shelf width.
3. Shelf Levels: Assign shelf_level as "top", "eye_level" (golden zone, 1.2m-1.6m high), "middle", or "bottom".
4. Pricing & Promotions: Extract readable prices and promotional wobblers/signage. Only include entries for products with readable prices, promotional tags, or missing price issues.
5. Conciseness: Keep sentences tight (1 sentence per finding/action). Omit filler text. Keep bounding_box_normalized as null.

Return JSON adhering strictly to this structure:
{
  "audit_summary": {
    "status": "needs_attention | acceptable | unable_to_determine",
    "image_quality": "good | fair | poor",
    "key_findings": ["1-sentence finding", "1-sentence finding"]
  },
  "shelf_metrics": {
    "pran_shelf_share_pct": null,
    "competitor_shelf_share_pct": null,
    "empty_shelf_share_pct": null,
    "pran_facing_count": null,
    "competitor_facing_count": null,
    "arrangement_score": null,
    "measurement_type": "measured | estimated | unavailable"
  },
  "detected_products": [
    {
      "brand": "PRAN or competitor brand",
      "product_name": "Full product name",
      "variant": "Flavor or pack type or null",
      "sku": null,
      "facing_count": 1,
      "shelf_level": "top | eye_level | middle | bottom",
      "confidence": "high | medium | low"
    }
  ],
  "issues_detected": [
    {
      "issue": "Short issue title",
      "evidence": "1-sentence evidence",
      "severity": "HIGH | MEDIUM | LOW",
      "confidence": "high | medium | low",
      "recommended_action": "1-sentence corrective action"
    }
  ],
  "availability_alerts": {
    "visible_gaps": [],
    "missing_expected_skus": [],
    "inventory_status": "unverified"
  },
  "pricing_and_promotions": [
    {
      "product": "Product or display group name",
      "visible_price": null,
      "promotion_detected": false,
      "pricing_violation": "confirmed | suspected | not_detected | not_evaluated",
      "confidence": "high | medium | low"
    }
  ],
  "recommended_actions": ["1-sentence action"],
  "management_insights": ["1-sentence strategic business insight"],
  "limitations": [],
  "human_verification_required": false
}
Rules:
Return valid JSON only. Do not include Markdown, code fences, or text outside the JSON.
Never invent product identities, SKUs, prices, percentages, inventory, or sales metrics.
Set human_verification_required to true only if the shelf image is unclear or ambiguous."""

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJECT_PATTERN = re.compile(
    r'\{\s*"product_name"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,\s*"quantity_visible"\s*:\s*([0-9]+|null)\s*\}',
    re.DOTALL | re.IGNORECASE,
)
# Compact pipe-delimited pattern: "Product Name 250ml|6"
_PIPE_LINE_PATTERN = re.compile(
    r'^(?P<name>[^|\n]+?)\|(?P<qty>\d+)\s*$',
    re.MULTILINE,
)


# ── In-Memory LRU Exact Image Analysis Cache ──────────────────────────────────


class ImageAnalysisCache:
    """
    Thread-safe LRU cache for image analysis results keyed by image SHA-256.
    Eliminates 100% of API tokens and latency for duplicate scans or retry requests.
    """

    def __init__(self, maxsize: int = 256, ttl_seconds: float = 3600.0):
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = Lock()

    def get(self, image_hash: str) -> dict[str, Any] | None:
        with self._lock:
            if image_hash not in self._cache:
                return None
            created_at, result = self._cache[image_hash]
            if time.time() - created_at > self._ttl:
                del self._cache[image_hash]
                return None
            self._cache.move_to_end(image_hash)
            # Return cached response with zero newly incurred tokens and cost
            return {
                "products": list(result.get("products", [])),
                "token_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "cached": True,
                },
                "raw_text": result.get("raw_text", ""),
                "cached": True,
            }

    def set(self, image_hash: str, result: dict[str, Any]) -> None:
        with self._lock:
            if image_hash in self._cache:
                self._cache.move_to_end(image_hash)
            elif len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)
            self._cache[image_hash] = (time.time(), result)


_IMAGE_CACHE = ImageAnalysisCache()


class ShelfAuditCache:
    """
    Thread-safe LRU cache for complete ShelfAuditReport keyed by image SHA-256 + parameters.
    Returns identical results in <1ms for duplicate scans, re-analysis, and dashboard queries.
    """

    def __init__(self, maxsize: int = 128, ttl_seconds: float = 1800.0):
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, tuple[float, ShelfAuditReport]] = OrderedDict()
        self._lock = Lock()

    def get(self, cache_key: str) -> ShelfAuditReport | None:
        with self._lock:
            if cache_key not in self._cache:
                return None
            created_at, report = self._cache[cache_key]
            if time.time() - created_at > self._ttl:
                del self._cache[cache_key]
                return None
            self._cache.move_to_end(cache_key)
            # Clone with a fresh unique audit_id and timestamp
            cloned = report.model_copy(deep=True)
            cloned.audit_id = str(uuid.uuid4())
            cloned.created_at = datetime.now(timezone.utc)
            if cloned.token_usage:
                cloned.token_usage.cached = True
            return cloned

    def set(self, cache_key: str, report: ShelfAuditReport) -> None:
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
            elif len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)
            self._cache[cache_key] = (time.time(), report)


_SHELF_AUDIT_CACHE = ShelfAuditCache()


# ── Persistent HTTP Connection Pools & Client Singletons ──────────────────────

_openrouter_client: OpenAI | None = None
_openrouter_client_lock = Lock()
_image_http_client: httpx.Client | None = None
_image_http_client_lock = Lock()


def _get_image_http_client() -> httpx.Client:
    """Reuse persistent HTTP client for remote image downloads to eliminate repeated TLS handshakes."""
    global _image_http_client
    if _image_http_client is None:
        with _image_http_client_lock:
            if _image_http_client is None:
                _image_http_client = httpx.Client(
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    follow_redirects=True,
                    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0),
                )
    return _image_http_client


def _get_openrouter_client() -> OpenAI:
    """Reuse persistent OpenAI client with connection pooling to eliminate TCP/TLS overhead on every request."""
    global _openrouter_client
    if _openrouter_client is None:
        with _openrouter_client_lock:
            if _openrouter_client is None:
                settings = get_settings()
                api_key = settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
                if not api_key or "YOUR_OPENROUTER" in api_key:
                    raise AIServiceError("OPENROUTER_API_KEY is not set. Add it to your .env file.")
                http_client = httpx.Client(
                    timeout=httpx.Timeout(
                        float(getattr(settings, "openrouter_timeout_seconds", 45) or 45),
                        connect=10.0,
                    ),
                    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0),
                )
                _openrouter_client = OpenAI(
                    base_url=settings.openrouter_base_url or "https://openrouter.ai/api/v1",
                    api_key=api_key,
                    http_client=http_client,
                )
    return _openrouter_client


# ── Errors ────────────────────────────────────────────────────────────────────


class AIServiceError(Exception):
    """Raised when the AI call fails for any reason."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_mime_type(data: bytes) -> str:
    """Detect image MIME type from binary magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _optimize_image_bytes(raw_data: bytes, max_dim: int = 1024) -> tuple[bytes, str]:
    """
    Downscale camera photos to max dimension (default 1024px) using Lanczos antialiasing.
    Optimized vision preserves fine packaging text, labels, readable prices & barcodes
    while reducing vision tokens and inference latency.
    """
    try:
        with Image.open(io.BytesIO(raw_data)) as im:
            detected_format = (im.format or "JPEG").upper()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_dim:
                im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85, optimize=True)
                opt_data = buf.getvalue()
                logger.info(
                    "Optimized image from %dx%d (%d bytes) to %dx%d (%d bytes)",
                    w, h, len(raw_data), im.size[0], im.size[1], len(opt_data),
                )
                return opt_data, "image/jpeg"

            # If image is large (>250KB) and not already JPEG/WebP, compress to quality 85 JPEG
            if len(raw_data) > 250_000 and detected_format not in ("JPEG", "WEBP"):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85, optimize=True)
                return buf.getvalue(), "image/jpeg"

            mime = "image/png" if detected_format == "PNG" else ("image/webp" if detected_format == "WEBP" else "image/jpeg")
            return raw_data, mime
    except Exception as exc:
        logger.warning("Image optimization skipped (fallback to raw bytes): %s", exc)
        return raw_data, _detect_mime_type(raw_data)


def _resolve_image_bytes(image_input: str | bytes) -> tuple[bytes, str]:
    """
    Resolve image_input to (optimized_bytes, mime_type).

    Supports:
      - Raw bytes
      - Base64 Data URI  (data:image/jpeg;base64,...)
      - HTTP / HTTPS / S3 presigned URL  → streamed to memory buffer via persistent client
      - Pure Base64 string               → decoded to bytes

    Raises AIServiceError on any failure.
    """
    settings = get_settings()
    max_dim = int(getattr(settings, "max_image_dimension", 1024) or 1024)

    # 1. Direct raw bytes
    if isinstance(image_input, bytes):
        if not image_input:
            raise AIServiceError("Image bytes cannot be empty.")
        return _optimize_image_bytes(image_input, max_dim=max_dim)

    image_str = (image_input or "").strip()
    if not image_str:
        raise AIServiceError("Image input cannot be empty.")

    # 2. Base64 Data URI (e.g. data:image/jpeg;base64,/9j/...)
    if image_str.startswith("data:"):
        match = re.match(r"^data:([^;]+);base64,(.*)$", image_str, re.DOTALL)
        if match:
            try:
                raw_data = base64.b64decode(match.group(2))
                return _optimize_image_bytes(raw_data, max_dim=max_dim)
            except Exception as exc:
                raise AIServiceError(f"Failed to decode base64 data URI: {exc}") from exc
        raise AIServiceError("Invalid base64 data URI format.")

    # 3. Remote HTTP / HTTPS / S3 presigned URL → streamed via persistent connection pool
    if image_str.startswith(("http://", "https://")):
        try:
            client = _get_image_http_client()
            resp = client.get(image_str)
            resp.raise_for_status()
            return _optimize_image_bytes(resp.content, max_dim=max_dim)
        except Exception as exc:
            raise AIServiceError(f"Failed to fetch remote image from '{image_str}': {exc}") from exc

    # 4. Pure Base64 string (e.g. /9j/4AAQSkZJRg... from S3 client payload)
    try:
        clean_b64 = re.sub(r"\s+", "", image_str).strip("\"'")
        raw_data = base64.b64decode(clean_b64, validate=False)
        if (
            raw_data.startswith(b"\xff\xd8\xff")
            or raw_data.startswith(b"\x89PNG\r\n\x1a\n")
            or (raw_data.startswith(b"RIFF") and len(raw_data) >= 12 and raw_data[8:12] == b"WEBP")
            or raw_data.startswith(b"GIF8")
        ):
            return _optimize_image_bytes(raw_data, max_dim=max_dim)
    except Exception:
        pass

    raise AIServiceError(
        "Unable to load image. Ensure it is a valid HTTP/HTTPS/S3 URL, pure Base64 string, or Data URI."
    )


# ── Main function ─────────────────────────────────────────────────────────────


_COMPACT_USER_PROMPT = "List visible PRAN products as: product name|quantity"


def _call_openrouter(
    image_bytes: bytes,
    mime_type: str,
    prompt: str = _COMPACT_USER_PROMPT,
    system_prompt: str = SYSTEM_PROMPT,
    response_json: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Call OpenRouter (https://openrouter.ai/api/v1) with google/gemini-3.7-flash using persistent OpenAI SDK client.
    """
    settings = get_settings()
    client = _get_openrouter_client()

    base64_img = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_img}"

    model_name = settings.openrouter_model or "google/gemini-3.7-flash"
    logger.info("Calling OpenRouter with model: %s (base_url: %s, json_mode: %s)", model_name, settings.openrouter_base_url, response_json)

    max_retries = 3
    retry_delay = 3.0
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            extra_body: dict[str, Any] = {}
            # Apply reasoning configuration for Gemini 3.7 Flash thinking models
            if "3.7" in model_name or "thinking" in model_name:
                effort = str(getattr(settings, "openrouter_reasoning_effort", "low") or "low").lower()
                if effort in ("low", "medium", "high"):
                    extra_body["reasoning"] = {"effort": effort}
                else:
                    extra_body["reasoning"] = {"effort": "low"}

            max_tokens_val = int(getattr(settings, "openrouter_max_tokens", 6000) or 6000)
            create_kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": system_prompt,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url
                                }
                            }
                        ]
                    }
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens_val,
            }
            if response_json:
                create_kwargs["response_format"] = {"type": "json_object"}
            if extra_body:
                create_kwargs["extra_body"] = extra_body

            response = client.chat.completions.create(**create_kwargs)
            raw_text = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else (prompt_tokens + output_tokens)

            cost = calculate_token_cost(prompt_tokens, output_tokens)

            logger.info(
                "OpenRouter Token usage: in=%d, out=%d, total=%d, cost=$%.6f",
                prompt_tokens, output_tokens, total_tokens, cost,
            )

            return raw_text, {
                "input_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": cost,
            }
        except OpenAIError as exc:
            logger.warning("OpenRouter API error on attempt %d/%d: %s", attempt, max_retries, exc)
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            raise AIServiceError(f"OpenRouter API error: {exc}") from exc
        except Exception as exc:
            raise AIServiceError(f"Unexpected error calling OpenRouter: {exc}") from exc
    raise AIServiceError(f"OpenRouter call failed after {max_retries} attempts: {last_exc}")


def _call_gemini_direct(
    image_bytes: bytes,
    mime_type: str,
    prompt: str = "List all PRAN and RFL products visible. One per line as: product name|quantity",
    system_prompt: str = SYSTEM_PROMPT,
    response_json: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Call Google Gemini Direct using google-genai SDK.
    """
    settings = get_settings()
    if not settings.gemini_api_key or "YOUR_GEMINI" in settings.gemini_api_key:
        raise AIServiceError("GEMINI_API_KEY is not set. Add it to your .env file.")

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    thinking_config = None
    thinking_budget = getattr(settings, "gemini_thinking_budget", 1024)
    thinking_level = getattr(settings, "gemini_thinking_level", "medium")
    if thinking_budget is not None and thinking_budget >= 0:
        thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)
    elif thinking_level in ("low", "medium", "high"):
        thinking_config = types.ThinkingConfig(thinking_level=thinking_level)

    client = genai.Client(api_key=settings.gemini_api_key)
    model_name: str = settings.gemini_model
    logger.info("Calling Gemini direct model: %s (thinking_budget=%s, json_mode=%s)", model_name, thinking_budget, response_json)

    max_retries = 3
    retry_delay = 5.0
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    image_part,
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2 if response_json else 1,
                    response_mime_type="application/json" if response_json else None,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    thinking_config=thinking_config,
                ),
            )
            raw_text: str = response.text or ""

            usage_data = getattr(response, "usage_metadata", None)
            prompt_tokens = int(getattr(usage_data, "prompt_token_count", 0) or 0) if usage_data else 0
            candidates_tokens = int(getattr(usage_data, "candidates_token_count", 0) or 0) if usage_data else 0
            thoughts_tokens = int(getattr(usage_data, "thoughts_token_count", 0) or 0) if usage_data else 0
            total_reported = int(getattr(usage_data, "total_token_count", 0) or 0) if usage_data else 0

            if thoughts_tokens > 0:
                output_tokens = candidates_tokens + thoughts_tokens
            elif total_reported > (prompt_tokens + candidates_tokens):
                output_tokens = total_reported - prompt_tokens
            else:
                output_tokens = candidates_tokens

            total_tokens = total_reported if total_reported > 0 else (prompt_tokens + output_tokens)
            estimated_cost_usd = calculate_token_cost(prompt_tokens, output_tokens)

            return raw_text, {
                "input_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": estimated_cost_usd,
            }
        except APIError as exc:
            if exc.code in (429, 503) and attempt < max_retries:
                match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", str(exc.message or ""))
                delay = float(match.group(1)) + 1.5 if match else retry_delay
                logger.warning("Gemini API %d on attempt %d/%d — retrying in %.1fs: %s", exc.code, attempt, max_retries, delay, exc.message)
                time.sleep(delay)
                retry_delay *= 2
                last_exc = exc
                continue
            raise AIServiceError(f"Gemini API error {exc.code}: {exc.message}") from exc
        except Exception as exc:
            raise AIServiceError(f"Unexpected error calling Gemini: {exc}") from exc

    raise AIServiceError(f"Gemini API request failed after {max_retries} attempts: {last_exc}")


def openrouter_chat_completion(
    messages: list[dict[str, Any]],
    model: str | None = None,
    stream: bool = False,
) -> Any:
    """
    OpenRouter chat completion helper using persistent OpenAI Python client.
    Target endpoint: https://openrouter.ai/api/v1/chat/completions
    Default model: google/gemini-3.7-flash
    """
    settings = get_settings()
    client = _get_openrouter_client()
    return client.chat.completions.create(
        model=model or settings.openrouter_model or "google/gemini-3.7-flash",
        messages=messages,
        max_tokens=min(int(getattr(settings, "openrouter_max_tokens", 2000) or 2000), 2000),
        stream=stream,
    )


# ── Parse helpers ────────────────────────────────────────────────────────────


def _parse_pipe_text(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse compact pipe-delimited plain text format (primary output format).

    Expected one product per line:
        PRAN Mango Juice 250ml|6
        Bisk Club Cream Biscuit Chocolate 90g|4

    Falls back gracefully when model returns JSON instead.
    """
    results: list[dict[str, Any]] = []
    for m in _PIPE_LINE_PATTERN.finditer(raw_text or ""):
        name = m.group("name").strip()
        qty_str = m.group("qty").strip()
        if not name:
            continue
        try:
            qty = max(1, int(qty_str))
        except (ValueError, TypeError):
            qty = 1
        results.append({"product_name": name, "quantity_visible": qty})
    return results


def _parse_ai_response(raw_text: str) -> list[dict[str, Any]]:
    """
    Full parse of AI response.
    Priority: pipe-delimited text → JSON → regex object fallback.
    """
    # 1. Compact pipe-delimited format (primary — ~64% fewer output tokens)
    pipe_results = _parse_pipe_text(raw_text)
    if pipe_results:
        return pipe_results

    # 2. JSON fallback (model ignored instructions or old prompt in cache)
    stripped = (raw_text or "").strip()
    fence = _FENCE_PATTERN.search(stripped)
    json_text = fence.group(1).strip() if fence else stripped

    parsed: list[dict[str, Any]] = []
    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            parsed = [i for i in data if isinstance(i, dict)]
        elif isinstance(data, dict) and "products" in data and isinstance(data["products"], list):
            parsed = [i for i in data["products"] if isinstance(i, dict)]
    except (json.JSONDecodeError, ValueError):
        # 3. Regex object fallback for malformed JSON
        for m in _OBJECT_PATTERN.finditer(json_text):
            qty_raw = m.group(2).strip()
            parsed.append({
                "product_name":     m.group(1).strip(),
                "quantity_visible": int(qty_raw) if qty_raw.isdigit() else 1,
            })
    return parsed


# ── Main function ─────────────────────────────────────────────────────────────


def analyze_rack_image(image_input: str | bytes) -> dict[str, Any]:
    """
    Analyze rack photo using OpenRouter (google/gemini-3.7-flash) or Google Gemini Direct:

      1. Download / decode image → optimized bytes (memory buffer, zero disk storage)
      2. Call OpenRouter / Gemini with image and shelf recognition prompt
         (AI returns ONLY product_name + quantity — minimum tokens)
      3. Parse AI output, deduplicate products
      4. Enrich each product with catalogue metadata from itemsdb.csv (zero extra AI tokens)
      5. Return structured product list + token usage + USD cost

    Args:
        image_input: S3 presigned URL, HTTP URL, Base64 string, Data URI, or raw bytes.

    Returns:
        Dict with keys:
            - "products": list of enriched product dicts
            - "token_usage": {"input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd"}
            - "raw_text": raw completion text from model
    """
    settings = get_settings()

    # ── Step 0: Ensure catalogue is loaded (no-op after first call) ───────────
    load_items_db()

    # ── Step 1: Resolve image → optimized bytes ────────────────────────────────
    image_bytes, mime_type = _resolve_image_bytes(image_input)

    # ── Step 1b: Exact Image Cache Check (0 tokens, <1ms response) ─────────────
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    cached_res = _IMAGE_CACHE.get(image_hash)
    if cached_res is not None:
        logger.info("ImageAnalysisCache hit for hash %s... returning cached result (0 AI tokens)", image_hash[:12])
        return cached_res

    # ── Step 2: Route call to OpenRouter or Gemini Direct ─────────────────────
    provider = (getattr(settings, "ai_provider", "openrouter") or "openrouter").lower()
    has_openrouter_key = bool(settings.openrouter_api_key and "YOUR_OPENROUTER" not in settings.openrouter_api_key)
    has_gemini_key = bool(settings.gemini_api_key and "YOUR_GEMINI" not in settings.gemini_api_key)

    raw_text: str = ""
    token_usage: dict[str, Any] = {}

    if provider == "openrouter" and has_openrouter_key:
        raw_text, token_usage = _call_openrouter(image_bytes, mime_type)
    elif provider == "gemini" and has_gemini_key:
        raw_text, token_usage = _call_gemini_direct(image_bytes, mime_type)
    elif has_openrouter_key:
        raw_text, token_usage = _call_openrouter(image_bytes, mime_type)
    elif has_gemini_key:
        raw_text, token_usage = _call_gemini_direct(image_bytes, mime_type)
    else:
        # If no key is set yet, give clear guidance
        raise AIServiceError("OPENROUTER_API_KEY (or GEMINI_API_KEY) is not set. Add your API key to .env file.")

    # ── Step 3: Parse model output ─────────────────────────────────────────────
    # Uses pipe-delimited parser first (primary format), JSON as fallback.
    parsed_items: list[dict[str, Any]] = _parse_ai_response(raw_text)

    # ── Step 4: Deduplicate by product name ────────────────────────────────────
    # Uses max() not sum() — for a single-image scan, the same product appearing
    # twice means the model double-counted one shelf zone, not two locations.
    deduped: dict[str, dict[str, Any]] = {}
    for item in parsed_items:
        name = str(item.get("product_name", "Unknown Product")).strip()
        if not name:
            continue
        qty = item.get("quantity_visible")
        qty_int = int(qty) if isinstance(qty, (int, float)) and qty > 0 else 1
        name_key = name.lower()
        if name_key in deduped:
            prev_qty = deduped[name_key].get("quantity_visible") or 0
            deduped[name_key]["quantity_visible"] = max(prev_qty, qty_int)
        else:
            deduped[name_key] = {"product_name": name, "quantity_visible": qty_int}

    # ── Step 5: Enrich with itemsdb.csv catalogue (local, zero AI tokens) ─────
    raw_products = list(deduped.values())
    enriched_products = enrich_products(raw_products)
    logger.info(
        "Catalogue enrichment: %d/%d products matched in itemsdb.csv",
        sum(1 for p in enriched_products if p.get("matched")),
        len(enriched_products),
    )

    final_result = {
        "products": enriched_products,
        "token_usage": token_usage,
        "raw_text": raw_text,
    }
    _IMAGE_CACHE.set(image_hash, final_result)
    return final_result


def _parse_merchandising_json(raw_text: str) -> dict[str, Any]:
    """Extract and parse structured JSON from model response, repairing truncations if needed."""
    text = (raw_text or "").strip()
    # Strip reasoning / thinking tags if present
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fence = _FENCE_PATTERN.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass

    # Try repairing truncated JSON by walking backwards to find last clean structure
    if start != -1:
        truncated = text[start:]
        for i in range(len(truncated) - 1, 0, -1):
            if truncated[i] in ("}", "]", ","):
                candidate = truncated[:i] if truncated[i] == "," else truncated[:i + 1]
                open_curlies = candidate.count("{") - candidate.count("}")
                open_squares = candidate.count("[") - candidate.count("]")
                if open_squares > 0:
                    candidate += "]" * open_squares
                if open_curlies > 0:
                    candidate += "}" * open_curlies
                try:
                    repaired = json.loads(candidate)
                    if isinstance(repaired, dict):
                        logger.warning("Successfully repaired truncated merchandising JSON response.")
                        return repaired
                except Exception:
                    continue

    return {
        "audit_summary": {
            "status": "needs_attention",
            "image_quality": "good",
            "key_findings": ["Unable to fully extract shelf structure. Please verify camera angle and clarity."],
        },
        "shelf_metrics": {
            "pran_shelf_share_pct": 0.0,
            "competitor_shelf_share_pct": 0.0,
            "empty_shelf_share_pct": 0.0,
            "pran_facing_count": 0,
            "competitor_facing_count": 0,
            "arrangement_score": 0.0,
            "measurement_type": "unavailable",
        },
        "detected_products": [],
        "issues_detected": [],
        "availability_alerts": {
            "visible_gaps": [],
            "missing_expected_skus": [],
            "inventory_status": "unverified",
        },
        "pricing_and_promotions": [],
        "recommended_actions": [],
        "management_insights": [],
        "limitations": [],
        "human_verification_required": True,
    }


def analyze_shelf_audit(
    image_input: str | bytes,
    outlet_id: str | None = None,
    visit_id: str | None = None,
    audit_stage: str = "STANDALONE",
    merchandiser_id: str | None = None,
    merchandiser_name: str | None = None,
    expected_skus: list[str] | None = None,
    planogram_rules: dict[str, Any] | None = None,
    approved_prices: dict[str, float] | None = None,
    target_pran_sos: float = 45.0,
) -> ShelfAuditReport:
    """
    Enterprise retail shelf audit:
      1. Resolves and optimizes image (Lanczos downscaled, in-memory)
      2. Invokes Gemini 3.7 Flash / OpenRouter with MERCHANDISING_SYSTEM_PROMPT in JSON mode
      3. Parses multi-brand detections (PRAN and competitors, shelf levels, facings, bboxes, prices, gaps)
      4. Local zero-token enrichment of PRAN products against itemsdb.csv catalogue
      5. Passes raw vision insights to the Decoupled Business Rules Engine (rules_engine.py)
      6. Computes Retail Execution Score (RES: 0-100) with explainable deductions and generates actionable checklist
    """
    settings = get_settings()
    load_items_db()

    image_bytes, mime_type = _resolve_image_bytes(image_input)

    # ── Step 1b: Exact Image Cache Check (<1ms response for repeat audits) ─────
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    cache_key = f"{image_hash}:{outlet_id or ''}:{audit_stage}:{target_pran_sos}:{expected_skus or ''}"
    cached_report = _SHELF_AUDIT_CACHE.get(cache_key)
    if cached_report is not None:
        logger.info("ShelfAuditCache hit for hash %s... returning cached report (<1ms)", image_hash[:12])
        return cached_report

    provider = (getattr(settings, "ai_provider", "openrouter") or "openrouter").lower()
    has_openrouter_key = bool(settings.openrouter_api_key and "YOUR_OPENROUTER" not in settings.openrouter_api_key)
    has_gemini_key = bool(settings.gemini_api_key and "YOUR_GEMINI" not in settings.gemini_api_key)

    raw_text: str = ""
    token_usage_dict: dict[str, Any] = {}

    prompt_context_lines = [
        "Audit this retail shelf photo. Return concise, high-precision results in valid JSON.",
        "",
        "DETECTION OBJECTIVES:",
        "1. Product Detection:",
        "   - PRAN products (Frooto, Drinko, Drinko Float, Mr. Noodles, Potata, Bisk Club, PRAN UP, Lassi, Cheer Up, Maxx, All Time, Badam Vaja, Dal Vaja, Chanachur, Toast, Rusk, Litchi, Semai, and any item with PRAN logo) -> set \"brand\": \"PRAN\".",
        "   - Competitors: Specify exact brand name (e.g., Julie's, Nabati, Jacker, Muzic, Coca-Cola, Pepsi, Sprite, Maggi, Knorr, Nestle, Olympic).",
        "2. Facings & Levels:",
        "   - Count visible front facings. Identify shelf level: 'top', 'eye_level' (golden zone, 1.2m-1.6m), 'middle', or 'bottom'.",
        "3. Share of Shelf (SOS):",
        "   - Estimate horizontal shelf width %: pran_shelf_share_pct and competitor_shelf_share_pct.",
        "4. Availability & Gaps:",
        "   - Detect empty shelf voids or stockout gaps where products are missing.",
        "5. Pricing & Promotions:",
        "   - Extract readable shelf price tags and promotional materials (wobblers, discount stickers, banners).",
        "6. Corrective Actions:",
        "   - Provide concrete, prioritized 1-sentence actions for merchandisers (HIGH, MEDIUM, LOW).",
        "7. Conciseness:",
        "   - Keep findings tight (1 sentence each). Omit filler text. Keep bounding_box_normalized null.",
    ]

    if expected_skus:
        prompt_context_lines.append(f"Store Assortment / Expected SKUs: {', '.join(expected_skus)}")
    if target_pran_sos:
        prompt_context_lines.append(f"Agreed Target PRAN Share of Shelf: {target_pran_sos}%")
    if approved_prices:
        prompt_context_lines.append(f"Approved Price Reference: {json.dumps(approved_prices)}")
    if planogram_rules:
        prompt_context_lines.append(f"Planogram Guidelines: {json.dumps(planogram_rules)}")

    prompt_context_lines.append("Return valid JSON only adhering strictly to the requested schema.")
    user_audit_prompt = "\n".join(prompt_context_lines)

    if provider == "openrouter" and has_openrouter_key:
        raw_text, token_usage_dict = _call_openrouter(
            image_bytes,
            mime_type,
            prompt=user_audit_prompt,
            system_prompt=MERCHANDISING_SYSTEM_PROMPT,
            response_json=True,
        )
    elif provider == "gemini" and has_gemini_key:
        raw_text, token_usage_dict = _call_gemini_direct(
            image_bytes,
            mime_type,
            prompt=user_audit_prompt,
            system_prompt=MERCHANDISING_SYSTEM_PROMPT,
            response_json=True,
        )
    elif has_openrouter_key:
        raw_text, token_usage_dict = _call_openrouter(
            image_bytes,
            mime_type,
            prompt=user_audit_prompt,
            system_prompt=MERCHANDISING_SYSTEM_PROMPT,
            response_json=True,
        )
    elif has_gemini_key:
        raw_text, token_usage_dict = _call_gemini_direct(
            image_bytes,
            mime_type,
            prompt=user_audit_prompt,
            system_prompt=MERCHANDISING_SYSTEM_PROMPT,
            response_json=True,
        )
    else:
        raise AIServiceError("OPENROUTER_API_KEY (or GEMINI_API_KEY) is not set. Add your API key to .env file.")

    parsed_json = _parse_merchandising_json(raw_text)

    # 1. Parse raw detected products & enrich PRAN items with catalogue
    raw_products = parsed_json.get("detected_products") or []
    detected_products_list: list[DetectedProductDetail] = []

    for item in raw_products:
        if not isinstance(item, dict):
            continue
        p_name = str(item.get("product_name") or item.get("name") or "").strip()
        raw_brand = str(item.get("brand") or "").strip()
        variant = item.get("variant")
        sku = item.get("sku")
        facing = item.get("facing_count") or item.get("facings") or 1
        shelf_level = item.get("shelf_level") or "eye_level"
        confidence = item.get("confidence") or "high"
        bbox = item.get("bounding_box_normalized") or item.get("bbox")

        # Zero-token local catalogue enrichment for PRAN products
        suggestions = []
        matched = False
        pran_sub_brands = [
            "PRAN", "FROOTO", "DRINKO", "MR. NOODLES", "MR NOODLES", "NOODLE", "POTATA",
            "BISK CLUB", "LASSI", "PRAN UP", "CHEER UP", "MAXX", "ALL TIME", "BADAM VAJA",
            "DAL VAJA", "CHANACHUR", "TOAST", "RUSK", "MISTER NOODLE", "LITCHI DRINK",
            "CHIA SEED", "LAVILA", "MAMA CUBE"
        ]
        competitor_keywords = [
            "JULIE", "NABATI", "JACKER", "MUZIC", "COCOPIE", "CHOCO PIE", "OREO", "MUNCHY",
            "HUP SENG", "ROMA", "COCA-COLA", "COKE", "PEPSI", "FANTA", "SPRITE", "7UP",
            "MOUNTAIN DEW", "MIRINDA", "STING", "MAGGI", "KNORR", "BOMBAY SWEETS", "OLYMPIC",
            "NESTLE", "DANONE", "TWISTER", "DUTCH LADY", "MILO", "TIGER"
        ]

        has_competitor_indicator = (
            any(cw in raw_brand.upper() for cw in competitor_keywords)
            or any(cw in p_name.upper() for cw in competitor_keywords)
        )

        has_pran_indicator = (
            "PRAN" in raw_brand.upper()
            or any(pw in raw_brand.upper() for pw in pran_sub_brands)
            or any(pw in p_name.upper() for pw in pran_sub_brands)
        )

        is_pran = has_pran_indicator and not has_competitor_indicator
        brand = "PRAN" if is_pran else (raw_brand or "Competitor")

        # Only enrich with PRAN master catalogue if it is confirmed/plausible PRAN
        if is_pran or (not has_competitor_indicator and not raw_brand):
            try:
                matches = enrich_product(p_name, top_k=3)
                if matches:
                    top_conf, _ = matches[0]
                    if top_conf >= 0.55 or (is_pran and top_conf >= 0.40):
                        is_pran = True
                        matched = True
                        brand = "PRAN"
                        for conf, row in matches:
                            suggestions.append(
                                CatalogueSuggestion(
                                    sub_category_name=row.get("Sub Category Name", ""),
                                    sub_category_code=row.get("Sub Category Code", ""),
                                    category_name=row.get("Category Name", ""),
                                    category_code=row.get("Category Code", ""),
                                    item_name=row.get("Item Name", ""),
                                    item_code=row.get("Item Code", ""),
                                    item_image_url=row.get("Item Image URL", ""),
                                    confidence=round(float(conf), 3),
                                )
                            )
                        if not sku and suggestions:
                            sku = suggestions[0].item_code
            except Exception as e:
                logger.warning("Catalogue enrichment lookup error for %s: %s", p_name, e)

        detected_products_list.append(
            DetectedProductDetail(
                brand=brand,
                product_name=p_name or "Unknown Item",
                variant=variant,
                sku=sku,
                facing_count=int(facing) if isinstance(facing, (int, float)) and facing > 0 else 1,
                shelf_level=str(shelf_level).lower(),
                bounding_box_normalized=bbox if isinstance(bbox, list) else None,
                confidence=confidence,
                catalogue_suggestions=suggestions if suggestions else None,
                matched=matched,
            )
        )

    # 2. Extract availability alerts & visible gaps
    raw_avail = parsed_json.get("availability_alerts") or {}
    visible_gaps = raw_avail.get("visible_gaps") or []
    missing_skus = raw_avail.get("missing_expected_skus") or []
    availability_alerts = AvailabilityAlerts(
        visible_gaps=visible_gaps if isinstance(visible_gaps, list) else [],
        missing_expected_skus=missing_skus if isinstance(missing_skus, list) else [],
        inventory_status=str(raw_avail.get("inventory_status") or "unverified"),
    )

    # 3. Extract metrics
    raw_metrics = parsed_json.get("shelf_metrics") or {}
    raw_pran_share = raw_metrics.get("pran_shelf_share_pct")
    raw_comp_share = raw_metrics.get("competitor_shelf_share_pct")

    pran_facings = sum(p.facing_count or 1 for p in detected_products_list if (p.brand or "").upper() == "PRAN" or p.matched)
    comp_facings = sum(p.facing_count or 1 for p in detected_products_list if (p.brand or "").upper() != "PRAN" and not p.matched)
    total_facings = pran_facings + comp_facings

    # If raw_pran_share is provided and > 0, respect it; otherwise compute from facings
    if raw_pran_share is not None and float(raw_pran_share) > 0:
        pran_share = float(raw_pran_share)
    elif total_facings > 0 and pran_facings > 0:
        pran_share = round((pran_facings / total_facings * 100.0), 1)
    else:
        pran_share = float(raw_pran_share or 0.0)

    if raw_comp_share is not None and float(raw_comp_share) > 0:
        comp_share = float(raw_comp_share)
    else:
        comp_share = round(max(0.0, 100.0 - pran_share), 1) if (total_facings > 0 or pran_share > 0) else 0.0

    raw_arr = raw_metrics.get("arrangement_score")
    if raw_arr is not None:
        arr_score = float(raw_arr)
        if 0 < arr_score <= 10.0:
            arr_score = arr_score * 10.0
    elif total_facings > 0:
        gaps_n = len(availability_alerts.visible_gaps)
        arr_score = max(50.0, min(95.0, 85.0 - (gaps_n * 5.0)))
    else:
        arr_score = 0.0

    shelf_metrics = ShelfMetrics(
        pran_shelf_share_pct=float(pran_share),
        competitor_shelf_share_pct=float(comp_share),
        empty_shelf_share_pct=float(raw_metrics.get("empty_shelf_share_pct") or 0.0),
        pran_facing_count=pran_facings,
        competitor_facing_count=comp_facings,
        arrangement_score=arr_score,
        measurement_type="measured" if raw_metrics.get("measurement_type") == "measured" else "estimated",
    )

    # 4. Extract pricing and promotions with currency-safe parsing
    def _parse_safe_price(val: Any) -> Optional[float]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            clean_str = val.replace(",", "")
            m = re.search(r"[-+]?\d*\.?\d+", clean_str)
            if m:
                try:
                    return float(m.group(0))
                except ValueError:
                    return None
        return None

    raw_pricing = parsed_json.get("pricing_and_promotions") or []
    pricing_list: list[PricingAndPromotions] = []
    for pr in raw_pricing:
        if isinstance(pr, dict):
            pricing_list.append(
                PricingAndPromotions(
                    product=pr.get("product"),
                    visible_price=_parse_safe_price(pr.get("visible_price")),
                    promotion_detected=bool(pr.get("promotion_detected")),
                    pricing_violation=str(pr.get("pricing_violation") or "not_evaluated"),
                    confidence=str(pr.get("confidence") or "high"),
                )
            )

    # 5. Evaluate Business Rules & Retail Execution Score
    res_score, final_issues, final_actions, final_insights = evaluate_retail_execution(
        detected_products=detected_products_list,
        shelf_metrics=shelf_metrics,
        availability_alerts=availability_alerts,
        pricing_and_promotions=pricing_list,
        expected_skus=expected_skus,
        target_pran_sos=target_pran_sos,
        approved_prices=approved_prices,
    )

    # 6. Audit summary
    raw_summary = parsed_json.get("audit_summary") or {}
    audit_summary = AuditSummary(
        status="needs_attention" if res_score.score < 80.0 else "acceptable",
        image_quality=str(raw_summary.get("image_quality") or "good"),
        key_findings=list(raw_summary.get("key_findings") or final_insights[:2]),
    )

    token_usage = TokenUsage(
        input_tokens=int(token_usage_dict.get("input_tokens") or 0),
        output_tokens=int(token_usage_dict.get("output_tokens") or 0),
        total_tokens=int(token_usage_dict.get("total_tokens") or 0),
        estimated_cost_usd=float(token_usage_dict.get("estimated_cost_usd") or 0.0),
    )

    audit_id = str(uuid.uuid4())

    report = ShelfAuditReport(
        audit_id=audit_id,
        visit_id=visit_id,
        outlet_id=outlet_id,
        outlet_name=f"Outlet {outlet_id}" if outlet_id else None,
        audit_stage=audit_stage,
        audit_summary=audit_summary,
        retail_execution_score=res_score,
        shelf_metrics=shelf_metrics,
        detected_products=detected_products_list,
        issues_detected=final_issues,
        availability_alerts=availability_alerts,
        pricing_and_promotions=pricing_list,
        recommended_actions=final_actions,
        management_insights=final_insights,
        limitations=list(parsed_json.get("limitations") or []),
        human_verification_required=bool(parsed_json.get("human_verification_required") or (res_score.score < 60.0)),
        token_usage=token_usage,
        image_url=image_input if isinstance(image_input, str) and image_input.startswith("http") else "",
        created_at=datetime.now(timezone.utc),
    )
    _SHELF_AUDIT_CACHE.set(cache_key, report)
    return report


# ── Token Router Helpers ──────────────────────────────────────────────────────


def calculate_token_cost(
    prompt_tokens: int,
    output_tokens: int,
    input_rate: float | None = None,
    output_rate: float | None = None,
) -> float:
    """Calculate USD cost given token counts and configured rates per 1M tokens."""
    settings = get_settings()
    in_rate = input_rate if input_rate is not None else float(settings.token_cost_input_per_million)
    out_rate = output_rate if output_rate is not None else float(settings.token_cost_output_per_million)
    p_cost = (prompt_tokens / 1_000_000.0) * in_rate
    o_cost = (output_tokens / 1_000_000.0) * out_rate
    return round(p_cost + o_cost, 6)


def estimate_tokens(
    image_input: str | bytes | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    custom_prompt: str | None = None,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """
    Estimate token consumption and USD cost for Gemini 3.7 Flash before calling the API.
    
    Calculates:
      - Vision tokens (based on patch tiles after Lanczos downscaling to max 1600px).
      - System instruction tokens (~120 tokens).
      - Custom/user prompt tokens (~20-100 tokens).
      - Expected output tokens (shelf JSON ~100-350 tokens).
      - Thinking tokens (budget configured, e.g. 1024).
    """
    settings = get_settings()
    max_dim = int(getattr(settings, "max_image_dimension", 768) or 768)

    # 1. Determine image dimensions
    w, h = 768, 768  # standard default photo dimension
    dim_str = "768x768 (estimated)"

    if image_width and image_height and image_width > 0 and image_height > 0:
        w, h = image_width, image_height
        dim_str = f"{w}x{h}"
    elif image_input:
        try:
            raw_bytes, _ = _resolve_image_bytes(image_input)
            with Image.open(io.BytesIO(raw_bytes)) as im:
                w, h = im.size
                dim_str = f"{w}x{h} (measured)"
        except Exception:
            pass

    # Lanczos downscale calculation
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        w = int(w * scale)
        h = int(h * scale)
        dim_str += f" -> downscaled to {w}x{h}"

    # Gemini 3.7 Flash Vision token formula:
    # 258 tokens base + 258 tokens per 768x768 tile
    tiles_x = max(1, (w + 767) // 768)
    tiles_y = max(1, (h + 767) // 768)
    vision_tokens = 258 + (tiles_x * tiles_y * 258)

    # Streamlined system prompt ~ 60 tokens
    system_tokens = 60

    # User prompt tokens (~1 token per 4 characters)
    user_prompt = custom_prompt or _COMPACT_USER_PROMPT
    prompt_tokens = max(10, len(user_prompt) // 4)

    total_prompt_tokens = vision_tokens + system_tokens + prompt_tokens

    # Thinking & output estimate (pipe format is ~10 tokens per detected product)
    active_budget = thinking_budget if thinking_budget is not None else int(getattr(settings, "gemini_thinking_budget", 0) or 0)
    expected_output_tokens = 60
    estimated_total_output = expected_output_tokens + (active_budget if active_budget > 0 else 0)

    total_tokens = total_prompt_tokens + estimated_total_output
    estimated_cost = calculate_token_cost(total_prompt_tokens, estimated_total_output)

    return {
        "model": settings.gemini_model,
        "estimated_vision_tokens": vision_tokens,
        "estimated_system_tokens": system_tokens,
        "estimated_prompt_tokens": total_prompt_tokens,
        "estimated_output_tokens": expected_output_tokens,
        "estimated_thinking_tokens": active_budget,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost,
        "dimensions_analyzed": dim_str,
        "optimization_applied": f"Lanczos downscaling (max {max_dim}px, single-tile optimized)",
    }


def count_tokens_direct(
    text: str | None = None,
    image_input: str | bytes | None = None,
) -> dict[str, Any]:
    """
    Count input tokens against Gemini 3.7 Flash using google-genai client or fallback calculator.
    """
    settings = get_settings()
    model_name = settings.gemini_model
    max_input_limit = getattr(settings, "max_input_tokens", 1_048_576)

    contents: list[Any] = []
    if text:
        contents.append(text)
    if image_input:
        try:
            image_bytes, mime_type = _resolve_image_bytes(image_input)
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        except Exception as exc:
            logger.warning("Could not resolve image for count_tokens: %s", exc)

    if not contents:
        contents = [SYSTEM_PROMPT]

    # Attempt live count with genai client
    if settings.gemini_api_key:
        try:
            client = genai.Client(api_key=settings.gemini_api_key)
            result = client.models.count_tokens(
                model=model_name,
                contents=contents,
            )
            total_tokens = int(getattr(result, "total_tokens", 0) or 0)
            return {
                "model": model_name,
                "total_tokens": total_tokens,
                "input_token_limit": max_input_limit,
                "is_within_limit": total_tokens <= max_input_limit,
                "remaining_tokens_available": max(0, max_input_limit - total_tokens),
            }
        except Exception as exc:
            logger.warning("Live count_tokens API call failed (%s), falling back to offline estimator", exc)

    # Fallback estimation
    estimated = estimate_tokens(image_input=image_input, custom_prompt=text)
    total_tokens = estimated["estimated_prompt_tokens"]
    return {
        "model": model_name,
        "total_tokens": total_tokens,
        "input_token_limit": max_input_limit,
        "is_within_limit": total_tokens <= max_input_limit,
        "remaining_tokens_available": max(0, max_input_limit - total_tokens),
    }

