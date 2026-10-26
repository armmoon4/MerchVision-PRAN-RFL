# PRAN-RFL Retail Merchandising & Shelf Intelligence API Documentation

## Overview

The PRAN-RFL Retail Merchandising & Shelf Intelligence module provides automated computer vision shelf auditing, real-time Planogram adherence assessment, Share of Shelf (SOS) analytics, promotional material verification, and deterministic Retail Execution Scoring (RES 0-100). The module processes single-camera shelf photographs directly in memory, matches detected products against the canonical master catalogue (1,954 SKUs in `itemsdb.csv`) via zero-token hybrid vector search, and delivers closed-loop verification for field merchandisers and executive BI dashboards.

## Base URL

```text
/api/v2/audit/
```

Additional related module base paths:
- Management Dashboard: `/api/v2/management/`
- Retail Outlets: `/api/v2/outlets`
- Master Catalogue: `/catalogue/`
- Token Telemetry & Accounting: `/tokens/`

## Authentication

All API endpoints can be accessed directly or with Bearer token authentication in production environments:

```http
Authorization: Bearer <access_token>
```

In standalone or field audit scenarios, endpoints accept requests with or without pre-registered outlet identifiers.

---

## Endpoints

### 1. Analyze Shelf Photo (URL or Base64)

```http
POST /api/v2/audit/analyze
```

Performs full synchronous shelf intelligence analysis from an image URL (HTTP/HTTPS, S3 presigned URL) or Base64-encoded image string. The image is downscaled in memory using Lanczos resampling to 1024px maximum dimension, fingerprinted via SHA-256 for exact-match caching, analyzed via Gemini 3.7 Flash vision inference, enriched against the local master product catalogue, evaluated by the business rules engine, and persisted to the relational database.

**Permissions:** Authenticated / Authorized Field Merchandiser

#### Request Body (`application/json`)

| Field | Type | Required | Description |
|---|---|---|---|
| `image_url` | string | Yes | S3 presigned URL, HTTP/HTTPS public URL, Data URI, or raw Base64 string. |
| `outlet_id` | string | No | UUID or string identifier of a registered retail outlet. |
| `outlet_name` | string | No | Human-readable store or gondola name (e.g., `Metro Hypermarket - Biscuit Aisle`). |
| `visit_id` | string | No | UUID linking this audit to an active field merchandiser visit session. |
| `audit_stage` | string | No | Stage of audit: `STANDALONE`, `BEFORE`, or `AFTER`. Default: `STANDALONE`. |
| `merchandiser_id` | string | No | Identifier of the field agent conducting the audit. |
| `merchandiser_name` | string | No | Display name of the field agent. |
| `expected_skus` | array[string] | No | List of required SKU codes or names expected on this shelf fixture. |
| `planogram_rules` | object | No | Optional dictionary defining eye-level target SKUs, minimum facings, or SOS thresholds. |
| `approved_prices` | object | No | Dictionary mapping SKU codes to approved shelf price numbers for price audit. |

#### Choices / Enums: `audit_stage`

| Value | Description |
|---|---|
| `STANDALONE` | Default independent audit without linked before/after comparison. |
| `BEFORE` | Baseline audit conducted at the start of a store visit before corrective actions. |
| `AFTER` | Post-correction audit conducted to verify issue resolution and score uplift. |

#### Success Response — 200 OK

