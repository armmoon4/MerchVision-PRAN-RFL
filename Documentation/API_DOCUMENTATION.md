# Prism — PRAN-RFL Rack Recognition & Token Cost Analytics API Documentation

Comprehensive API documentation for the **PRAN-RFL Rack Recognition System** backend service with **Zero Local Disk Storage (100% In-Memory Vision Pipeline)** and integrated **Token Usage & Cost Analysis**.

---

## 1. Overview & Base URLs

The Prism API processes shelf and rack photos provided as **S3 Presigned URLs**, **Remote HTTP/HTTPS URLs**, **Raw Base64 strings (e.g. `/9j/...`)**, or **Binary Streams**. 

It identifies PRAN-RFL product SKUs, calculates visible unit quantities, and returns exact **input tokens**, **output tokens**, **total tokens**, and **estimated USD cost** using Google Gemini 3.7 Flash vision models.

### Key Architectural Highlights:
- **Zero Local Disk Footprint**: Images are processed directly in RAM (`io.BytesIO`) and streamed to Google Gemini without downloading, caching, or saving image files locally.
- **Flexible Image Inputs**: Supports pure Base64 strings (e.g. `/9j/4AAQ...`), Data URIs (`data:image/jpeg;base64,...`), S3 URLs, and direct file uploads.
- **In-Memory Optimization**: Resizes images in memory to optimize Gemini vision tile tokens before transmission.

| Environment | Base URL | Notes |
|---|---|---|
| **Local (SQLite)** | `http://localhost:8000` | Default dev environment |
| **Docker Compose** | `http://localhost:8000` | Backend with PostgreSQL 16 |
| **Interactive Swagger UI** | `http://localhost:8000/docs` | Live testing & OpenAPI spec |
| **ReDoc UI** | `http://localhost:8000/redoc` | Formatted API reference |
| **Web Dashboard** | `http://localhost:8000/` or `/ui` | Graphical test UI & Token Analytics |

---

## 2. Token Pricing & Cost Calculation

For every image processed by Gemini 3.7 Flash, the system tracks token usage directly from the LLM completion response and calculates estimated costs:

$$\text{Estimated Cost (USD)} = \left(\frac{\text{Input Tokens}}{1,000,000} \times \text{Rate}_{\text{input}}\right) + \left(\frac{\text{Output Tokens}}{1,000,000} \times \text{Rate}_{\text{output}}\right)$$

### Default Configured Rates:
- **Input (Prompt) Rate:** `$0.10` per 1,000,000 tokens
- **Output (Completion) Rate:** `$0.40` per 1,000,000 tokens
- Configurable in `.env` via `TOKEN_COST_INPUT_PER_MILLION` and `TOKEN_COST_OUTPUT_PER_MILLION`.

---

## 3. Endpoints Reference

### 3.1. System & Health

#### `GET /health`
Liveness probe to verify server availability.

- **URL:** `/health`
- **Method:** `GET`
- **Authentication:** None

**Response `200 OK`:**
```json
{
  "status": "ok"
}
```

---

### 3.2. Single-Call Direct Analysis (1 API Request, Download-Then-Delete)

#### `POST /analyze` (or `/analyze/url`)
Analyzes a rack image from an **S3 URL, HTTP/HTTPS URL, or Base64 string** directly in **one single synchronous API call**. The server downloads the image to a secure temp file, runs Gemini AI vision recognition, deletes the temp file immediately, and returns the analysis results.

- **URL:** `/analyze` (Aliases: `/api/analyze`, `/analyze/url`, `/uploads/analyze`)
- **Method:** `POST`
- **Content-Type:** `application/json`

**JSON Request Body Schema (`AnalyzeRequest`):**

| Field | Type | Required | Description |
|---|---|---|---|
| `image_url` | String | **Yes** | S3 presigned URL, HTTP/HTTPS URL, Data URI, or pure Base64 image string (e.g. `/9j/...`). |

*(Note: `image` is also supported as an alias for `image_url`)*

**Example 1 — S3 Presigned URL Payload:**
```json
{
  "image_url": "https://my-bucket.s3.amazonaws.com/racks/shelf_01.jpg"
}
```

**Example 2 — Pure Base64 Payload:**
```json
{
  "image_url": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQDx..."
}
```

