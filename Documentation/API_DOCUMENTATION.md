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

### Example 2 — Analyze via cURL (JSON Base64 / S3 URL):
```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://my-bucket.s3.amazonaws.com/racks/shelf_01.jpg"
  }'
```