```json
{
  "audit_id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
  "visit_id": null,
  "outlet_id": "OUTLET-104",
  "outlet_name": "Metro Hypermarket - Biscuit Aisle",
  "audit_stage": "STANDALONE",
  "audit_summary": {
    "status": "needs_attention",
    "image_quality": "good",
    "key_findings": [
      "PRAN Bisk Club Mama Cube Wafers detected on top and middle shelf levels.",
      "Competitor brands (Julie's Love Letters, Nabati Bites, Nabati Rolls) occupy 90% of visible shelf space.",
      "PRAN Share of Shelf measured at 10.0%, falling short of the agreed 45.0% target.",
      "Promotional talker/wobbler present on shelf edge promoting Mama wafer contest ('BELI & MENANG')."
    ]
  },
  "retail_execution_score": {
    "score": 67.8,
    "grade": "Needs Attention",
    "availability_score": 35.0,
    "planogram_score": 9.8,
    "sos_score": 8.0,
    "pricing_score": 10.0,
    "promotion_score": 5.0,
    "deductions": [
      "[-] -5.2 pts: Product facings misaligned or grouping irregular",
      "[-] -12.0 pts: Shelf share (10.0%) is below agreed target (45.0%)",
      "[-] -5.0 pts: Promotional material damaged or placement suboptimal"
    ]
  },
  "shelf_metrics": {
    "pran_shelf_share_pct": 10.0,
    "competitor_shelf_share_pct": 90.0,
    "empty_shelf_share_pct": 0.0,
    "pran_facing_count": 2,
    "competitor_facing_count": 8,
    "arrangement_score": 65.0,
    "measurement_type": "estimated"
  },
  "pricing_and_promotions": [
    {
      "product": "PRAN Bisk Club Mama Cube Wafer Biscuits",
      "visible_price": 3.90,
      "promotion_detected": true,
      "pricing_violation": "not_evaluated",
      "confidence": "high"
    },
    {
      "product": "Julie's Love Letters Wafer Cubes",
      "visible_price": 5.50,
      "promotion_detected": false,
      "pricing_violation": "not_evaluated",
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
      "confidence": "high",
      "catalogue_suggestions": [
        {
          "sub_category_name": "272-Bisc-Cream",
          "sub_category_code": "272",
          "category_name": "145-Biscuit",
          "category_code": "145",
          "item_name": "Bisk Club Cream Biscuit Chocolate 90g",
          "item_code": "102210",
          "item_image_url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/102210_l.jpg.jpg",
          "confidence": 0.812
        }
      ],
      "matched": true
    },
    {
      "brand": "Julie's",
      "product_name": "Love Letters Wafer Cubes",
      "variant": "Chocolate Hazelnut",
      "sku": "COMP-JULIE-01",
      "facing_count": 4,
      "shelf_level": "middle",
      "confidence": "high",
      "catalogue_suggestions": null,
      "matched": false
    },
    {
      "brand": "Nabati",
      "product_name": "Bites Wafer Rolls",
      "variant": "Cheese",
      "sku": "COMP-NABATI-02",
      "facing_count": 4,
      "shelf_level": "bottom",
      "confidence": "high",
      "catalogue_suggestions": null,
      "matched": false
    }
  ],
  "issues_detected": [
    {
      "id": "e4a77914-fa06-47b1-b93b-52cb7ecae2bc",
      "issue": "PRAN shelf presence below target",
      "evidence": "Current PRAN shelf share is 10.0% vs target 45.0%",
      "severity": "MEDIUM",
      "confidence": "high",
      "recommended_action": "Review agreed shelf allocation and negotiate additional facings for core products.",
      "is_resolved": false
    },
    {
      "id": "31278fdc-567e-4074-b529-87a38b2512f4",
      "issue": "Shelf arrangement needs alignment",
      "evidence": "Shelf arrangement score assessed at 65.0/100",
      "severity": "MEDIUM",
      "confidence": "medium",
      "recommended_action": "Align product labels forward and group same-brand variants together.",
      "is_resolved": false
    }
  ],
  "availability_alerts": {
    "visible_gaps": [],
    "missing_expected_skus": [],
    "inventory_status": "unverified"
  },
  "recommended_actions": [
    "Review agreed shelf allocation and negotiate additional facings for core products.",
    "Align product labels forward and group same-brand variants together."
  ],
  "management_insights": [
    "SOS deficit of 35.0% indicates significant opportunity for facing expansion.",
    "Promotional wobbler 'BELI & MENANG' detected on shelf edge."
  ],
  "limitations": [
    "Single camera monocular perspective; metrics reflect visible front facings only."
  ],
  "human_verification_required": false,
  "token_usage": {
    "input_tokens": 1248,
    "output_tokens": 582,
    "total_tokens": 1830,
    "estimated_cost_usd": 0.003119,
    "cached": false
  },
  "image_url": "https://images.unsplash.com/photo-1578916171728-46686eac8d58?w=800",
  "created_at": "2026-10-26T14:30:00Z"
}
```

