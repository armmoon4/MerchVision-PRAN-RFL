# MerchVision-PRAN-RFL

> **AI-Powered Retail Merchandising & Shelf Intelligence System**  
> Processes retail shelf photos from **S3 bucket URLs, pure Base64 strings, or direct binary streams** completely **in-memory with zero local disk storage**.  
> Uses **Google Gemini 3.7 Flash** for high-fidelity vision perception, coupled with a **Decoupled Business Rules Engine** for deterministic Retail Execution Scoring (RES), Share of Shelf (SOS), planogram compliance, POSM wobbler detection, and out-of-stock gap detection. Enriched locally via SentenceTransformers + BM25Okapi against 1,954 canonical PRAN SKUs with zero extra AI tokens.
>
> Full API Reference: See [Documentation/API_DOCUMENTATION.md](Documentation/API_DOCUMENTATION.md) for complete payload schemas, response structures, and integration code.

---

## Table of Contents

- [System Architecture](#system-architecture)
  - [1. Perception to Decoupled Rules Pipeline](#1-perception-to-decoupled-rules-pipeline)
  - [2. Hybrid Catalogue Matching with Competitor Isolation](#2-hybrid-catalogue-matching-with-competitor-isolation)
- [Key Capabilities](#key-capabilities)
  - [Standalone Shelf AI Audit](#standalone-shelf-ai-audit)
  - [Retail Execution Score (RES) Breakdown](#retail-execution-score-res-breakdown)
  - [Executive Management Dashboard](#executive-management-dashboard)
- [Prerequisites](#prerequisites)
- [Quick Start — Docker](#quick-start--docker)
- [Quick Start — Local (No Docker)](#quick-start--local-no-docker)
- [Environment Configuration](#environment-configuration)
- [API Reference](#api-reference)
  - [V2 Auditing Endpoints](#v2-auditing-endpoints)
  - [Management Dashboard Endpoint](#management-dashboard-endpoint)
  - [Catalogue & Semantic Search Endpoints](#catalogue--semantic-search-endpoints)
  - [Token Router & Cost Telemetry](#token-router--cost-telemetry)
- [Interactive Web UI Dashboard](#interactive-web-ui-dashboard)
- [Project Structure](#project-structure)

---

## System Architecture

### 1. Perception to Decoupled Rules Pipeline

The platform decouples generative visual perception from business execution rules to guarantee audit reproducibility, eliminate hallucinated scoring, and minimize token costs:

```
[ Uploaded Shelf Image (e.g. 4000x3000, 12 MP) ]
                     │
                     ▼  (1. In-RAM Lanczos Resampling: downscaled to 1024–1600px max dimension)
     [ RAM Image Stream (io.BytesIO) ]
                     │
                     ▼  (1b. Exact Image Cache: SHA-256 fingerprinting for duplicate submissions)
     [ Cache Verification ]
                     │
                     ▼  (2. Gemini 3.7 Flash: extracts facings, shelf levels, pricing & wobblers)
     [ Perceptual JSON Extraction ]
                     │
                     ▼  (3. Zero-Token Local Linking: SentenceTransformers + BM25 + CrossEncoder)
     [ Enriched Product Detections (1,954 SKUs) ]
                     │
                     ▼  (4. Deterministic Rules Engine: calculates RES 0–100 & deductions)
     [ Verified Execution Score, Deductions & Action Checklist ]
```

| Pipeline Stage | Technical Mechanism | Business Value |
|---|---|---|
| **1. In-Memory Resampling** | Image resized in RAM using high-fidelity Lanczos filtering to 1024–1600px max dimension (`io.BytesIO`). | Preserves fine product text, packaging barcodes, and price tags while keeping vision prompt tokens low. |
| **1b. Exact Image Cache** | SHA-256 fingerprint hashes image payload before calling external LLMs. | Duplicate submissions return cached audit results in 0ms with **$0.00 cost** and **0 tokens consumed**. |
| **2. Gemini 3.7 Vision Perception** | Extracts PRAN and competitor facings, shelf levels (`top`, `middle`, `bottom`), visible price tags, and promotional talkers/wobblers (e.g. `'BELI & MENANG'`). | Accurate scene understanding with controlled reasoning effort (`OPENROUTER_REASONING_EFFORT=low`). |
| **3. Hybrid Catalogue Matching** | Matches detected product strings against `itemsdb.csv` (1,954 SKUs) using `SentenceTransformer` (`all-MiniLM-L6-v2`), `BM25Okapi` keyword indexing, and Cross-Encoder reranking. | **0 extra LLM tokens spent**. Guarantees canonical Item Code, Category, Sub-Category, and confidence score. |
| **4. Competitor Brand Isolation** | Explicit competitor keyword filtering (`JULIE`, `NABATI`, `JACKER`, `MUZIC`, `COCOPIE`, `CHOCO PIE`, `OREO`). | Prevents competitor products from being incorrectly overwritten as PRAN items during semantic catalogue search. |
| **5. Decoupled Rules Engine** | Transparent rule calculations for Retail Execution Score (RES), Arrangement Score (normalized 0–100), Share of Shelf (SOS), and priority action items. | Deterministic scoring with explainable deductions; prevents LLM hallucination of scores. |

---

## Key Capabilities

### Standalone Shelf AI Audit
- **Zero Configuration Required**: Does not require pre-registered store IDs or manual shelf mapping. Merchandisers or supervisors simply upload a shelf photo.
- **Share of Shelf (SOS)**: Automatically computes PRAN shelf share percentage vs. competitor shelf share percentage.
- **Arrangement Score**: Normalized 0–100 scale evaluating facing grouping, label forward alignment, and shelf neatness.
- **Pricing & POSM Wobbler Detection**: Detects shelf price tags (e.g. PRAN RM 3.90 vs. Competitor RM 5.50) and active promotional talkers/wobblers (e.g. contest tags `'BELI & MENANG'`).
- **Availability & Out-of-Stock**: Identifies empty shelf gaps and missing planogram SKUs.

### Retail Execution Score (RES) Breakdown

The Retail Execution Score is evaluated deterministically out of **100 points**:

$$\text{RES} = \text{Availability} (40) + \text{Planogram} (15) + \text{SOS} (25) + \text{Pricing} (10) + \text{Promotion} (10) - \text{Deductions}$$

| Metric | Max Weight | Evaluation Criteria |
|---|---|---|
| **Product Availability** | 40 pts | Core SKU presence and absence of visible shelf out-of-stock voids. |
| **Planogram Compliance** | 15 pts | Proper arrangement on assigned shelf tiers (`top`, `middle`, `bottom`) and alignment. |
| **Share of Shelf (SOS)** | 25 pts | PRAN visible facing share relative to the agreed category target (e.g. 45%). |
| **Pricing Display** | 10 pts | Verified shelf price tags present and readable on shelf edge. |
| **POSM & Promotion** | 10 pts | Active presence of promotional talkers, wobblers, shelf strips, or contest material. |

### Executive Management Dashboard
- **Executive Overview KPIs**: Real-time aggregated metrics across audits (Avg SOS, Avg Execution Score, Resolution Rate).
- **Category Share Breakdown**: Side-by-side PRAN vs. Competitor share across Beverages, Noodles, Biscuits, and Culinary.
- **On-Shelf Availability (OSA)**: Network-wide stockout frequency tracking highlighting top missing SKUs.
- **Exception-First Ranking**: Stores ranked by an **Urgency Index** ($\text{Missing SKUs} \times \text{Tier} + \text{Competitor Domination}$) requiring immediate distributor replenishment.

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| **Docker & Docker Compose** | Docker 24+, Compose v2+ | Recommended for complete production deployment |
| **Python** | 3.11+ / 3.12+ / 3.13+ | For local non-Docker development |
| **AI Provider Key** | OpenRouter or Google Gemini Direct | Configured in `.env` |

---

## Quick Start — Docker

Run the entire platform (FastAPI backend + PostgreSQL 16 + pre-warmed embeddings) with a single command:

```bash
docker compose up --build -d
```

- Web UI Dashboard: **http://localhost:8000/**
- Swagger Interactive API: **http://localhost:8000/docs**
- ReDoc API Reference: **http://localhost:8000/redoc**
- Token Router Status: **http://localhost:8000/tokens/models**

---

## Quick Start — Local (No Docker)

### Step 1 — Clone and Install Dependencies
```bash
git clone https://github.com/armmoon4/MerchVision-PRAN-RFL.git
cd MerchVision-PRAN-RFL
pip install -r requirements.txt
```

### Step 2 — Configure Environment
```bash
copy .env.example .env    # Windows
cp .env.example .env      # Linux/macOS
```

Edit `.env` to set your OpenRouter or Gemini API key:
```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=google/gemini-3.7-flash
OPENROUTER_REASONING_EFFORT=low
OPENROUTER_MAX_TOKENS=6000
ENABLE_RERANKER=true
MAX_IMAGE_DIMENSION=1600
```

### Step 3 — Launch FastAPI Application
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## API Reference

### V2 Auditing Endpoints

#### 1. Analyze Shelf Photo (URL or Base64)
- **Endpoint**: `POST /api/v2/audit/analyze`
- **Content-Type**: `application/json`

**Request Body:**
```json
{
  "image_url": "https://images.unsplash.com/photo-1578916171728-46686eac8d58?w=800",
  "outlet_id": null,
  "outlet_name": "Metro Retail Gondola"
}
```

**Response `200 OK`:**
```json
{
  "audit_id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
  "audit_stage": "STANDALONE",
  "audit_summary": {
    "status": "needs_attention",
    "image_quality": "good",
    "key_findings": [
      "PRAN Bisk Club Mama Cube Wafers detected on multiple shelf levels.",
      "Competitor snack brands occupy significant shelf space.",
      "PRAN Share of Shelf estimated at ~22%, falling short of agreed target of 45.0%.",
      "Promotional talker/wobbler present on shelf edge promoting Mama wafer contest ('BELI & MENANG')."
    ]
  },
  "retail_execution_score": {
    "score": 74.6,
    "grade": "Needs Attention",
    "availability_score": 35.0,
    "planogram_score": 11.1,
    "sos_score": 8.5,
    "pricing_score": 10.0,
    "promotion_score": 10.0,
    "deductions": [
      "[-] -14 pts: Product facings misaligned or grouping irregular",
      "[-] -12 pts: Shelf share (22.0%) is below agreed target (45%)"
    ]
  },
  "shelf_metrics": {
    "pran_shelf_share_pct": 22.0,
    "competitor_shelf_share_pct": 78.0,
    "empty_shelf_share_pct": 0.0,
    "pran_facing_count": 12,
    "competitor_facing_count": 6,
    "arrangement_score": 75.0,
    "measurement_type": "estimated"
  },
  "pricing_and_promotions": [
    {
      "product": "PRAN Bisk Club Mama Cube Wafer Biscuits",
      "visible_price": 3.9,
      "promotion_detected": true,
      "confidence": "high"
    },
    {
      "product": "Julie's Love Letters Wafer Cubes",
      "visible_price": 5.5,
      "promotion_detected": false,
      "confidence": "high"
    }
  ],
  "detected_products": [
    {
      "brand": "PRAN",
      "product_name": "Bisk Club Mama Cube Wafer Biscuits",
      "variant": "Chocolate",
      "sku": "102210",
      "facing_count": 2,
      "shelf_level": "top",
      "matched": true,
      "catalogue_suggestions": [
        {
          "item_name": "Bisk Club Cream Biscuit Chocolate 90g",
          "item_code": "102210",
          "item_image_url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/102210_l.jpg.jpg",
          "confidence": 0.718
        }
      ]
    }
  ],
  "token_usage": {
    "input_tokens": 2468,
    "output_tokens": 1583,
    "total_tokens": 4051,
    "estimated_cost_usd": 0.007787
  }
}
```

#### 2. Analyze Shelf Photo File (Multipart Stream)
- **Endpoint**: `POST /api/v2/audit/analyze/file`
- **Content-Type**: `multipart/form-data`
- **Form Fields**: `file` (Binary image file)

#### 3. Closed-Loop Improvement Verification
- **Endpoint**: `POST /api/v2/audit/verify`
- **Content-Type**: `application/json`
- **Description**: Submits an "AFTER" photo following corrective merchandising actions to calculate score improvement delta.

---

### Management Dashboard Endpoint

#### `GET /api/v2/management/dashboard`
Returns aggregated shelf performance data across territories and categories.

- **Query Parameters**:
  - `territory`: Filter by territory (e.g. `Dhaka North`, `Chittagong Central`).
  - `category`: Filter by category (e.g. `Beverages`, `Biscuits`, `Noodles`, `Culinary`).

**Response Structure**:
- `executive_overview`: Total audits, average PRAN SOS %, average RES score, issue resolution rate %.
- `shelf_intelligence`: Overall SOS and category share breakdown (PRAN vs. Competitors).
- `product_availability`: On-Shelf Availability (OSA %) and top missing SKUs.
- `exception_outlets`: Outlets ranked by Urgency Index requiring immediate replenishment.

---

### Catalogue & Semantic Search Endpoints

- `GET /catalogue/search?query=Frooto&limit=10`: Local hybrid search (SentenceTransformer + BM25 + CrossEncoder) across 1,954 products with 0 extra LLM tokens.
- `GET /catalogue/stats`: Catalogue item count and vector indexing status.
- `POST /catalogue/rebuild`: Re-computes local sentence embedding index.

---

### Token Router & Cost Telemetry

- `GET /tokens/models`: Active vision model limits, reasoning configuration, and token rates.
- `GET /tokens/pricing`: Input/output pricing rates per 1K and 1M tokens.
- `GET /tokens/stats`: Cumulative token usage, total scans, and aggregated expenditure.
- `POST /tokens/estimate`: Pre-computes estimated tokens and cost prior to execution.

---

## Interactive Web UI Dashboard

The frontend dashboard is served directly from `testui.html` at `http://localhost:8000/`. It provides a clean, emoji-free, corporate interface designed for both field merchandisers and executive leadership:

1. **Shelf AI Audit (Image to Insights)**:
   - **Handheld Smartphone Simulator**: Simulates on-the-ground mobile experience showing Retail Execution Score, arrangement score, deductions, sub-score matrix, and actionable checklist.
   - **Executive Desk Studio**: High-resolution image inspection, executive key findings, pricing and active wobbler display, and detected products explorer.
   - **Instant Demo**: One-click **Wafer Audit Demo** button instantly loads real wafer gondola audit data (`041b7223-9fd2-4d53-ab59-8e232beaa69d`) for testing.
2. **Management Dashboard**:
   - Executive overview KPI cards, Share of Shelf gauges, category share breakdown table, OSA stockout tracking, and exception-first urgency ranking.
3. **AI Vision Lab & SKUs**:
   - Direct low-latency SKU extraction testing with raw model output.
4. **ItemsDB Catalogue Explorer**:
   - Interactive search across all 1,954 canonical PRAN SKUs.
5. **API Telemetry & Costs**:
   - Real-time token consumption, average cost per scan, and API latency metrics.

---

## Project Structure

```
MerchVision-PRAN-RFL/
├── app/
│   ├── config.py                 # Settings (.env, Gemini 3.7 Flash, token rates)
│   ├── database.py               # Database engine (PostgreSQL 16 / SQLite)
│   ├── main.py                   # FastAPI routing, middleware, and lifecycle hooks
│   ├── models.py                 # SQLAlchemy ORM models (AuditSession, RackUpload)
│   ├── schemas.py                # Pydantic validation schemas
│   ├── routers/
│   │   ├── audit_router.py       # V2 Auditing & Management Dashboard endpoints
│   │   └── token_router.py       # Token routing, estimation, and telemetry
│   └── services/
│       ├── ai_service.py         # Gemini 3.7 Flash vision, in-RAM Lanczos, competitor isolation
│       ├── rules_engine.py       # Deterministic Retail Execution Scoring (RES) & deductions
│       ├── items_db_service.py   # Hybrid catalogue search (BM25 + SentenceTransformer)
│       └── storage_service.py    # Zero-disk in-memory byte stream handlers
├── Documentation/
│   └── API_DOCUMENTATION.md      # Comprehensive API specification & payload guide
├── itemsdb.csv                   # Official PRAN-RFL master product catalogue (1,954 SKUs)
├── testui.html                   # Web dashboard & interactive studio (emoji-free)
├── docker-compose.yml            # Production container configuration
├── Dockerfile                    # Container build specification
├── requirements.txt              # Production Python dependencies
└── README.md                     # Project documentation
```