**Response `200 OK`:**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "COMPLETED",
  "shop_id": null,
  "merchandiser_id": null,
  "image_url": "https://my-bucket.s3.amazonaws.com/racks/shelf_01.jpg",
  "detected_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "quantity_visible": 6
    },
    {
      "product_name": "PRAN Lassi 200ml",
      "quantity_visible": 4
    },
    {
      "product_name": "RFL Water Bottle 1L",
      "quantity_visible": 2
    }
  ],
  "token_usage": {
    "input_tokens": 1280,
    "output_tokens": 94,
    "total_tokens": 1374,
    "estimated_cost_usd": 0.000166
  },
  "input_tokens": 1280,
  "output_tokens": 94,
  "total_tokens": 1374,
  "estimated_cost_usd": 0.000166,
  "error_message": null,
  "created_at": "2026-09-22T09:30:00Z"
}
```

---

#### `POST /analyze/file`
Uploads and streams a binary image file directly in RAM for instant 1-call analysis (zero disk writes).

- **URL:** `/analyze/file` (Alias: `/analyze/upload`)
- **Method:** `POST`
- **Content-Type:** `multipart/form-data`

**Form Parameters:**
- `file`: Binary file (`image/jpeg`, `image/png`, `image/webp`).
- `shop_id`: String (optional).
- `merchandiser_id`: String (optional).

---

### 3.3. Asynchronous Rack Uploads & Polling (Zero Disk Storage)

#### `POST /uploads` / `POST /uploads/url`
Enqueues an in-memory background AI recognition task and returns HTTP 202 immediately.

- **URL:** `/uploads/url` (JSON) or `/uploads` (Multipart File)
- **Method:** `POST`

**Response `202 Accepted`:**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "PENDING",
  "message": "Image input received. Processing started."
}
```

---

#### `GET /uploads/{upload_id}` (Alias: `GET /uploads/{upload_id}/result`)
Retrieves the status, detected products, and token analysis for a specific upload ID.

- **URL:** `/uploads/{upload_id}`
- **Method:** `GET`

**Response `200 OK` (Completed):**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "COMPLETED",
  "shop_id": null,
  "merchandiser_id": null,
  "image_url": "base64_in_memory",
  "detected_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "quantity_visible": 6
    }
  ],
  "input_tokens": 1280,
  "output_tokens": 94,
  "total_tokens": 1374,
  "estimated_cost_usd": 0.000166,
  "token_usage": {
    "input_tokens": 1280,
    "output_tokens": 94,
    "total_tokens": 1374,
    "estimated_cost_usd": 0.000166
  },
  "error_message": null,
  "created_at": "2026-09-22T09:30:00Z",
  "updated_at": "2026-09-22T09:30:03Z"
}
```

---

#### `GET /uploads/summary` (Alias: `GET /analysis/summary`)
Returns aggregate metrics across all scanned racks, including total input tokens, output tokens, total tokens, total USD cost, and top detected items.

**Response `200 OK`:**
```json
{
  "total_scans": 48,
  "completed_scans": 45,
  "processing_scans": 1,
  "pending_scans": 0,
  "failed_scans": 2,
  "total_products_detected": 312,
  "unique_products_count": 24,
  "total_input_tokens": 58400,
  "total_output_tokens": 4230,
  "total_tokens": 62630,
  "total_estimated_cost_usd": 0.007532,
  "avg_tokens_per_scan": 1391.78,
  "avg_cost_per_scan_usd": 0.000167,
  "top_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "total_quantity": 84,
      "scan_appearances": 28
    }
  ],
  "recent_uploads": [...]
}
```

---

---

### 3.4. OpenRouter & Token Router API (`/tokens`)

The Token Router API manages token quotas, model capability queries, pre-inference token estimation, live token counting, and payload routing through **OpenRouter** (`https://openrouter.ai/api/v1`) using the model ID `google/gemini-3.7-flash`.

#### `GET /tokens/models`
Returns model specifications, token boundaries, thinking capabilities, and feature support for **google/gemini-3.7-flash** on OpenRouter.

**Response `200 OK`:**
```json
{
  "provider": "openrouter",
  "model_code": "google/gemini-3.7-flash",
  "openrouter_endpoint": "https://openrouter.ai/api/v1/chat/completions",
  "version": "Stable: gemini-3.7-flash (OpenRouter: google/gemini-3.7-flash)",
  "latest_update": "August 2026",
  "input_token_limit": 1048576,
  "output_token_limit": 65536,
  "supported_inputs": ["Text", "Image", "Video", "Audio", "PDF"],
  "supported_outputs": ["Text"],
  "capabilities": {
    "audio_generation": false,
    "caching": true,
    "code_execution": true,
    "computer_use": "Supported (Preview)",
    "file_search": true,
    "function_calling": true,
    "grounding_with_google_maps": true,
    "image_generation": false,
    "live_api": false,
    "search_grounding": true,
    "structured_outputs": true,
    "thinking": "Supported (low, medium, high)",
    "url_context": true,
    "batch_api": true,
    "flex_inference": true,
    "priority_inference": true
  },
  "active_thinking_budget": 1024,
  "active_thinking_level": "medium",
  "pricing_input_per_million": 0.75,
  "pricing_output_per_million": 3.75
}
```

---

#### `GET /tokens/pricing`
Returns active token pricing rates per 1,000,000 tokens and 1,000 tokens (OpenRouter rates: $0.75/1M input, $3.75/1M output).

**Response `200 OK`:**
```json
{
  "provider": "openrouter",
  "model": "google/gemini-3.7-flash",
  "currency": "USD",
  "input_cost_per_million": 0.75,
  "output_cost_per_million": 3.75,
  "input_cost_per_1k": 0.00075,
  "output_cost_per_1k": 0.00375,
  "formula": "cost = (input_tokens / 1,000,000 * input_rate) + (output_tokens / 1,000,000 * output_rate)"
}
```