#### Error Responses

- **400 Bad Request** — Missing required image parameter
```json
{
  "detail": "image_url or image is required"
}
```

- **422 Unprocessable Entity** — Image file corrupt or decoding failure
```json
{
  "detail": "Failed to decode image from provided payload: cannot identify image file"
}
```

- **502 Bad Gateway** — Upstream Vision LLM Failure
```json
{
  "detail": "Upstream vision model error: Connection timed out to OpenRouter"
}
```

---

### 2. Analyze Shelf Photo (Binary File Upload)

```http
POST /api/v2/audit/analyze/file
```

Direct binary upload of a camera photograph using `multipart/form-data`. Streams image bytes purely through memory (`io.BytesIO`) without writing temporary files to local disk storage.

**Permissions:** Authenticated / Authorized Field Merchandiser

#### Request Body (`multipart/form-data`)

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | Binary image file (JPEG, PNG, WebP). Max recommended size: 20 MB. |
| `outlet_id` | string | No | UUID of pre-registered retail outlet. |
| `outlet_name` | string | No | Friendly outlet name. |
| `visit_id` | string | No | UUID linking audit to field merchandiser visit. |
| `audit_stage` | string | No | `STANDALONE`, `BEFORE`, or `AFTER`. Default: `STANDALONE`. |
| `target_pran_sos` | float | No | Target PRAN SOS percentage (default: `45.0`). |

#### Success Response — 200 OK

Returns identical `ShelfAuditReport` schema as detailed in `POST /api/v2/audit/analyze`.

#### Error Responses

- **400 Bad Request** — Invalid file format
```json
{
  "detail": "Invalid image format. Allowed formats: image/jpeg, image/png, image/webp"
}
```

---

### 3. Closed-Loop Verify Audit (Before / After Comparison)

```http
POST /api/v2/audit/verify
```

Validates corrective actions executed by a merchandiser during a store visit. Compares a baseline `BEFORE` audit against a newly captured `AFTER` photo, computing score improvement, resolved issues, remaining issues, and delta in PRAN Share of Shelf.

**Permissions:** Authenticated Field Merchandiser

#### Request Body (`application/json`)

| Field | Type | Required | Description |
|---|---|---|---|
| `audit_id` | string | Yes | UUID of the initial `BEFORE` shelf audit. |
| `after_image_url` | string | Yes | Image URL or Base64 string of shelf photo captured after fixes. |
| `completed_actions` | array[string] | No | List of action strings marked as completed by the merchandiser. |

#### Success Response — 200 OK

```json
{
  "visit_id": "a9082bd8-a6d9-4824-a740-fcbc84fbfbc9",
  "before_audit_id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
  "after_audit_id": "99df2194-c119-4cb3-a9d5-7ceb2c3a388b",
  "before_score": 67.8,
  "after_score": 86.4,
  "score_improvement": 18.6,
  "is_audit_passed": true,
  "resolved_issues": [
    "Shelf arrangement needs alignment",
    "PRAN shelf presence below target"
  ],
  "remaining_issues": [],
  "pran_sos_delta_pct": 18.0,
  "after_report": {
    "audit_id": "99df2194-c119-4cb3-a9d5-7ceb2c3a388b",
    "audit_stage": "AFTER",
    "retail_execution_score": {
      "score": 86.4,
      "grade": "Good",
      "availability_score": 35.0,
      "planogram_score": 23.4,
      "sos_score": 18.0,
      "pricing_score": 10.0,
      "promotion_score": 0.0,
      "deductions": []
    },
    "shelf_metrics": {
      "pran_shelf_share_pct": 28.0,
      "competitor_shelf_share_pct": 72.0,
      "empty_shelf_share_pct": 0.0,
      "pran_facing_count": 6,
      "competitor_facing_count": 8,
      "arrangement_score": 90.0,
      "measurement_type": "estimated"
    }
  }
}
```

#### Error Responses

- **404 Not Found** — Initial audit record not found
```json
{
  "detail": "Before audit with ID 041b7223-9fd2-4d53-ab59-8e232beaa69d not found"
}
```

