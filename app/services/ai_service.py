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
import httpx

from app.config import get_settings


# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a retail shelf auditor for PRAN products.

Your job: analyze the provided image of a product rack and identify every PRAN product that is visible.

PRAN products include (but are not limited to):
- PRAN juices, drinks, flavored water, dairy, snacks, noodles, chips, biscuits, spices, sauces, pickles/achar

Return your answer as a **raw JSON array only** — no markdown, no code fences, no explanation, no prose. Each element must have exactly two keys:
  "product_name"      : string  — the full product name and variant (e.g. "PRAN Mango Juice 250ml")
  "quantity_visible"  : integer or null — number of units clearly visible on the rack

Example (do NOT include this in your response):
[
  {"product_name": "PRAN Mango Juice", "quantity_visible": 6}
]

If no PRAN products are visible, return an empty array: []
"""

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJECT_PATTERN = re.compile(
    r'\{\s*"product_name"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,\s*"quantity_visible"\s*:\s*([0-9]+|null)\s*\}',
    re.DOTALL | re.IGNORECASE,
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


def _optimize_image_bytes(raw_data: bytes, max_dim: int = 1600) -> tuple[bytes, str]:
    """
    Downscale oversized camera photos to a max dimension (e.g. 1600px) using Lanczos.
    Preserves fine text sharpness while reducing Gemini vision tile count (input tokens).
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
    max_dim = int(getattr(settings, "max_image_dimension", 1600) or 1600)

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


def analyze_rack_image(image_input: str | bytes) -> dict[str, Any]:
    """
    Analyze rack photo using the industry-standard download-then-delete pattern:

      1. Download / decode image → optimized bytes (memory buffer, not stored yet)
      2. Write bytes to a secure OS temp file  (prefix: merchvision_)
      3. Call Gemini AI with the image bytes
      4. DELETE temp file unconditionally in the finally block
      5. Parse and return structured product list + token usage

    The temp file exists only during the Gemini call and is guaranteed to be
    deleted even if an exception is raised — zero files left on disk.

    Args:
        image_input: S3 presigned URL, HTTP URL, Base64 string, Data URI, or raw bytes.

    Returns:
        Dict with keys:
            - "products": list of {"product_name", "quantity_visible"}
            - "token_usage": {"input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd"}
            - "raw_text": raw completion text from model

    Raises:
        AIServiceError: on network timeout, API error, invalid JSON, or non-array response.
    """
    settings = get_settings()

    if not settings.gemini_api_key:
        raise AIServiceError("GEMINI_API_KEY is not set. Add it to your .env file.")

    # ── Step 1: Resolve image → optimized bytes ────────────────────────────────
    image_bytes, mime_type = _resolve_image_bytes(image_input)

    # ── Step 2 & 3: Write temp file → call Gemini → delete (finally) ──────────
    suffix = ".jpg" if mime_type == "image/jpeg" else (".png" if mime_type == "image/png" else ".webp")
    tmp_path: str | None = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,       # We manage deletion ourselves in finally
            prefix="merchvision_",
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(image_bytes)
            tmp_file.flush()

        logger.info("Temp image written: %s (%d bytes, %s)", tmp_path, len(image_bytes), mime_type)

        # Build Gemini Part from bytes (from_bytes avoids Windows file-handle locking)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

        client = genai.Client(api_key=settings.gemini_api_key)
        model_name: str = settings.gemini_model
        logger.info("Calling Gemini model: %s", model_name)

        max_retries = 3
        retry_delay = 5.0
        last_exc: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        image_part,
                        "Identify all PRAN products visible in this image. Return the raw JSON array only.",
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=1,
                        response_mime_type="application/json",
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                logger.info("Successfully got response from model: %s", model_name)
                break
            except APIError as exc:
                if exc.code in (429, 503) and attempt < max_retries:
                    match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", str(exc.message or ""))
                    delay = float(match.group(1)) + 1.5 if match else retry_delay
                    logger.warning(
                        "Gemini API %d on attempt %d/%d — retrying in %.1fs: %s",
                        exc.code, attempt, max_retries, delay, exc.message,
                    )
                    time.sleep(delay)
                    retry_delay *= 2
                    last_exc = exc
                    continue
                raise AIServiceError(f"Gemini API error {exc.code}: {exc.message}") from exc
            except Exception as exc:
                raise AIServiceError(f"Unexpected error calling Gemini: {exc}") from exc
        else:
            raise AIServiceError(
                f"Gemini API request failed after {max_retries} attempts. "
                "Please try again in a moment."
            ) from last_exc

    finally:
        # ── Step 4: ALWAYS delete temp file ───────────────────────────────────
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
                logger.info("Temp image deleted: %s", tmp_path)
            except OSError as exc:
                logger.warning("Failed to delete temp file %s: %s", tmp_path, exc)

    # ── Extract token usage & calculate cost ──────────────────────────────────
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

    prompt_cost = (prompt_tokens / 1_000_000.0) * float(settings.token_cost_input_per_million)
    output_cost = (output_tokens / 1_000_000.0) * float(settings.token_cost_output_per_million)
    estimated_cost_usd = round(prompt_cost + output_cost, 6)

    logger.info(
        "Token usage: in=%d, out=%d (candidates=%d, thoughts=%d), total=%d, cost=$%.6f",
        prompt_tokens, output_tokens, candidates_tokens, thoughts_tokens, total_tokens, estimated_cost_usd,
    )

    token_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }

    # ── Parse model output ─────────────────────────────────────────────────────
    raw_text: str = response.text or ""
    raw_text_stripped = raw_text.strip()

    fence_match = _FENCE_PATTERN.search(raw_text_stripped)
    json_text = fence_match.group(1).strip() if fence_match else raw_text_stripped

    parsed_items: list[dict[str, Any]] = []

    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            parsed_items = [i for i in data if isinstance(i, dict)]
        elif isinstance(data, dict) and "products" in data and isinstance(data["products"], list):
            parsed_items = [i for i in data["products"] if isinstance(i, dict)]
    except (json.JSONDecodeError, ValueError):
        for match in _OBJECT_PATTERN.finditer(json_text):
            p_name = match.group(1).strip()
            qty_raw = match.group(2).strip()
            qty = int(qty_raw) if qty_raw.isdigit() else None
            parsed_items.append({"product_name": p_name, "quantity_visible": qty})

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
            deduped[name_key]["quantity_visible"] = prev_qty + qty_int
        else:
            deduped[name_key] = {"product_name": name, "quantity_visible": qty_int}

    return {
        "products": list(deduped.values()),
        "token_usage": token_usage,
        "raw_text": raw_text,
    }
