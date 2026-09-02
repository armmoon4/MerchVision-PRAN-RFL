# Prism — PRAN-RFL Rack Recognition & Token Cost Analytics API Documentation

Comprehensive API documentation for the **PRAN-RFL Rack Recognition System** backend service with integrated **Token Usage & Cost Analysis**.

---

## 1. Overview & Base URLs

The Prism API processes shelf and rack photos taken by retail merchandisers, identifying PRAN-RFL product SKUs, calculating visible unit quantities, and measuring exact **input tokens**, **output tokens**, **total tokens**, and **estimated USD cost** using Google Gemini 3.7 Flash vision models.

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

### 3.2. Single-Call Direct Analysis (1 API Request)

#### `POST /analyze`
Analyzes a rack image file directly in **one single API call** without requiring separate polling. Returns product detections and token usage immediately.

- **URL:** `/analyze` (Alias: `/uploads/analyze`)
- **Method:** `POST`
- **Content-Type:** `multipart/form-data`

**Form Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `file` | Binary File | **Yes** | Image file (`image/jpeg`, `image/png`, `image/webp`). Max size: 10 MB. |
| `shop_id` | String | No | Shop identifier (e.g. `SHOP-Gulshan-102`). |
| `merchandiser_id` | String | No | Merchandiser identifier (e.g. `MER-Rahim-45`). |

**Example Request (cURL):**
```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@/path/to/rack_shelf.jpg" \
  -F "shop_id=SHOP-102" \
  -F "merchandiser_id=MER-45"
```

**Response `200 OK`:**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "COMPLETED",
  "shop_id": "SHOP-102",
  "merchandiser_id": "MER-45",
  "image_url": "http://localhost:8000/media/uploads/SHOP-102/ab62985150b840a480a575b828fb1c49.jpg",
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
  "created_at": "2026-09-02T08:50:00Z"
}
```

---

#### `POST /analyze/url`
Analyzes a rack image from a public URL in **one single API call**.

- **URL:** `/analyze/url`
- **Method:** `POST`
- **Content-Type:** `application/json`

**JSON Request Body:**
```json
{
  "image_url": "https://example.com/rack_shelf.jpg",
  "shop_id": "SHOP-102",
  "merchandiser_id": "MER-45"
}
```

**Response `200 OK`:** Same structure as `POST /analyze`.

---

### 3.3. Asynchronous Rack Uploads & Polling

#### `POST /uploads`
Uploads a rack image and enqueues a background AI recognition job.

- **URL:** `/uploads`
- **Method:** `POST`
- **Content-Type:** `multipart/form-data`

**Response `202 Accepted`:**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "PENDING",
  "message": "Image received. Processing started."
}
```

---

#### `POST /uploads/url`
Submits an image URL and enqueues background processing.

- **URL:** `/uploads/url`
- **Method:** `POST`
- **Content-Type:** `application/json`

**Response `202 Accepted`:**
```json
{
  "upload_id": "f0051207-7e9f-4f5d-a8a1-8b1fed212103",
  "status": "PENDING",
  "message": "Image URL received. Processing started."
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
  "shop_id": "SHOP-102",
  "merchandiser_id": "MER-45",
  "image_url": "http://localhost:8000/media/uploads/SHOP-102/ab62985150b840a480a575b828fb1c49.jpg",
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
  "created_at": "2026-09-02T08:50:00Z",
  "updated_at": "2026-09-02T08:50:04Z"
}
```

---

#### `GET /uploads` (Aliases: `GET /analysis`, `GET /results`)
Returns a paginated list of analysis records with token metrics, search, and filtering.

- **URL:** `/uploads`
- **Method:** `GET`
- **Query Parameters:** `status`, `shop_id`, `merchandiser_id`, `search`, `limit`, `offset`

---

#### `GET /uploads/summary` (Alias: `GET /analysis/summary`)
Returns aggregated summary statistics including **total input tokens**, **total output tokens**, **total tokens**, **total cost in USD**, and **average tokens per scan**.

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

#### `DELETE /uploads/{upload_id}`
Permanently deletes an upload record and removes its saved image from disk.

---

## 4. Single API Call Usage Example (Python & cURL)

### cURL Single API:
```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@shelf.jpg" \
  -F "shop_id=SHOP-102"
```

### Python Single API:
```python
import requests

res = requests.post(
    "http://localhost:8000/analyze",
    files={"file": open("shelf.jpg", "rb")},
    data={"shop_id": "SHOP-102"}
)
data = res.json()
print("Products:", data["detected_products"])
print(f"Tokens: In={data['input_tokens']}, Out={data['output_tokens']}, Total={data['total_tokens']}")
print(f"Cost: ${data['estimated_cost_usd']:.6f}")
```