---

### 4. Retrieve Audit Report

```http
GET /api/v2/audit/{audit_id}
```

Retrieves the persisted, structured `ShelfAuditReport` for a given audit session UUID.

**Permissions:** Authenticated

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `audit_id` | string (UUID) | Yes | Unique identifier of the shelf audit. |

#### Success Response — 200 OK

Returns the complete `ShelfAuditReport` JSON structure.

#### Error Responses

- **404 Not Found**
```json
{
  "detail": "Audit 041b7223-9fd2-4d53-ab59-8e232beaa69d not found"
}
```

---

### 5. List Shelf Audits

```http
GET /api/v2/audits
```

Returns a paginated list of shelf audits with high-level scores and status filters.

**Permissions:** Authenticated

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `outlet_id` | string | No | Filter by specific retail outlet UUID. |
| `stage` | string | No | Filter by audit stage: `STANDALONE`, `BEFORE`, `AFTER`. |
| `status` | string | No | Filter by summary status: `needs_attention`, `acceptable`. |
| `limit` | integer | No | Maximum records to return (default: `50`, max: `200`). |
| `offset` | integer | No | Offset for pagination (default: `0`). |

#### Success Response — 200 OK

```json
{
  "total": 128,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
      "outlet_id": "OUTLET-104",
      "outlet_name": "Metro Hypermarket",
      "audit_stage": "STANDALONE",
      "status": "needs_attention",
      "res_score": 67.8,
      "pran_shelf_share_pct": 10.0,
      "competitor_shelf_share_pct": 90.0,
      "created_at": "2026-10-26T14:30:00Z"
    }
  ]
}
```

---

### 6. Management Analytics Dashboard

```http
GET /api/v2/management/dashboard
```

Delivers aggregated executive business intelligence across 5 analytical modules: Executive Overview, Category Shelf Intelligence, Product Availability, Merchandiser Performance, and Exception-First Store Rankings.

**Permissions:** Executive / Territory Manager

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `territory` | string | No | Filter data by territory name (e.g., `Dhaka North`, `Chittagong Central`). |
| `category` | string | No | Filter by product category (e.g., `Biscuit`, `Beverage`, `Culinary`). |

#### Success Response — 200 OK

```json
{
  "executive_overview": {
    "total_audits": 48,
    "outlets_covered": 32,
    "avg_pran_sos_pct": 38.5,
    "avg_execution_score": 74.2,
    "issue_resolution_rate_pct": 82.5,
    "failed_audits_count": 6
  },
  "shelf_intelligence": {
    "pran_overall_sos_pct": 38.5,
    "competitor_overall_sos_pct": 61.5,
    "golden_shelf_eye_level_pran_pct": 29.4,
    "category_breakdown": [
      {
        "category_name": "Biscuit",
        "pran_share_pct": 34.0,
        "competitor_share_pct": 66.0
      },
      {
        "category_name": "Beverage",
        "pran_share_pct": 48.0,
        "competitor_share_pct": 52.0
      }
    ]
  },
  "product_availability": {
    "on_shelf_availability_pct": 89.2,
    "out_of_stock_incidents": 14,
    "top_missing_skus": [
      {
        "sku": "100125",
        "item_name": "PRAN Frooto Mango Juice 250ml",
        "missing_occurrences": 8
      }
    ]
  },
  "exception_outlets": [
    {
      "outlet_id": "OUTLET-104",
      "outlet_name": "Metro Hypermarket",
      "territory": "Dhaka Central",
      "res_score": 67.8,
      "urgency": "HIGH",
      "primary_issue": "PRAN shelf presence below target: 10.0% vs 45.0% target"
    }
  ]
}
```

---

### 7. List Retail Outlets

```http
GET /api/v2/outlets
```

Lists master retail outlets registered in the system along with target SOS and expected SKU configurations.

**Permissions:** Authenticated

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `territory` | string | No | Filter by territory name. |
| `limit` | integer | No | Maximum records (default: `50`). |

#### Success Response — 200 OK

