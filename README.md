# MerchVision-PRAN-RFL

> AI-powered retail merchandising backend — processes retail rack photos from **S3 bucket URLs, pure Base64 strings (e.g. `/9j/...`), or direct streams** completely **in-memory with zero local disk storage**. Uses **Google Gemini 2.5 Flash-Lite / 3.7 Flash** with suppressed reasoning and compact pipe-delimited outputs to detect PRAN-RFL product SKUs, enriched locally via SentenceTransformers + BM25 with zero extra AI tokens.
>
> 📖 **Full API Reference**: See [Documentation/API_DOCUMENTATION.md](Documentation/API_DOCUMENTATION.md) for detailed payload schemas, sequencing, and code examples.

---

## Table of Contents

- [Core Architecture & Vision Pipeline](#core-architecture--vision-pipeline)
  - [Pipeline Breakdown](#pipeline-breakdown)
- [Prerequisites](#prerequisites)
- [Quick Start — Local (No Docker)](#quick-start--local-no-docker)
- [Quick Start — Docker](#quick-start--docker)
- [Token Cost Calculation](#token-cost-calculation)
- [API Reference](#api-reference)
  - [1. Single-Call Rack Analysis](#1-single-call-rack-analysis)
  - [2. Items Catalogue & Semantic Search](#2-items-catalogue--semantic-search)
  - [3. Token Router & Cost Analytics](#3-token-router--cost-analytics)
- [Project Structure](#project-structure)

---

## Core Architecture & Vision Pipeline

The system operates **100% in-memory** with a cost- and latency-optimized pipeline:

```
[ Uploaded Shelf Image (e.g. 4000x3000) ]
                   │
                   ▼  (1. Auto-Crop & Lanczos 768px: raw 6,000+ vision tokens cut to ~516–700)
    [ Downscaled to 768px ]
                   │
                   ▼  (1b. Exact Image Cache: if duplicate photo → 0 tokens, $0.00 cost)
    [ Cache Check ]
                   │
                   ▼  (2. Thinking Suppressed: effort="none" / budget=0 eliminates 1,000+ reasoning tokens)
    [ Call Gemini 2.5 Flash-Lite / 3.7 Flash ]
                   │
                   ▼  (3. Pipe Syntax: ~10 tokens/item → total actual output is ONLY ~50–70 TOKENS)
    [ Raw Text: "Frooto 250ml|6" (~50–70 tokens) ]
                   │
                   ▼  (4. Zero AI Tokens: SentenceTransformer + BM25 local enrichment)
    [ Enriched Output: SKU, Category, Sub-Category, Confidence, Qty ]
```

### Pipeline Breakdown

| Step | Optimization Stage | Technical Mechanism | Impact on Tokens & Latency |
|---|---|---|---|
| **1** | **Auto-Crop & Lanczos 768px** | High-resolution camera images (e.g. 4000×3000, 12 MP) are downscaled in RAM (`io.BytesIO`) using high-fidelity Lanczos resampling to 768px max dimension. | Cuts raw vision tile tokens from **6,000+ tokens** down to only **~516–700 tokens** while preserving product text sharpness. |
| **1b** | **Exact Image Cache** | SHA-256 fingerprinting checks for duplicate shelf photo submissions. | If a duplicate image is detected: **0 tokens consumed, $0.00 cost**, and instant 0ms response. |
| **2** | **Thinking Suppressed** | Enforces reasoning suppression via OpenRouter (`effort="none"`, `max_tokens=0`) or Gemini Direct (`thinking_budget=0`). | Eliminates **1,000+ reasoning tokens** and avoids 3–5 seconds of unnecessary thinking latency per scan. |
| **3** | **Compact Pipe Syntax** | Model is instructed to respond in ultra-compact plain text: `product name\|quantity` (e.g. `Frooto 250ml\|6`) rather than verbose JSON arrays. | Output is only **~10 tokens per detected item**, keeping total completion output to **ONLY ~50–70 TOKENS**. |
| **4** | **Zero AI Tokens Enrichment** | AI output is matched locally in memory against the 1,954-product `itemsdb.csv` catalogue using `SentenceTransformer` (`all-MiniLM-L6-v2`), `BM25Okapi` keyword search, and Cross-Encoder reranking. | **0 extra LLM tokens spent**. Returns verified SKU Item Code, Item Name, Category, Sub-Category, and Confidence (0.0–1.0). |

---

## Prerequisites

### For Local (No Docker)

| Requirement | Version | Check |
|---|---|---|
| Python | 3.11+ / 3.13+ | `python --version` |
| Pipenv / Pip | any | `pipenv --version` |
| OpenRouter or Gemini API Key | — | [openrouter.ai/keys](https://openrouter.ai/keys) or [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

### For Docker

| Requirement | Version | Check |
|---|---|---|
| Docker Desktop | any | `docker --version` |
| Docker Compose | v2+ | `docker compose version` |
| API Key | — | Set in `.env` |

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
# or: pip install -r requirements.txt
```

### Step 3 — Configure environment

```bash
copy .env.example .env        # Windows
cp .env.example .env          # Mac/Linux
```

Open `.env` and set your preferred AI provider:

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=google/gemini-3.7-flash
OPENROUTER_REASONING_EFFORT=none

# Or for Gemini direct:
# AI_PROVIDER=gemini
# GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxx
# GEMINI_MODEL=gemini-3.7-flash
# GEMINI_THINKING_BUDGET=0
```

### Step 4 — Start the server

```bash
pipenv run uvicorn app.main:app --reload
```

Open **http://localhost:8000** in your browser for the Web UI dashboard or **http://localhost:8000/docs** for Swagger API Docs.

---

## Quick Start — Docker

Run the entire stack with PostgreSQL 16 and pre-warmed embeddings:

```bash
docker compose up --build -d
```

---

## Token Cost Calculation

Pricing is calculated dynamically based on configurable rates in `.env`:

$$\text{Estimated Cost (USD)} = \left(\frac{\text{Input Tokens}}{1,000,000} \times \text{Rate}_{\text{input}}\right) + \left(\frac{\text{Output Tokens}}{1,000,000} \times \text{Rate}_{\text{output}}\right)$$

- Default Input Rate: `$0.75` per 1M tokens (OpenRouter `google/gemini-3.7-flash`)
- Default Output Rate: `$3.75` per 1M tokens
- Also supports Gemini 2.5 Flash-Lite / direct Gemini rates configurable via `.env`.

---

## API Reference

### 1. Single-Call Rack Analysis

#### `POST /analyze`

Analyzes an S3 URL, HTTP image URL, Data URI, or pure Base64 string in **one synchronous call**:

**Request (`application/json`)**
```json
{
  "image_url": "https://s3.amazonaws.com/bucket/shelf_photo.jpg"
}
```

*Or with Base64 payload:*
```json
{
  "image_url": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQ..."
}
```

**Response `200 OK` (With Zero-AI-Token Local Enrichment):**
```json
{
  "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
  "status": "COMPLETED",
  "image_url": "https://s3.amazonaws.com/bucket/shelf_photo.jpg",
  "detected_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "quantity_visible": 6,
      "matched": true,
      "catalogue_suggestions": [
        {
          "sub_category_name": "Juice",
          "sub_category_code": "SC012",
          "category_name": "Beverage",
          "category_code": "CAT004",
          "item_name": "PRAN Frooto Mango Juice 250ml",
          "item_code": "PRAN-MJ-250",
          "confidence": 0.942
        }
      ]
    },
    {
      "product_name": "Bisk Club Cream Biscuit Chocolate 90g",
      "quantity_visible": 4,
      "matched": true,
      "catalogue_suggestions": [
        {
          "sub_category_name": "Biscuits",
          "sub_category_code": "SC008",
          "category_name": "Bakery & Confectionery",
          "category_code": "CAT002",
          "item_name": "Bisk Club Chocolate Cream 90g",
          "item_code": "BC-CC-090",
          "confidence": 0.915
        }
      ]
    }
  ],
  "token_usage": {
    "input_tokens": 586,
    "output_tokens": 58,
    "total_tokens": 644,
    "estimated_cost_usd": 0.000657
  },
  "input_tokens": 586,
  "output_tokens": 58,
  "total_tokens": 644,
  "estimated_cost_usd": 0.000657,
  "error_message": null,
  "created_at": "2026-10-06T12:00:00Z"
}
```

#### `POST /analyze/file`

Uploads a binary image file (`multipart/form-data`) and streams it purely in RAM with zero disk writes.

---

### 2. Items Catalogue & Semantic Search

- `GET /catalogue/search?q=Frooto&limit=10`: Search local `itemsdb.csv` catalogue by text, category, or SKU code with hybrid BM25 + cosine scoring (zero AI tokens).
- `GET /catalogue/stats`: View catalogue size (1,954 products) and embedding index status.
- `POST /catalogue/rebuild`: Force rebuild of product embeddings.

---

### 3. Token Router & Cost Analytics

- `GET /tokens/models`: View active model limits, capabilities, and pricing rates.
- `GET /tokens/pricing`: View current input/output rates per 1M and 1K tokens.
- `POST /tokens/estimate`: Pre-calculate estimated vision tokens, system tokens, and costs before execution.
- `POST /tokens/count`: Live token count calculation against model limits.
- `GET /uploads/summary`: Aggregated scan count, total products detected, token usage, and cumulative USD spend.

---

## Project Structure

```
MerchVision-PRAN-RFL/
├── app/
│   ├── config.py                 # Pydantic settings (.env, models, token limits)
│   ├── database.py               # SQLAlchemy SQLite/PostgreSQL setup
│   ├── main.py                   # FastAPI app, /analyze routes, and lifecycles
│   ├── models.py                 # ORM database models (RackUpload)
│   ├── schemas.py                # Pydantic request & response models
│   ├── routers/
│   │   └── token_router.py       # Token router, pricing, estimation & counting
│   └── services/
│       ├── ai_service.py         # In-memory vision, Lanczos 768px, suppressed thinking, pipe parser
│       ├── items_db_service.py   # Hybrid search (SentenceTransformer + BM25 + CrossEncoder)
│       └── storage_service.py    # Zero-disk in-memory stream helpers
├── Documentation/
│   └── API_DOCUMENTATION.md      # Full API specification and sequencing guide
├── itemsdb.csv                   # Official PRAN-RFL catalogue (1,954 products)
├── testui.html                   # Web UI dashboard and interactive tester
├── requirements.txt
├── Pipfile
└── README.md
```
