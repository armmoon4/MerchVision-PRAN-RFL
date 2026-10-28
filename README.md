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

### Standalone Shelf AI Audit (Image-Only Mode)
- **Zero Planogram Dependency**: Answers *"What PRAN products are on this shelf and how much space do they take?"* without requiring pre-configured expected SKU lists.
- **Decoupled Brand & SKU Identity**: Classifies every facing into `BrandClass` (`pran`, `competitor`, `unknown`) before catalogue matching, preventing catalogue misses from corrupting facing counts or Share of Shelf.
- **Master Catalogue Enrichment**: Matches PRAN detections into `SkuStatus` (`matched`, `candidate`, `unknown_pran`). Items missing from `itemsdb.csv` are preserved as `unknown_pran` and still counted as PRAN facings.
- **Share of Shelf (SOS)**: Automatically computes PRAN shelf share percentage vs. competitor shelf share percentage by visible linear shelf width.
- **Placement & Golden Zone**: Evaluates proportion of PRAN facings placed at prime eye level vs. top/bottom shelves.
- **Arrangement Score**: Normalized 0–100 scale evaluating facing grouping, label forward alignment, and shelf neatness.
- **Pricing & POSM Wobbler Detection**: Detects shelf price tags (e.g. PRAN RM 3.90 vs. Competitor RM 5.50) and active promotional talkers/wobblers (e.g. contest tags `'BELI & MENANG'`).
- **Stock Health & Gaps**: Identifies visible shelf voids and gaps around product facings.

### Retail Execution Score (RES) Breakdown

The Retail Execution Score is evaluated deterministically out of **100 points** across **6 image-provable pillars**:

```
RES (100 pts) = Presence (20) + SOS (25) + Placement (20) + Arrangement (15) + Stock Health (10) + Price & Promo (10)
```

| Pillar | Max Weight | Evaluation Criteria |
|---|---|---|
| **1. Presence** | 20 pts | PRAN products detected at all; points scale with number of distinct PRAN SKUs (variety). |
| **2. Visibility (SOS)** | 25 pts | PRAN linear shelf share percentage compared against reference category benchmark (default: 30%). |
| **3. Placement** | 20 pts | Golden Zone ratio: percentage of PRAN facings positioned at prime eye level. |
| **4. Shelf Arrangement** | 15 pts | Facing alignment, front-facing orientation, and variant grouping neatness (0–100 scale). |
| **5. Stock Health** | 10 pts | Absence of visible shelf gaps and out-of-stock voids around products. |
| **6. Price & Promo** | 10 pts | Verified shelf price tags present (up to 7 pts) + promotional wobblers/POSM present (3 pts). |

> **Special Case (No PRAN Detected):** When zero PRAN facings are identified on a fixture, the API returns `score: null` with grade `"No PRAN Detected"`.

### Executive Management Dashboard
- **Executive Overview KPIs**: Real-time aggregated metrics across audits (Avg SOS, Avg Execution Score, Resolution Rate).
- **Category Share Breakdown**: Side-by-side PRAN vs. Competitor share across Beverages, Noodles, Biscuits, and Culinary.
- **On-Shelf Availability (OSA)**: Network-wide stockout frequency tracking highlighting top missing SKUs.
- **Exception-First Ranking**: Stores ranked by an **Urgency Index** (`Missing SKUs * Store Tier + Competitor Domination`) requiring immediate distributor replenishment.

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

### Postman API Collection & Environment

A complete Postman collection and environment are included in the repository root:
- **Postman Collection:** [`MerchVision_postman_collection.json`](MerchVision_postman_collection.json) (38 endpoints covering System, Survey AI, Merchandising Audit, Catalogue, and Token Router)
- **Postman Environment:** [`MerchVision_postman_environment.json`](MerchVision_postman_environment.json)
- **Default Base URL:** `http://172.17.4.199:8093/`