```json
[
  {
    "id": "OUTLET-104",
    "outlet_name": "Metro Hypermarket",
    "outlet_type": "Supermarket",
    "territory": "Dhaka Central",
    "distributor_name": "PRAN Regional Distribution",
    "tier": "Tier 1 - Platinum",
    "target_pran_sos_pct": 45.0,
    "expected_skus": ["102210", "100125", "105541"]
  }
]
```

---

### 8. Master Catalogue Hybrid Search

```http
GET /catalogue/search
```

Performs low-latency (<15ms) hybrid vector and BM25 search over the 1,954 products in `itemsdb.csv`. Combines 384-dimensional cosine similarity (`all-MiniLM-L6-v2`) with lexical keyword matching. Consumes 0 remote LLM tokens.

**Permissions:** Authenticated

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Product search query string (e.g. `Mama Wafer`, `Frooto`, `Potata`). |
| `limit` | integer | No | Number of top candidates to return (default: `5`, max: `25`). |

#### Success Response — 200 OK

```json
{
  "query": "Mama Wafer",
  "total_matches": 5,
  "results": [
    {
      "item_code": "102210",
      "item_name": "Bisk Club Cream Biscuit Chocolate 90g",
      "category_code": "145",
      "category_name": "145-Biscuit",
      "sub_category_code": "272",
      "sub_category_name": "272-Bisc-Cream",
      "confidence": 0.812,
      "item_image_url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/102210_l.jpg.jpg"
    }
  ]
}
```

---

### 9. Master Catalogue Status & Rebuild

#### `GET /catalogue/stats`
Returns total indexed products, vector dimension, and embedding cache readiness.

#### `POST /catalogue/rebuild`
Re-reads `itemsdb.csv` and regenerates pre-computed embeddings.

---

### 10. Token Telemetry & Cost Accounting Endpoints

#### `GET /tokens/models`
Returns currently active AI models, context windows, and configured token pricing rates.

#### `GET /tokens/pricing`
Returns pricing per 1,000 and 1,000,000 tokens for prompt input and completion output.

#### `GET /tokens/stats`
Returns cumulative token consumption, total scans processed, and total USD expenditure.

#### `POST /tokens/estimate`
Calculates estimated token usage and USD cost for a given image resolution prior to calling vision inference.

---

## Field References & Calculation Logic

### 1. `shelf_metrics` Reference

The `shelf_metrics` object encapsulates geometric and perceptual measurements extracted from the shelf image.

| Field | Type | Description |
|---|---|---|
| `pran_shelf_share_pct` | float | Percentage of total visible shelf width occupied by PRAN-branded items. Range: `0.0` to `100.0`. |
| `competitor_shelf_share_pct` | float | Percentage of visible shelf width occupied by competing FMCG brands. Range: `0.0` to `100.0`. |
| `empty_shelf_share_pct` | float | Percentage of visible shelf space currently unoccupied due to stockouts or removed items. |
| `pran_facing_count` | integer | Total integer count of front-facing PRAN product packages. |
| `competitor_facing_count` | integer | Total integer count of front-facing competitor product packages. |
| `arrangement_score` | float | Visual cleanliness score out of 100 based on alignment, variant clustering, and front-facing presentation. |
| `measurement_type` | string | `measured` if calibrated boundary data is present; `estimated` for single monocular camera vision. |

---

### 2. How `shelf_metrics` Is Calculated

In `app/services/ai_service.py`, `shelf_metrics` is computed via deterministic post-processing:

#### Facing Count Aggregation
1. The vision model identifies and segments every distinct product cluster on the shelf.
2. Sum of facings for items where `brand == "PRAN"` yields `pran_facing_count`.
3. Sum of facings for non-PRAN or competitor items yields `competitor_facing_count`.

#### Share of Shelf (SOS) Calculation
The model estimates horizontal linear space occupancy. If linear share is omitted or unverified, it falls back to facing ratio:

```text
pran_shelf_share_pct = (pran_facing_count / total_facing_count) * 100.0
competitor_shelf_share_pct = max(0.0, 100.0 - pran_shelf_share_pct - empty_shelf_share_pct)
```

