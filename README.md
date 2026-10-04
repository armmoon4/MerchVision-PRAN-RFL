# MerchVision-PRAN-RFL

> AI-powered retail merchandising backend — processes retail rack photos from **S3 bucket URLs, pure Base64 strings (e.g. `/9j/...`), or direct streams** completely **in-memory with zero local disk storage**. Google Gemini 3.7 Flash detects all PRAN-RFL product SKUs and measures exact token usage and estimated USD costs.
>
>  **Full API Reference**: See [Documentation/API_DOCUMENTATION.md](Documentation/API_DOCUMENTATION.md) for detailed payload schemas, sequencing, and code examples.

---

## Table of Contents

- [Zero-Disk Architecture & How It Works](#zero-disk-architecture--how-it-works)
- [Prerequisites](#prerequisites)
- [Quick Start — Local (No Docker)](#quick-start--local-no-docker)
- [Quick Start — Docker](#quick-start--docker)
- [Token Cost Calculation](#token-cost-calculation)
- [API Reference](#api-reference)
- [Dedicated API Docs](Documentation/API_DOCUMENTATION.md)
- [Project Structure](#project-structure)

---

## Zero-Disk Architecture & How It Works

The system operates **100% in-memory**:
- **Zero Local Disk Footprint**: Images are never saved, cached, or written to `./media` or the local file system.
- **Input Versatility**: Accepts S3 presigned URLs, remote HTTP/HTTPS links, raw Base64 strings (e.g. `/9j/4AAQ...`), or direct binary streams.
- **In-Memory Optimization**: Resizes and optimizes images in RAM (`io.BytesIO`) using Lanczos downscaling to minimize Gemini vision tokens before streaming to the Gemini API.

```
Client Payload (S3 URL / Base64 / Binary Stream)
                     │
                     ▼
       ┌───────────────────────────────┐
       │ In-Memory Ingestion & Memory  │
       │ Resizing (0 Disk Persistence) │
       └──────────────┬────────────────┘
                      │
                      ▼
       ┌───────────────────────────────┐
       │ Google Gemini 3.7 Flash API   │
       │ (Part.from_bytes in RAM)      │
       └──────────────┬────────────────┘
                      │
                      ▼
       ┌───────────────────────────────┐
       │ 1-Call Immediate Response     │
       │ - Detected PRAN-RFL Products  │
       │ - Token Breakdown (In/Out/Tot)│
       │ - Estimated USD Cost          │
       └───────────────────────────────┘
```

---

## Prerequisites

### For Local (No Docker)

| Requirement | Version | Check |
|---|---|---|
| Python | 3.13+ | `python --version` |
| Pipenv | any | `pipenv --version` |
| Gemini API Key | — | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

### For Docker

| Requirement | Version | Check |
|---|---|---|
| Docker Desktop | any | `docker --version` |
| Docker Compose | v2+ | `docker compose version` |
| Gemini API Key | — | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

---

## Quick Start — Local (No Docker)

Uses **SQLite** (zero setup) + pure in-memory vision processing.

### Step 1 — Clone and enter the project

```bash
cd MerchVision-PRAN-RFL
```

### Step 2 — Install dependencies

```bash
pipenv install
```

### Step 3 — Configure environment

```bash
copy .env.example .env        # Windows
cp .env.example .env          # Mac/Linux
```

Open `.env` and **set your Gemini API key**:

```env
GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxx
```

### Step 4 — Start the server

```bash
pipenv run uvicorn app.main:app --reload
```

Open **http://localhost:8000** in your browser for the Web UI dashboard or **http://localhost:8000/docs** for Swagger API Docs.

---

## Token Cost Calculation

Gemini token pricing is calculated dynamically based on configurable rates in `.env`:

$$\text{Estimated Cost (USD)} = \left(\frac{\text{Input Tokens}}{1,000,000} \times \text{Rate}_{\text{input}}\right) + \left(\frac{\text{Output Tokens}}{1,000,000} \times \text{Rate}_{\text{output}}\right)$$

- Default Input Rate: `$0.10` per 1M tokens
- Default Output Rate: `$0.40` per 1M tokens

---

## API Reference

### 1. Direct 1-Call Analysis (Download-Then-Delete)

#### `POST /analyze`

Analyzes an S3 URL, HTTP image URL, or Base64 string in **one synchronous call**:

**Request (`application/json`)**
```json
{
  "image_url": "https://s3.amazonaws.com/bucket/image.jpg"
}
```
*Or with Base64 string:*
```json
{
  "image_url": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQ..."
}
```

**Response `200 OK`**
```json
{
  "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
  "status": "COMPLETED",
  "image_url": "https://s3.amazonaws.com/bucket/image.jpg",
  "detected_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "quantity_visible": 6
    },
    {
      "product_name": "PRAN Lassi 200ml",
      "quantity_visible": 4
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

Accepts a binary image file (`multipart/form-data`) and streams it purely in RAM without writing to disk.

---

### 2. Analytics & Summaries

#### `GET /uploads/summary`

Returns aggregated stats: total scans, detected products, total input/output tokens, and cumulative USD cost.

#### `GET /uploads`

Returns paginated history of scans and token metrics.

---

## Project Structure

```
MerchVision-PRAN-RFL/
├── app/
│   ├── config.py             # Settings from .env via Pydantic
│   ├── database.py           # SQLAlchemy session and engine
│   ├── main.py               # FastAPI routes & Swagger setup
│   ├── models.py             # Database ORM models
│   ├── schemas.py            # Pydantic request/response models
│   └── services/
│       ├── ai_service.py     # In-memory Gemini vision & token analytics
│       └── storage_service.py# In-memory streaming & validation (Zero Disk)
├── Documentation/
│   └── API_DOCUMENTATION.md  # Comprehensive API documentation
├── testui.html               # Interactive Web UI dashboard
├── requirements.txt
├── Pipfile
└── README.md
```
