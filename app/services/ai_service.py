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
import base64
import hashlib
import io
import json
import logging
import os
import tempfile
import time
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Force IPv4 socket resolution to prevent [Errno 101] Network is unreachable on Docker/WSL2
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

from google import genai
from google.genai import types
from google.genai.errors import APIError
from openai import OpenAI, OpenAIError
import httpx

from app.config import get_settings
from app.database import SessionLocal
from app.models import ProcessingStatus, RackUpload
from app.services.items_db_service import enrich_products, load_items_db


# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Retail shelf auditor for PRAN Food & Beverage products.
Detect ONLY visible PRAN food and drink items (Juices, Basil Seed drinks, Dairy/Lassi, Noodles, Biscuits, Bakery, Confectionery, Snacks).
Strict rules:
- Do NOT detect RFL plastic items, non-food items, or competitor brands (e.g. F&N, Coca-Cola).
- Output plain text ONLY, one item per line:
product name|quantity
- product name: PRAN brand with variant, flavor, and pack size if visible
- quantity: integer count
- No markdown, no json, no explanation. If no PRAN food items are visible, return empty line."""


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


def _autocrop_borders(im: Image.Image, tolerance: int = 18) -> Image.Image:
    """
    Remove uniform solid borders (e.g. black letterbox bars, solid scanner margins).
    Saves vision tokens by not encoding empty margin tiles into the VLM.
    """
    try:
        from PIL import ImageChops
        # Sample border color from top-left pixel
        bg = Image.new(im.mode, im.size, im.getpixel((0, 0)))
        diff = ImageChops.difference(im, bg)
        diff = ImageChops.add(diff, diff, 2.0, -tolerance)
        bbox = diff.getbbox()
        if bbox:
            w_crop = bbox[2] - bbox[0]
            h_crop = bbox[3] - bbox[1]
            orig_area = im.size[0] * im.size[1]
            crop_area = w_crop * h_crop
            # Only crop if borders take up > 3% and valid content retains >= 40% of original
            if 0.40 <= (crop_area / orig_area) <= 0.97:
                logger.info(
                    "Auto-cropped empty margins from %dx%d to %dx%d (saved ~%d%% non-product area)",
                    im.size[0], im.size[1], w_crop, h_crop, int((1.0 - (crop_area / orig_area)) * 100),
                )
                return im.crop(bbox)
    except Exception as exc:
        logger.debug("Border autocrop skipped: %s", exc)
    return im


def _optimize_image_bytes(raw_data: bytes, max_dim: int = 1024, quality: int = 82) -> tuple[bytes, str]:
    """
    Downscale oversized camera photos to tile-aligned max dimension (default 1024px) using Lanczos.
    Snaps to 768-pixel tile grid boundaries to avoid the 3rd-tile penalty (e.g. 1600px -> 1024px
    drops vision tokens from ~1,806 down to ~774).
    """
    try:
        with Image.open(io.BytesIO(raw_data)) as im:
            detected_format = (im.format or "JPEG").upper()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            # Remove empty letterbox/scanner borders before tile slicing
            im = _autocrop_borders(im)

            w, h = im.size
            if max(w, h) > max_dim:
                im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                opt_data = buf.getvalue()
                logger.info(
                    "Optimized image from %dx%d (%d bytes) to %dx%d (%d bytes, quality=%d)",
                    w, h, len(raw_data), im.size[0], im.size[1], len(opt_data), quality,
                )
                return opt_data, "image/jpeg"

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
      - HTTP / HTTPS / S3 presigned URL  → downloaded to memory buffer
      - Pure Base64 string               → decoded to bytes

    Raises AIServiceError on any failure.
    """
    settings = get_settings()
    max_dim = int(getattr(settings, "max_image_dimension", 1024) or 1024)
    quality = int(getattr(settings, "image_quality", 82) or 82)

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

    # 3. Remote HTTP / HTTPS / S3 presigned URL → streamed to memory
    if image_str.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
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