#### Arrangement Score Normalization
The visual arrangement score evaluates whether products are neatly aligned, labels are oriented forward, and same-variant items are grouped together.
- If the model scores on a 0-10 scale (e.g. `6.5`), it is normalized: `6.5 * 10.0 = 65.0 / 100`.
- If omitted, fallback formula applies: `arrangement_score = max(50.0, min(95.0, 85.0 - (visible_gaps * 5.0)))`.

---

### 3. Walkthrough with Scenario Example

Consider the audited wafer shelf fixture:
```json
"shelf_metrics": {
  "pran_shelf_share_pct": 10.0,
  "competitor_shelf_share_pct": 90.0,
  "empty_shelf_share_pct": 0.0,
  "pran_facing_count": 2,
  "competitor_facing_count": 8,
  "arrangement_score": 65.0,
  "measurement_type": "estimated"
}
```

1. **Facing Counts**:
   - `pran_facing_count = 2` (2 facings of PRAN Bisk Club Mama Cube Wafers).
   - `competitor_facing_count = 8` (4 facings of Julie's Love Letters + 4 facings of Nabati Bites).
   - Total Facings = 10.
2. **Share of Shelf**:
   - PRAN packages occupy 10% of linear horizontal shelf space.
   - Competitors occupy 90% of linear horizontal shelf space.
   - `pran_shelf_share_pct = 10.0%`
   - `competitor_shelf_share_pct = 90.0%`
   - `empty_shelf_share_pct = 0.0%` (fully stocked shelf).
3. **Arrangement Score**:
   - Model assesses label alignment and product clustering at 6.5 / 10 due to competitor encroachment.
   - Normalization: `6.5 * 10.0 = 65.0 / 100`.

---

### 4. How `shelf_metrics` Drives Retail Execution Score (RES)

In `app/services/rules_engine.py`, the Retail Execution Score is computed deterministically across 5 commercial pillars:

$$\text{RES} = \text{Availability (35)} + \text{Planogram (25)} + \text{SOS (20)} + \text{Pricing (10)} + \text{Promotion (10)}$$

#### Pillar 1: Availability Pillar (Max 35.0 pts)
- Base score = `35.0` pts.
- Deductions: Missing expected SKUs (`-5.0` pts each) and visible stockout gaps (`-4.0` pts each).
- With 0 missing SKUs and 0 gaps: `Availability Score = 35.0 / 35.0`.

#### Pillar 2: Planogram & Arrangement Pillar (Max 25.0 pts)
- **Arrangement sub-component (15.0 pts max)**:
  $$\text{Arrangement Sub-Score} = \left(\frac{\text{arrangement\_score}}{100.0}\right) \times 15.0 = \left(\frac{65.0}{100.0}\right) \times 15.0 = 9.75 \text{ pts}$$
  $$\text{Deduction} = 15.0 - 9.75 = 5.25 \text{ pts}$$
  Logged as: `[-] -5.2 pts: Product facings misaligned or grouping irregular`
- **Eye-Level Placement sub-component (10.0 pts max)**:
  PRAN items detected on top shelf; Competitor items occupy prime eye-level tier.
- Resulting Planogram Score: `9.8 / 25.0`.

#### Pillar 3: Share of Shelf (SOS) Pillar (Max 20.0 pts)
- Base score = `20.0` pts.
- Agreed Target PRAN SOS: `45.0%`.
- Actual Measured PRAN SOS: `10.0%`.
- Deficit:
  $$\text{Deficit} = 45.0\% - 10.0\% = 35.0\%$$
  $$\text{Penalty} = \min(35.0 \times 0.5, 12.0) = 12.0 \text{ pts (Max Cap)}$$
  $$\text{SOS Score} = 20.0 - 12.0 = 8.0 / 20.0 \text{ pts}$$
- Generates Issue: `[MEDIUM] PRAN shelf presence below target: Current PRAN shelf share is 10.0% vs target 45%`.

#### Pillar 4: Pricing Compliance Pillar (Max 10.0 pts)
- Base score = `10.0` pts.
- No approved price violations detected: `Pricing Score = 10.0 / 10.0`.

#### Pillar 5: Promotion & POSM Pillar (Max 10.0 pts)
- Base score = `10.0` pts.
- Wobbler detected but suboptimal positioning: `5.0 / 10.0`.

#### Total Composite Score:
$$\text{Total Score} = 35.0 + 9.8 + 8.0 + 10.0 + 5.0 = 67.8 / 100$$
$$\text{Grade} = \text{"Needs Attention"} \quad (65 \le \text{Score} < 80)$$

---

## Model Reference

### 1. `ShelfAudit` (Table: `shelf_audits`)

Stores every individual shelf photograph audit session.

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | string (UUID) | System | Primary key. |
| `visit_id` | string (UUID) | Yes | Optional foreign key to `merchandiser_visits`. |
| `outlet_id` | string | Yes | Foreign key to `outlets`. |
| `audit_stage` | string | Yes | Audit phase: `STANDALONE`, `BEFORE`, or `AFTER`. |
| `image_url` | string | Yes | Image URL or storage reference. |
| `status` | string | System | Audit summary status (`needs_attention`, `acceptable`). |
| `res_score` | float | System | Retail Execution Score (0-100). |
| `pran_shelf_share_pct` | float | System | PRAN linear shelf share percentage. |
| `competitor_shelf_share_pct` | float | System | Competitor linear shelf share percentage. |
| `empty_shelf_share_pct` | float | System | Empty shelf percentage. |
| `pran_facing_count` | integer | System | Number of PRAN front facings. |
| `competitor_facing_count` | integer | System | Number of competitor front facings. |
| `arrangement_score` | float | System | Cleanliness / arrangement score (0-100). |
| `missing_skus_count` | integer | System | Total missing expected SKUs. |
| `visible_gaps_count` | integer | System | Total visible shelf gaps. |
| `issues_count` | integer | System | Total flagged issues count. |
| `full_analysis` | JSON | System | Complete serialized `ShelfAuditReport` payload. |
| `input_tokens` | integer | System | Prompt tokens consumed by vision model. |
| `output_tokens` | integer | System | Completion tokens consumed by vision model. |
| `total_tokens` | integer | System | Total tokens consumed. |
| `estimated_cost_usd` | float | System | USD cost of vision API call. |
| `created_at` | datetime | System | UTC creation timestamp. |
| `updated_at` | datetime | System | UTC last update timestamp. |

---

### 2. `AuditIssueRecord` (Table: `audit_issues`)

Stores individual merchandising issues extracted from an audit.

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | string (UUID) | System | Primary key. |
| `audit_id` | string (UUID) | Yes | Foreign key to `shelf_audits`. |
| `severity` | string | System | Severity level: `HIGH`, `MEDIUM`, or `LOW`. |
| `issue` | text | System | Concise issue title. |
| `evidence` | text | System | Quantitative evidence string. |
| `recommended_action` | text | System | Prescriptive corrective step for merchandiser. |
| `is_resolved` | integer | Yes | `0` = unresolved, `1` = resolved. |
| `created_at` | datetime | System | UTC creation timestamp. |

---

### 3. `Outlet` (Table: `outlets`)

Retail store master data.

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | string | Yes | Unique store identifier (e.g. `OUTLET-104`). |
| `outlet_name` | string | Yes | Retail store name. |
| `outlet_type` | string | Yes | Store format (e.g., `Supermarket`, `General Store`). |
| `territory` | string | Yes | Regional territory (e.g., `Dhaka Central`). |
| `distributor_name` | string | Yes | Regional distributor name. |
| `tier` | string | Yes | Store tier (e.g., `Tier 1 - Platinum`, `Tier 2 - Gold`). |
| `target_pran_sos_pct` | float | Yes | Target PRAN Share of Shelf percentage (default: `45.0`). |
| `expected_skus` | JSON (list) | Yes | List of mandatory SKU codes expected on shelf. |
| `created_at` | datetime | System | UTC registration timestamp. |

---

### 4. `Visit` (Table: `merchandiser_visits`)

Tracks field merchandiser visits to retail outlets.

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | string (UUID) | System | Primary key. |
| `outlet_id` | string | Yes | Foreign key to `outlets`. |
| `merchandiser_id` | string | Yes | Merchandiser identifier. |
| `merchandiser_name` | string | Yes | Merchandiser display name. |
| `visit_date` | datetime | Yes | Visit timestamp. |
| `status` | string | Yes | `IN_PROGRESS`, `COMPLETED`, or `FLAGGED`. |
| `initial_score` | float | System | Retail Execution Score from `BEFORE` audit. |
| `final_score` | float | System | Retail Execution Score from `AFTER` audit. |
| `created_at` | datetime | System | UTC creation timestamp. |

---

## URL Configuration

Summary of all available API routes across the application:

| HTTP Method | URL Path | Handler / Name | Description |
|---|---|---|---|
| `GET` | `/health` | `health_check` | Server liveness probe. |
| `POST` | `/api/v2/audit/analyze` | `analyze_shelf_audit_v2` | Primary shelf analysis from URL or Base64. |
| `POST` | `/api/v2/audit/analyze/file` | `analyze_shelf_audit_file_v2` | Binary photo file upload analysis. |
| `POST` | `/api/v2/audit/verify` | `verify_audit_improvement` | Closed-loop before/after verification. |
| `GET` | `/api/v2/audit/{audit_id}` | `get_audit_report` | Retrieve complete audit report by UUID. |
| `GET` | `/api/v2/audits` | `list_audits` | Paginated list of historic audits. |
| `GET` | `/api/v2/management/dashboard` | `get_management_dashboard` | Executive 5-module BI dashboard. |
| `GET` | `/api/v2/outlets` | `list_outlets` | Master retail outlets list. |
| `GET` | `/catalogue/search` | `catalogue_search` | Hybrid vector + BM25 catalogue search. |
| `GET` | `/catalogue/stats` | `catalogue_stats` | Catalogue embedding readiness and stats. |
| `POST` | `/catalogue/rebuild` | `catalogue_rebuild` | Force rebuild of catalogue vector index. |
| `GET` | `/tokens/models` | `get_models` | Active AI models and pricing configuration. |
| `GET` | `/tokens/pricing` | `get_pricing` | Token rates per 1k and 1M tokens. |
| `GET` | `/tokens/stats` | `get_token_stats` | Aggregated token usage and USD expenditure. |
| `POST` | `/tokens/estimate` | `estimate_tokens` | Pre-inference cost and token estimation. |

---

## Error Reference

| HTTP Status | Error Detail Code | Description | Corrective Action |
|---|---|---|---|
| `400 Bad Request` | `INVALID_INPUT` | Missing required `image_url` or uploaded file. | Provide a valid Base64 string, HTTP URL, or binary file. |
| `400 Bad Request` | `INVALID_FILE_TYPE` | Uploaded file MIME type is not an allowed image format. | Upload JPEG, PNG, or WebP images only. |
| `404 Not Found` | `AUDIT_NOT_FOUND` | Specified `audit_id` does not exist in the database. | Verify the audit UUID. |
| `404 Not Found` | `OUTLET_NOT_FOUND` | Specified `outlet_id` does not exist in master records. | Verify the outlet ID or omit to run standalone audit. |
| `422 Unprocessable Entity` | `IMAGE_DECODE_ERROR` | Image payload is corrupt or unreadable by PIL. | Ensure Base64 string is correctly padded and uncorrupted. |
| `500 Internal Server Error` | `RULES_ENGINE_FAILURE` | Error during deterministic scoring or catalogue linking. | Inspect server logs for details. |
| `502 Bad Gateway` | `UPSTREAM_VISION_ERROR` | Remote vision LLM API call timed out or failed. | Verify OpenRouter API key and upstream provider status. |

---

## Dependencies & Core Technologies

- **Web Framework**: FastAPI (Asynchronous Python 3.11+)
- **ORM & Database**: SQLAlchemy 2.0 with PostgreSQL 16
- **Vision AI Perception**: Google Gemini 3.7 Flash via OpenRouter API
- **In-Memory Image Processing**: Pillow 10.x (Lanczos Antialiasing, RAM-only streams)
- **Local Catalogue Matching**: `sentence-transformers` (`all-MiniLM-L6-v2`) + `rank_bm25` (`BM25Okapi`) over 1,954 SKUs in `itemsdb.csv`
- **Data Validation**: Pydantic v2