**Quick Setup in Postman:**
1. Open Postman $\rightarrow$ Click **Import** $\rightarrow$ Select `MerchVision_postman_collection.json` and `MerchVision_postman_environment.json`.
2. Select the **MerchVision Server (http://172.17.4.199:8093)** environment.
3. The `baseUrl` is pre-configured to `http://172.17.4.199:8093`. An automatic pre-request script strips any trailing slash if present so `{{baseUrl}}/endpoint` always resolves cleanly.

### Retail Merchandising Endpoints (`/merchandising/...`)

#### 1. Analyze Shelf Photo (URL or Base64)
- **Endpoint**: `POST /merchandising/analyze` *(also aliased as `/merchandising/audit/analyze` and `/api/v2/audit/analyze`)*
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
  "input_context": "image_only",
  "fixture_category": "Wafer Biscuit Section",
  "image_quality": {
    "score": 0.94,
    "issues": []
  },
  "audit_stage": "STANDALONE",
  "audit_summary": {
    "status": "needs_attention",
    "image_quality": "good",
    "key_findings": [
      "PRAN Bisk Club Mama Cube Wafers detected across multiple shelf levels.",
      "Competitor snack brands occupy significant shelf width.",
      "PRAN Share of Shelf estimated at ~22.0%, below reference benchmark of 30.0%.",
      "Promotional talker/wobbler present on shelf edge promoting Mama wafer contest ('BELI & MENANG')."
    ]
  },
  "retail_execution_score": {
    "score": 72.1,
    "grade": "Needs Attention",
    "mode": "image_only",
    "pillars": {
      "presence": 17.0,
      "visibility_sos": 18.3,
      "placement": 13.3,
      "arrangement": 11.2,
      "stock_health": 10.0,
      "price_promo": 10.0
    },
    "availability_score": 17.0,
    "planogram_score": 13.3,
    "sos_score": 18.3,
    "pricing_score": 10.0,
    "promotion_score": 10.0,
    "deductions": [
      "[-] -6.7 pts: Weak Golden Zone placement (67% of PRAN items at eye level)",
      "[-] -3.8 pts: Product facings misaligned or irregular grouping"
    ]
  },
  "shelf_metrics": {
    "pran_shelf_share_pct": 22.0,
    "competitor_shelf_share_pct": 78.0,
    "empty_shelf_share_pct": 0.0,
    "pran_facing_count": 6,
    "competitor_facing_count": 11,
    "arrangement_score": 75.0,
    "measurement_type": "estimated"
  },
  "pran_summary": {
    "pran_detected": true,
    "distinct_products": 3,
    "total_facings": 6,
    "sos_pct": 22.0,
    "eye_level_share_pct": 66.7,
    "sku_breakdown": [
      {
        "product_name": "Bisk Club Mama Cube Wafer Biscuits Chocolate",
        "sku_status": "matched",
        "sku_code": "102210",
        "facings": 2,
        "levels": ["eye_level"]
      },
      {
        "product_name": "Bisk Club Mama Cube Wafer Biscuits Strawberry",
        "sku_status": "matched",
        "sku_code": "102210",
        "facings": 2,
        "levels": ["eye_level"]
      },
      {
        "product_name": "Mama Cube Wafer Milk Vanilla",
        "sku_status": "matched",
        "sku_code": "107785",
        "facings": 2,
        "levels": ["top"]
      }
    ]
  },
  "catalogue_coverage": {
    "pran_facings_total": 6,
    "matched": 6,
    "candidate": 0,
    "unknown_pran": 0
  },
  "competitors": [
    { "brand": "Julie's", "facings": 2 },
    { "brand": "Nabati", "facings": 5 },
    { "brand": "Muzic", "facings": 1 },
    { "brand": "Cocopie", "facings": 3 }
  ],
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
      "brand_class": "pran",
      "product_name": "Bisk Club Mama Cube Wafer Biscuits",
      "variant": "Chocolate",
      "sku": "102210",
      "sku_status": "matched",
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
- **Endpoint**: `POST /merchandising/analyze/file` *(alias: `/merchandising/audit/analyze/file`, `/api/v2/audit/analyze/file`)*
- **Content-Type**: `multipart/form-data`
- **Form Fields**: `file` (Binary image file)

---

### Management Dashboard Endpoint

#### `GET /merchandising/dashboard`
*(also aliased as `/merchandising/management/dashboard` and `/api/v2/management/dashboard`)*
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

The frontend dashboard is served directly from `testui.html` at `http://localhost:8000/`. It provides a clean, emoji-free, corporate interface designed for data-first testing and executive review:

1. **Shelf AI Audit Studio (Image-Only Testing UI)**:
   - **Desktop Testing Studio Grid**: Full-width data testing interface (no phone mockup) with side-by-side controls, live image preview, and comprehensive metrics inspection.
   - **6-Pillar Execution Scorecard**: Visual progress bars and numerical scores for Presence (/20), SOS (/25), Placement (/20), Arrangement (/15), Stock Health (/10), and Price & Promo (/10).
   - **PRAN SKU Breakdown Table**: Displays detected PRAN products, status badges (`Matched`, `Candidate`, `Unknown PRAN`), canonical SKU codes, facings, and shelf levels.
   - **Competitor Brands Summary**: Real-time tags displaying competitor commercial brands and facing counts.
   - **Key Metrics Chips**: PRAN Facings, Competitor Facings, Distinct PRAN SKUs, Golden Zone Share %, Empty Shelf Gaps, and Catalogue Coverage (`M / C / U`).
   - **Perception Telemetry**: Token usage breakdown, estimated cost in USD, and fixture category classification.
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
│   ├── brands.py                 # Brand registry (PRAN tokens, generic category word filter, competitor brands)
│   ├── config.py                 # Settings (.env, Gemini 3.7 Flash, SOS benchmarks, token rates)
│   ├── database.py               # Database engine (PostgreSQL 16 / SQLite)
│   ├── main.py                   # FastAPI routing, middleware, and lifecycle hooks
│   ├── models.py                 # SQLAlchemy ORM models (AuditSession, RackUpload)
│   ├── schemas.py                # Pydantic schemas (BrandClass, SkuStatus, 6-pillar RES, PranSummary)
│   ├── routers/
│   │   ├── audit_router.py       # V2 Auditing & Management Dashboard endpoints
│   │   └── token_router.py       # Token routing, estimation, and telemetry
│   └── services/
│       ├── ai_service.py         # Gemini 3.7 Flash vision, in-RAM Lanczos, decoupled brand detection
│       ├── rules_engine.py       # Deterministic 6-pillar Retail Execution Scoring (RES) & deductions
│       ├── items_db_service.py   # Hybrid catalogue search (BM25 + SentenceTransformer)
│       └── storage_service.py    # Zero-disk in-memory byte stream handlers
├── Documentation/
│   └── API_DOCUMENTATION.md      # Comprehensive API specification & payload guide
├── itemsdb.csv                   # Official PRAN-RFL master product catalogue (1,954 SKUs)
├── testui.html                   # Web dashboard & interactive desktop studio (emoji-free)
├── docker-compose.yml            # Production container configuration
├── Dockerfile                    # Container build specification
├── requirements.txt              # Production Python dependencies
└── README.md                     # Project documentation
```
