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
import io
import json
import logging
import os
import tempfile
import time
import re
import socket
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
from app.services.items_db_service import enrich_products, load_items_db


# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a retail shelf auditor for PRAN and RFL products.

Your job: analyze the provided image of a product rack and identify every PRAN or RFL product that is visible.

PRAN and RFL brands include (but are not limited to):
- PRAN juices, drinks, Frooto, Drinko, flavored water, dairy, Lassi, Moo, snacks, noodles, Mr. Noodles,
  chips, biscuits, Bisk Club, spices, sauces, pickles/achar, Fit Crackers, All Time, Potata
- RFL plastics, housewares, and any RFL-branded consumer goods visible on shelf

Return your answer as **plain text only** — one product per line in this exact format:
  product name|quantity

Rules:
- product name: full name with brand + variant + size if visible (e.g. PRAN Mango Juice 250ml)
- quantity: integer — best estimate of units clearly visible; use 1 if unsure
- NO markdown, NO JSON, NO code fences, NO explanation, NO blank lines between entries
- If nothing is visible, output a single empty line

Example output (do NOT copy this into your response):
PRAN Mango Juice 250ml|6
Bisk Club Cream Biscuit Chocolate 90g|4
PRAN Lassi Strawberry 200ml|3
"""

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


def _optimize_image_bytes(raw_data: bytes, max_dim: int = 1024) -> tuple[bytes, str]:
    """
    Downscale oversized camera photos to a max dimension (e.g. 1024px) using Lanczos.
    Preserves fine text sharpness while cutting vision tile count and token costs by ~50%.
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
                im.save(buf, format="JPEG", quality=88, optimize=True)
                opt_data = buf.getvalue()
                logger.info(
                    "Optimized image from %dx%d (%d bytes) to %dx%d (%d bytes)",
                    w, h, len(raw_data), im.size[0], im.size[1], len(opt_data),
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


_COMPACT_USER_PROMPT = "List all PRAN and RFL products visible. One per line as: product name|quantity"


def _call_openrouter(
    image_bytes: bytes,
    mime_type: str,
    prompt: str = _COMPACT_USER_PROMPT,
) -> tuple[str, dict[str, Any]]:
    """
    Call OpenRouter (https://openrouter.ai/api/v1) with google/gemini-3.7-flash using OpenAI SDK.
    """
    settings = get_settings()
    api_key = settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key or "YOUR_OPENROUTER" in api_key:
        raise AIServiceError("OPENROUTER_API_KEY is not set. Add it to your .env file.")

    base64_img = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_img}"

    client = OpenAI(
        base_url=settings.openrouter_base_url or "https://openrouter.ai/api/v1",
        api_key=api_key,
        timeout=float(getattr(settings, "openrouter_timeout_seconds", 45) or 45),
    )

    model_name = settings.openrouter_model or "google/gemini-3.7-flash"
    logger.info("Calling OpenRouter with model: %s (base_url: %s)", model_name, settings.openrouter_base_url)

    max_retries = 3
    retry_delay = 3.0
    last_exc: Exception | None = None

    effort = str(getattr(settings, "openrouter_reasoning_effort", "none") or "none").lower()
    max_r_tokens = getattr(settings, "openrouter_reasoning_max_tokens", 0)
    # Models like google/gemini-3.7-flash mandate reasoning on OpenRouter and reject effort='none'
    # with "Reasoning is mandatory for this endpoint and cannot be disabled."
    # Using effort='minimal' satisfies OpenRouter while yielding 0 reasoning tokens.
    if "gemini-3.7" in model_name.lower() and effort == "none":
        effort = "minimal"

    for attempt in range(1, max_retries + 1):
        try:
            extra_body: dict[str, Any] = {}
            if effort in ("none", "minimal", "low", "medium", "high"):
                extra_body["reasoning"] = {"effort": effort}
            elif max_r_tokens is not None and max_r_tokens >= 0:
                extra_body["reasoning"] = {"max_tokens": int(max_r_tokens)}

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
                "temperature": float(getattr(settings, "openrouter_temperature", 0.1)),
                # Penalise repetition so the model can't loop on the same line (e.g. 55x 'PRAN Hot Snacks|1')
                "frequency_penalty": 0.2,
                # Hard ceiling; pipe format needs ~250 tokens even for a full rack
                "max_tokens": int(getattr(settings, "openrouter_max_tokens", 600)),
            }
            if extra_body:
                create_kwargs["extra_body"] = extra_body

            response = client.chat.completions.create(**create_kwargs)
            raw_text = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            # Actual output/completion tokens billed by OpenRouter (visible text + reasoning)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else (prompt_tokens + output_tokens)

            # Use OpenRouter's exact billed USD cost if provided directly in usage
            direct_cost = getattr(usage, "cost", None)
            if direct_cost is not None and isinstance(direct_cost, (int, float)) and direct_cost >= 0:
                cost = round(float(direct_cost), 6)
            else:
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
            if "Reasoning is mandatory" in str(exc) and effort != "minimal":
                logger.warning("Endpoint mandates reasoning, retrying with effort='minimal'...")
                effort = "minimal"
                continue
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
    logger.info("Calling Gemini direct model: %s (thinking_budget=%s)", model_name, thinking_budget)

    max_retries = 3
    retry_delay = 5.0
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    image_part,
                    "List all PRAN and RFL products visible. One per line as: product name|quantity",
                ],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=1,
                    # No response_mime_type: we use compact pipe-delimited plain text
                    # (incompatible with application/json mode) which saves ~64% output tokens.
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

    # System prompt ~ 125 tokens
    system_tokens = 125

    # User prompt tokens (~1 token per 4 characters)
    user_prompt = custom_prompt or "Identify all PRAN products visible in this image. Return the raw JSON array only."
    prompt_tokens = max(15, len(user_prompt) // 4)

    total_prompt_tokens = vision_tokens + system_tokens + prompt_tokens

    # Thinking & output estimate
    active_budget = thinking_budget if thinking_budget is not None else int(getattr(settings, "gemini_thinking_budget", 128) or 128)
    expected_output_tokens = 200
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
        "optimization_applied": f"Lanczos downscaling (max {max_dim}px)",
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

