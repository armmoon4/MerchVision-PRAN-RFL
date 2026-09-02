"""
app/services/ai_service.py — Calls OpenRouter/Gemini to detect PRAN-RFL products.

Design notes:
  - Uses the openai SDK pointed at OpenRouter's OpenAI-compatible base URL.
  - Sends the image as a URL in a vision-capable user message.
  - Instructs the model (system prompt) to return ONLY a JSON array — no prose.
  - Strips markdown code fences before json.loads (model sometimes ignores instructions).
  - Temperature 0.1: this is an extraction task, not creative generation.
  - Raises AIServiceError on timeout, bad JSON, or non-array result.
"""
import base64
import json
import mimetypes
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI, APITimeoutError, APIStatusError

from app.config import get_settings


def _prepare_image_payload_url(image_url: str) -> str:
    """
    If image_url is a localhost URL or local path, convert to a base64 data URI
    so OpenRouter / Gemini can read the image directly from the payload.
    """
    parsed = urlparse(image_url)
    if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "testserver") or not parsed.scheme:
        path_str = parsed.path.lstrip("/")
        candidate_paths = [
            Path(path_str),
            Path("media") / path_str.replace("media/", "", 1),
            Path("media") / path_str,
        ]
        for p in candidate_paths:
            if p.exists() and p.is_file():
                mime, _ = mimetypes.guess_type(str(p))
                mime = mime or "image/jpeg"
                encoded = base64.b64encode(p.read_bytes()).decode("utf-8")
                return f"data:{mime};base64,{encoded}"
    return image_url

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a retail shelf auditor for PRAN-RFL products.

Your job: analyze the provided image of a product rack and identify every \
PRAN-RFL product that is visible.

PRAN-RFL products include (but are not limited to):
- PRAN juices, drinks, flavored water, dairy, snacks, noodles, chips, biscuits
- RFL plastic products: water bottles, containers, kitchenware, storage items

Return your answer as a **raw JSON array only** — no markdown, no code fences, \
no explanation, no prose. Each element must have exactly two keys:
  "product_name"      : string  — the full product name and variant (e.g. "PRAN Mango Juice 250ml")
  "quantity_visible"  : integer or null — number of units clearly visible on the rack

Example (do NOT include this in your response):
[
  {"product_name": "PRAN Mango Juice", "quantity_visible": 6},
  {"product_name": "RFL Water Bottle",    "quantity_visible": 3}
]

If no PRAN-RFL products are visible, return an empty array: []
"""

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# ── Errors ────────────────────────────────────────────────────────────────────


class AIServiceError(Exception):
    """Raised when the AI call fails for any reason."""


# ── Main function ─────────────────────────────────────────────────────────────


def analyze_rack_image(image_url: str) -> dict[str, Any]:
    """
    Send *image_url* to Gemini via OpenRouter and return the parsed product list
    along with token usage metrics and estimated cost.

    Args:
        image_url: Publicly accessible URL or local path of the rack photo.

    Returns:
        Dict with keys:
            - "products": list of {"product_name", "quantity_visible"}
            - "token_usage": {"input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd"}
            - "raw_text": raw completion text from model

    Raises:
        AIServiceError: on network timeout, API error, invalid JSON, or
                        non-array response from the model.
    """
    settings = get_settings()

    if not settings.openrouter_api_key:
        raise AIServiceError(
            "OPENROUTER_API_KEY is not set. Add it to your .env file."
        )

    client = OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=float(settings.openrouter_timeout_seconds),
    )

    payload_image_url = _prepare_image_payload_url(image_url)

    try:
        response = client.chat.completions.create(
            model=settings.openrouter_model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": payload_image_url},
                        },
                        {
                            "type": "text",
                            "text": "Identify all PRAN-RFL products visible on this rack.",
                        },
                    ],
                },
            ],
        )
    except APITimeoutError as exc:
        raise AIServiceError(
            f"OpenRouter request timed out after {settings.openrouter_timeout_seconds}s"
        ) from exc
    except APIStatusError as exc:
        raise AIServiceError(
            f"OpenRouter API error {exc.status_code}: {exc.message}"
        ) from exc
    except Exception as exc:
        raise AIServiceError(f"Unexpected error calling OpenRouter: {exc}") from exc

    # ── Extract token usage & calculate cost ──────────────────────────────────
    usage_data = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage_data, "prompt_tokens", 0) or 0) if usage_data else 0
    completion_tokens = int(getattr(usage_data, "completion_tokens", 0) or 0) if usage_data else 0
    total_tokens = int(getattr(usage_data, "total_tokens", prompt_tokens + completion_tokens) or 0) if usage_data else (prompt_tokens + completion_tokens)

    prompt_cost = (prompt_tokens / 1_000_000.0) * float(settings.token_cost_input_per_million)
    completion_cost = (completion_tokens / 1_000_000.0) * float(settings.token_cost_output_per_million)
    estimated_cost_usd = round(prompt_cost + completion_cost, 6)

    token_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }

    # ── Parse the model's text output ─────────────────────────────────────────
    raw_text: str = response.choices[0].message.content or ""
    raw_text_stripped = raw_text.strip()

    # Strip markdown code fences if present
    fence_match = _FENCE_PATTERN.search(raw_text_stripped)
    json_text = fence_match.group(1).strip() if fence_match else raw_text_stripped

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        snippet = raw_text_stripped[:300]
        raise AIServiceError(
            f"Model returned invalid JSON. Parse error: {exc}. "
            f"Raw response snippet: {snippet!r}"
        ) from exc

    if not isinstance(parsed, list):
        snippet = raw_text_stripped[:300]
        raise AIServiceError(
            f"Model response is not a JSON array. "
            f"Raw response snippet: {snippet!r}"
        )

    # Normalize each entry (ensure required keys exist)
    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "product_name": str(item.get("product_name", "Unknown Product")),
                "quantity_visible": item.get("quantity_visible"),
            }
        )

    return {
        "products": normalized,
        "token_usage": token_usage,
        "raw_text": raw_text,
    }