---

#### `POST /tokens/chat`
Execute direct chat completions with `google/gemini-3.7-flash` via OpenRouter.

**Request Body:**
```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful software engineering assistant."},
    {"role": "user", "content": "Explain Oracle database indexing in simple terms."}
  ]
}
```

**Response `200 OK`:**
```json
{
  "provider": "openrouter",
  "model": "google/gemini-3.7-flash",
  "content": "An Oracle database index works like the index at the back of a book...",
  "token_usage": {
    "input_tokens": 28,
    "output_tokens": 142,
    "total_tokens": 170,
    "estimated_cost_usd": 0.0005535
  },
  "status": "success"
}
```

---

#### `GET /tokens/summary` (or `/tokens/stats`)
Aggregates total tokens consumed, overall USD cost, and per-scan averages across all historical requests.

**Response `200 OK`:**
```json
{
  "total_scans": 12,
  "completed_scans": 12,
  "total_input_tokens": 15360,
  "total_output_tokens": 1420,
  "total_tokens": 16780,
  "total_estimated_cost_usd": 0.002104,
  "avg_tokens_per_scan": 1398.33,
  "avg_cost_per_scan_usd": 0.000175,
  "pricing_rates": { ... }
}
```

---

#### `POST /tokens/estimate`
Pre-calculates estimated Gemini vision tokens, system instruction tokens, expected output tokens, and USD cost prior to making a vision call.

**Request Body:**
```json
{
  "image_width": 1920,
  "image_height": 1080,
  "thinking_budget": 1024
}
```

**Response `200 OK`:**
```json
{
  "model": "gemini-3.7-flash",
  "estimated_vision_tokens": 1290,
  "estimated_system_tokens": 125,
  "estimated_prompt_tokens": 1435,
  "estimated_output_tokens": 200,
  "estimated_thinking_tokens": 1024,
  "estimated_total_tokens": 2659,
  "estimated_cost_usd": 0.000633,
  "dimensions_analyzed": "1920x1080 -> downscaled to 1600x900",
  "optimization_applied": "Lanczos downscaling (max 1600px)"
}
```

---

#### `POST /tokens/count`
Counts exact tokens against Gemini 3.7 Flash limits using the Google GenAI SDK token counter.

**Request Body:**
```json
{
  "text": "Identify PRAN juices and quantify visible units."
}
```

**Response `200 OK`:**
```json
{
  "model": "gemini-3.7-flash",
  "total_tokens": 10,
  "input_token_limit": 1048576,
  "is_within_limit": true,
  "remaining_tokens_available": 1048566
}
```

---

#### `POST /tokens/route`
Token-aware router that analyzes input size, checks context boundaries (1,048,576 tokens), and advises optimal routing mode (synchronous, async queue, or batch).

**Request Body:**
```json
{
  "prompt": "Analyze shelf rack photo",
  "priority": "standard"
}
```

**Response `200 OK`:**
```json
{
  "model": "gemini-3.7-flash",
  "route": "synchronous_direct",
  "estimated_input_tokens": 135,
  "configured_thinking_budget": 1024,
  "configured_thinking_level": "medium",
  "max_input_limit": 1048576,
  "max_output_limit": 65536,
  "is_payload_valid": true,
  "recommended_batch_mode": false,
  "message": "Standard payload: routed to immediate synchronous zero-disk vision analysis."
}
```

---

## 4. Code Examples (Python & cURL)

### Example 1 — Analyze S3 / Pure Base64 Payload via Python:
```python
import requests

payload = {
    "image_url": "https://my-bucket.s3.amazonaws.com/racks/shelf_01.jpg"  # or Base64 string "/9j/..."
}

res = requests.post("http://localhost:8000/analyze", json=payload)
data = res.json()

print("Products:", data["detected_products"])
print(f"Tokens: In={data['input_tokens']}, Out={data['output_tokens']}, Total={data['total_tokens']}")
print(f"Cost: ${data['estimated_cost_usd']:.6f}")
```

### Example 2 — Token Estimation via Python:
```python
import requests

res = requests.post("http://localhost:8000/tokens/estimate", json={
    "image_width": 2400,
    "image_height": 1800,
    "thinking_budget": 1024,
})
print("Estimated Token Usage:", res.json())
```

### Example 3 — Analyze via cURL (JSON Base64 / S3 URL):
```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://my-bucket.s3.amazonaws.com/racks/shelf_01.jpg"
  }'
```

### Example 4 — Direct OpenRouter with OpenAI Python SDK:
```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY", "your-openrouter-api-key"),
)

response = client.chat.completions.create(
    model="google/gemini-3.7-flash",
    messages=[
        {
            "role": "user",
            "content": "Explain retail shelf SKU recognition."
        }
    ]
)

print(response.choices[0].message.content)
```