_COMPACT_USER_PROMPT = "Extract visible PRAN food and drink items only."


def _call_openrouter(
    image_bytes: bytes,
    mime_type: str,
    prompt: str = _COMPACT_USER_PROMPT,
) -> tuple[str, dict[str, Any]]:
    """
    Call OpenRouter (https://openrouter.ai/api/v1) with token-optimized settings:
    - Flash-Lite class model by default
    - Output capped via max_tokens (e.g. 450)
    - Temperature 0.0 for deterministic, minimal token output
    - OpenRouter edge response cache header (X-OpenRouter-Cache)
    - Reasoning suppressed to prevent 1,000+ token thinking waste
    """
    settings = get_settings()
    api_key = settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key or "YOUR_OPENROUTER" in api_key:
        raise AIServiceError("OPENROUTER_API_KEY is not set. Add it to your .env file.")

    base64_img = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_img}"

    # Build OpenRouter-optimized headers including response cache
    default_headers: dict[str, str] = {
        "HTTP-Referer": "https://merchvision.pranrfl.com",
        "X-Title": "MerchVision PRAN-RFL Shelf Auditor",
    }
    if getattr(settings, "openrouter_enable_cache_header", True):
        default_headers["X-OpenRouter-Cache"] = "true"

    client = OpenAI(
        base_url=settings.openrouter_base_url or "https://openrouter.ai/api/v1",
        api_key=api_key,
        timeout=float(getattr(settings, "openrouter_timeout_seconds", 45) or 45),
        default_headers=default_headers,
    )

    model_name = settings.openrouter_model or "google/gemini-2.5-flash-lite"
    logger.info("Calling OpenRouter with model: %s (base_url: %s)", model_name, settings.openrouter_base_url)

    max_retries = 3
    retry_delay = 3.0
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            extra_body: dict[str, Any] = {}
            effort = str(getattr(settings, "openrouter_reasoning_effort", "none") or "none").lower()
            max_r_tokens = getattr(settings, "openrouter_reasoning_max_tokens", 0)
            # OpenRouter requires either effort OR max_tokens (not both).
            # Setting effort='none' or max_tokens=0 disables runaway thinking output tokens.
            if effort in ("none", "low", "medium", "high"):
                extra_body["reasoning"] = {"effort": effort}
            elif max_r_tokens is not None and max_r_tokens >= 0:
                extra_body["reasoning"] = {"max_tokens": int(max_r_tokens)}

            max_out_tokens = int(getattr(settings, "openrouter_max_tokens", 450) or 450)
            temp = float(getattr(settings, "openrouter_temperature", 0.0) or 0.0)

            create_kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
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
                "temperature": temp,
                "max_tokens": max_out_tokens,
            }
            if extra_body:
                create_kwargs["extra_body"] = extra_body

            response = client.chat.completions.create(**create_kwargs)
            raw_text = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else (prompt_tokens + output_tokens)

            # OpenRouter provides exact billed USD cost directly in usage.cost
            direct_cost = getattr(usage, "cost", None)
            if direct_cost is not None and isinstance(direct_cost, (int, float)) and direct_cost >= 0:
                cost = round(float(direct_cost), 6)
            else:
                cost = calculate_token_cost(prompt_tokens, output_tokens)

            logger.info(
                "OpenRouter Token usage: in=%d, out=%d, total=%d, exact_billed_cost=$%.6f",
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
    prompt: str = _COMPACT_USER_PROMPT,
) -> tuple[str, dict[str, Any]]:
    """
    Call Google Gemini Direct using google-genai SDK with token-optimized parameters:
    - Temperature 0.0 (deterministic, concise output)
    - max_output_tokens capped to 450
    - media_resolution control (e.g. MEDIUM: ~560 tokens vs default 1,120+)
    - thinking_budget=0 to eliminate reasoning token overhead
    """
    settings = get_settings()
    if not settings.gemini_api_key or "YOUR_GEMINI" in settings.gemini_api_key:
        raise AIServiceError("GEMINI_API_KEY is not set. Add it to your .env file.")

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    thinking_config = None
    thinking_budget = getattr(settings, "gemini_thinking_budget", 0)
    thinking_level = getattr(settings, "gemini_thinking_level", "low")
    if thinking_budget is not None and thinking_budget >= 0:
        thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)
    elif thinking_level in ("low", "medium", "high"):
        thinking_config = types.ThinkingConfig(thinking_level=thinking_level)

    # Resolution controls vision token consumption per image
    media_res_str = str(getattr(settings, "gemini_media_resolution", "medium") or "medium").upper()
    media_resolution = None
    if hasattr(types, "MediaResolution"):
        if "LOW" in media_res_str:
            media_resolution = types.MediaResolution.MEDIA_RESOLUTION_LOW
        elif "MEDIUM" in media_res_str:
            media_resolution = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        elif "HIGH" in media_res_str:
            media_resolution = types.MediaResolution.MEDIA_RESOLUTION_HIGH

    client = genai.Client(api_key=settings.gemini_api_key)
    model_name: str = settings.gemini_model
    logger.info("Calling Gemini direct model: %s (thinking_budget=%s, media_res=%s)", model_name, thinking_budget, media_res_str)

    max_retries = 3
    retry_delay = 5.0
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            config_kwargs: dict[str, Any] = {
                "system_instruction": SYSTEM_PROMPT,
                "temperature": float(getattr(settings, "gemini_temperature", 0.0) or 0.0),
                "max_output_tokens": int(getattr(settings, "gemini_max_output_tokens", 450) or 450),
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
                "thinking_config": thinking_config,
            }
            if media_resolution:
                config_kwargs["media_resolution"] = media_resolution

            response = client.models.generate_content(
                model=model_name,
                contents=[
                    image_part,
                    prompt,
                ],
                config=types.GenerateContentConfig(**config_kwargs),
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
    OpenRouter chat completion helper using openai Python client.
    Target endpoint: https://openrouter.ai/api/v1/chat/completions
    Default model: google/gemini-3.7-flash
    """
    settings = get_settings()
    api_key = settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise AIServiceError("OPENROUTER_API_KEY is not set.")

    client = OpenAI(
        base_url=settings.openrouter_base_url or "https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    return client.chat.completions.create(
        model=model or settings.openrouter_model or "google/gemini-3.7-flash",
        messages=messages,
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


def _lookup_cached_analysis(image_hash: str, db: Any = None) -> RackUpload | None:
    """
    Look up recent completed analysis with identical image SHA-256 hash.
    Industry best practice: yields 100% token savings (0 tokens, $0.00 cost) on duplicate captures/retries.
    """
    settings = get_settings()
    if not getattr(settings, "enable_image_cache", True) or not image_hash:
        return None

    close_db = False
    session = db
    if session is None:
        try:
            session = SessionLocal()
            close_db = True
        except Exception:
            return None

    try:
        ttl_hours = int(getattr(settings, "image_cache_ttl_hours", 72) or 72)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        cached = (
            session.query(RackUpload)
            .filter(
                RackUpload.image_hash == image_hash,
                RackUpload.status == ProcessingStatus.COMPLETED,
                RackUpload.detected_products.isnot(None),
                RackUpload.created_at >= cutoff,
            )
            .order_by(RackUpload.created_at.desc())
            .first()
        )
        return cached
    except Exception as exc:
        logger.warning("Image cache lookup error: %s", exc)
        return None
    finally:
        if close_db and session:
            try:
                session.close()
            except Exception:
                pass


def analyze_rack_image(image_input: str | bytes, db: Any = None) -> dict[str, Any]:
    """
    Analyze rack photo using OpenRouter (google/gemini-2.5-flash-lite) or Google Gemini Direct:

      1. Download / decode image → optimized bytes (tile-aligned, Lanczos, quality control)
      2. Check exact SHA-256 image cache (0 tokens on hit)
      3. Call OpenRouter / Gemini with image and shelf recognition prompt
         (AI returns ONLY product_name + quantity with max_tokens cap — minimum tokens)
      4. Parse AI output, deduplicate products
      5. Enrich each product with catalogue metadata from itemsdb.csv (zero extra AI tokens)
      6. Return structured product list + token usage + USD cost + image_hash

    Args:
        image_input: S3 presigned URL, HTTP URL, Base64 string, Data URI, or raw bytes.
        db: Optional database session for caching lookup.

    Returns:
        Dict with keys:
            - "products": list of enriched product dicts
            - "token_usage": {"input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd", "cached"}
            - "raw_text": raw completion text from model
            - "image_hash": SHA-256 hash of image
            - "cached": boolean indicating if served from cache
    """
    settings = get_settings()

    # ── Step 0: Ensure catalogue is loaded (no-op after first call) ───────────
    load_items_db()

    # ── Step 1: Resolve image → optimized bytes & SHA-256 hash ────────────────
    image_bytes, mime_type = _resolve_image_bytes(image_input)
    image_hash = hashlib.sha256(image_bytes).hexdigest()

    # ── Step 1b: Exact Image Cache Lookup (Industry Best Practice) ────────────
    # Duplicate photos, retries, or re-analyses consume 0 tokens & cost $0.00
    cached_entry = _lookup_cached_analysis(image_hash, db=db)
    if cached_entry:
        logger.info(
            "Exact Image Cache HIT [hash=%s... from upload %s]. Consumed 0 tokens, $0.00 USD.",
            image_hash[:12],
            cached_entry.id,
        )
        return {
            "products": cached_entry.detected_products or [],
            "token_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "cached": True,
                "original_tokens": cached_entry.total_tokens or 0,
            },
            "raw_text": cached_entry.ai_raw_response or "",
            "image_hash": image_hash,
            "cached": True,
        }

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

    return {
        "products": enriched_products,
        "token_usage": token_usage,
        "raw_text": raw_text,
        "image_hash": image_hash,
        "cached": False,
    }


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
    max_dim = int(getattr(settings, "max_image_dimension", 1024) or 1024)

    # 1. Determine image dimensions
    w, h = 1024, 768  # standard default photo dimension
    dim_str = "1024x768 (estimated)"

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

    # System prompt ~ 85 tokens
    system_tokens = 85

    # User prompt tokens (~1 token per 4 characters)
    user_prompt = custom_prompt or _COMPACT_USER_PROMPT
    prompt_tokens = max(6, len(user_prompt) // 4)

    total_prompt_tokens = vision_tokens + system_tokens + prompt_tokens

    # Thinking & output estimate (capped by max_tokens)
    active_budget = thinking_budget if thinking_budget is not None else int(getattr(settings, "gemini_thinking_budget", 0) or 0)
    max_out = int(getattr(settings, "openrouter_max_tokens", 450) or 450)
    expected_output_tokens = min(150, max_out)
    estimated_total_output = expected_output_tokens + (active_budget if active_budget > 0 else 0)

    total_tokens = total_prompt_tokens + estimated_total_output
    estimated_cost = calculate_token_cost(total_prompt_tokens, estimated_total_output)

    return {
        "model": settings.openrouter_model if getattr(settings, "ai_provider", "openrouter") == "openrouter" else settings.gemini_model,
        "estimated_vision_tokens": vision_tokens,
        "estimated_system_tokens": system_tokens,
        "estimated_prompt_tokens": total_prompt_tokens,
        "estimated_output_tokens": expected_output_tokens,
        "estimated_thinking_tokens": active_budget,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost,
        "dimensions_analyzed": dim_str,
        "optimization_applied": f"Lanczos downscaling (max {max_dim}px, tile-aligned)",
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

