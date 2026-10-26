# PRAN-RFL Retail Merchandising & Shelf Intelligence API Documentation

## Overview

The PRAN-RFL Retail Merchandising & Shelf Intelligence module operates in **image-only, detect-all-PRAN-products mode**. The product of the system is answering: **"What PRAN products are on this shelf and how much space do they take?"** Rather than gating evaluation against rigid pre-configured store lists, the system accepts raw shelf photos, segments all visible products, cleanly decouples brand identification from catalogue SKU linking, and executes deterministic Retail Execution Scoring across six image-provable commercial pillars.

Master catalogue matching against the 1,954 SKUs in `itemsdb.csv` acts as enrichment rather than a gate. Any PRAN item not present in the master catalogue is classified into the `unknown_pran` bucket, ensuring Share of Shelf and facing counts remain 100% accurate without data corruption.

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

In image-only mode, endpoints require only the shelf photo itself; outlet configuration is optional.

---

## Endpoints

### 1. Analyze Shelf Photo (URL or Base64)

```http
POST /api/v2/audit/analyze
```

Performs synchronous shelf intelligence analysis from an image URL (HTTP/HTTPS, S3 presigned URL) or Base64-encoded image string. The image is processed in RAM (Lanczos downscaling to 1024px maximum dimension), fingerprinted via SHA-256 for exact-match caching, analyzed via Gemini 3.7 Flash vision perception, enriched with brand and catalogue status, evaluated across image-provable pillars, and persisted to the relational database.

**Permissions:** Authenticated / Authorized Field Merchandiser

#### Request Body (`application/json`)

| Field | Type | Required | Description |
|---|---|---|---|
| `image_url` | string | Yes | S3 presigned URL, HTTP/HTTPS URL, Data URI, or raw Base64 string. |
| `outlet_id` | string | No | Optional UUID or string identifier of a registered retail outlet. |
| `outlet_name` | string | No | Optional friendly store or gondola name. |
| `visit_id` | string | No | Optional UUID linking this audit to an active field visit. |
| `audit_stage` | string | No | Stage of audit: `STANDALONE`, `BEFORE`, or `AFTER`. Default: `STANDALONE`. |
| `target_pran_sos` | float | No | Reference target PRAN Share of Shelf percentage (default: `30.0`). |

#### Choices / Enums: `BrandClass`

| Value | Description |
|---|---|
| `pran` | Confirmed PRAN brand item verified via text, logo, sub-brand, or pack design. |
| `competitor` | Identified competitor commercial brand (Julie's, Nabati, Jacker, Oreo, etc.). |
| `unknown` | Unidentifiable package or brand without clear branding text. |

#### Choices / Enums: `SkuStatus`

| Value | Description |
|---|---|
| `matched` | High-confidence catalogue match (score >= 0.78 with margin >= 0.05). |
| `candidate` | Plausible catalogue match (score >= 0.55); top 3 candidates attached. |
| `unknown_pran` | Confirmed PRAN brand item not found in `itemsdb.csv`. Still counted as PRAN facings. |
| `n/a` | Competitor product; catalogue matching not applicable. |

#### Success Response — 200 OK (PRAN Detected on Shelf)

```json
{
  "audit_id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
  "input_context": "image_only",
  "fixture_category": "biscuit",
  "image_quality": {
    "score": 0.88,
    "issues": []
  },
  "pran_summary": {
    "pran_detected": true,
    "distinct_products": 2,
    "total_facings": 6,
    "sos_pct": 35.0,
    "eye_level_share_pct": 50.0,
    "sku_breakdown": [
      {
        "product_name": "PRAN Bisk Club Mama Cube Wafer Biscuits",
        "sku_status": "matched",
        "sku_code": "102210",
        "facings": 4,
        "levels": ["eye_level", "middle"]
      },
      {
        "product_name": "PRAN Frooto Mango Juice 250ml",
        "sku_status": "matched",
        "sku_code": "100125",
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
    {
      "brand": "Julie's",
      "facings": 8
    },
    {
      "brand": "Nabati",
      "facings": 4
    }
  ],
  "retail_execution_score": {
    "score": 72.1,
    "grade": "Good",
    "mode": "image_only",
    "pillars": {
      "presence": 14.0,
      "visibility_sos": 25.0,
      "placement": 10.0,
      "arrangement": 13.1,
      "stock_health": 10.0,
      "price_promo": 0.0
    },
    "deductions": [
      "[-] -6.0 pts: Low PRAN product variety (2 distinct SKU(s) visible)",
      "[-] -10.0 pts: Weak Golden Zone placement (50% of PRAN items at eye level)",
      "[-] -3.0 pts: No visible promotional signage, talkers, or wobblers detected"
    ]
  },
  "shelf_metrics": {
    "pran_shelf_share_pct": 35.0,
    "competitor_shelf_share_pct": 65.0,
    "empty_shelf_share_pct": 0.0,
    "pran_facing_count": 6,
    "competitor_facing_count": 12,
    "arrangement_score": 87.0,
    "measurement_type": "estimated"
  },
  "detected_products": [
    {
      "brand": "PRAN",
      "brand_class": "pran",
      "brand_evidence": "logo: PRAN, text: 'Bisk Club Mama Cube'",
      "brand_confidence": 0.95,
      "product_name": "PRAN Bisk Club Mama Cube Wafer Biscuits",
      "variant": "Chocolate",
      "sku": "102210",
      "sku_code": "102210",
      "sku_status": "matched",
      "facing_count": 4,
      "shelf_level": "eye_level",
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
      "brand_class": "competitor",
      "brand_evidence": "text: 'Julie's Love Letters'",
      "brand_confidence": 0.95,
      "product_name": "Julie's Love Letters Wafer Cubes",
      "variant": "Chocolate Hazelnut",
      "sku": null,
      "sku_code": null,
      "sku_status": "n/a",
      "facing_count": 8,
      "shelf_level": "middle",
      "confidence": "high",
      "catalogue_suggestions": null,
      "matched": false
    }
  ],
  "issues_detected": [
    {
      "id": "e4a77914-fa06-47b1-b93b-52cb7ecae2bc",
      "issue": "Low eye-level Golden Zone presence",
      "evidence": "Only 3 of 6 PRAN facings are placed at prime eye level",
      "severity": "MEDIUM",
      "confidence": "high",
      "recommended_action": "Negotiate moving high-margin PRAN core items to eye-level shelf tiers.",
      "is_resolved": false
    }
  ],
  "availability_alerts": {
    "visible_gaps": [],
    "missing_expected_skus": [],
    "inventory_status": "unverified"
  },
  "recommended_actions": [
    "Relocate high-velocity PRAN SKUs to middle eye-level shelf tier."
  ],
  "management_insights": [
    "PRAN product variety is limited to 2 SKU(s) on this shelf fixture.",
    "Healthy eye-level placement: 3 facings (50%) situated in the Golden Zone."
  ],
  "human_verification_required": false,
  "token_usage": {
    "input_tokens": 1248,
    "output_tokens": 582,
    "total_tokens": 1830,
    "estimated_cost_usd": 0.003119,
    "cached": false
  },
  "created_at": "2026-10-26T15:00:00Z"
}
```

#### Success Response — 200 OK (Special Case: No PRAN Detected on Shelf)

When zero PRAN products are visible, no unearned points are awarded from secondary pillars. The endpoint returns `score: null` and status `no_pran_detected`:

```json
{
  "audit_id": "c2ff9ad0-9a69-4e95-95fa-e081213691eb",
  "input_context": "image_only",
  "fixture_category": "general",
  "image_quality": {
    "score": 0.80,
    "issues": ["partial_view"]
  },
  "pran_summary": {
    "pran_detected": false,
    "distinct_products": 0,
    "total_facings": 0,
    "sos_pct": 0.0,
    "eye_level_share_pct": 0.0,
    "sku_breakdown": []
  },
  "catalogue_coverage": {
    "pran_facings_total": 0,
    "matched": 0,
    "candidate": 0,
    "unknown_pran": 0
  },
  "competitors": [
    {
      "brand": "Kellogg's",
      "facings": 10
    },
    {
      "brand": "Nestle",
      "facings": 8
    }
  ],
  "retail_execution_score": {
    "score": null,
    "grade": "No PRAN Detected",
    "mode": "image_only",
    "pillars": {
      "presence": 0.0,
      "visibility_sos": 0.0,
      "placement": 0.0,
      "arrangement": 0.0,
      "stock_health": 0.0,
      "price_promo": 0.0
    },
    "deductions": [
      "No PRAN products visible on shelf fixture"
    ]
  },
  "audit_summary": {
    "status": "no_pran_detected",
    "image_quality": "good",
    "key_findings": [
      "No PRAN products visible; this may be a competitor-only fixture or the wrong photo."
    ]
  },
  "human_verification_required": false
}
```

#### Error Responses

- **400 Bad Request** — Missing image parameter
```json
{
  "detail": "image_url or image is required"
}
```

- **422 Unprocessable Entity** — Corrupted image bytes
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
| `file` | file | Yes | Binary image file (JPEG, PNG, WebP). |
| `outlet_id` | string | No | Optional UUID of pre-registered retail outlet. |
| `outlet_name` | string | No | Optional friendly outlet name. |
| `target_pran_sos` | float | No | Reference target PRAN SOS percentage (default: `30.0`). |

#### Success Response — 200 OK

Returns identical `ShelfAuditReport` schema as detailed in `POST /api/v2/audit/analyze`.

---

### 3. Closed-Loop Verify Audit (Before / After Comparison)

```http
POST /api/v2/audit/verify
```

Validates corrective merchandising actions by comparing a baseline `BEFORE` audit against a newly captured `AFTER` photo.

**Permissions:** Authenticated Field Merchandiser

#### Request Body (`application/json`)

| Field | Type | Required | Description |
|---|---|---|---|
| `audit_id` | string | Yes | UUID of the initial `BEFORE` shelf audit. |
| `after_image_url` | string | Yes | Image URL or Base64 string of shelf photo captured after fixes. |
| `completed_actions` | array[string] | No | Optional list of action strings marked as completed. |

#### Success Response — 200 OK

```json
{
  "visit_id": "a9082bd8-a6d9-4824-a740-fcbc84fbfbc9",
  "before_audit_id": "041b7223-9fd2-4d53-ab59-8e232beaa69d",
  "after_audit_id": "99df2194-c119-4cb3-a9d5-7ceb2c3a388b",
  "before_score": 58.2,
  "after_score": 78.4,
  "score_improvement": 20.2,
  "is_audit_passed": true,
  "resolved_issues": [
    "Low eye-level Golden Zone presence"
  ],
  "remaining_issues": [],
  "pran_sos_delta_pct": 12.0,
  "after_report": { ... }
}
```

---

### 4. Retrieve Audit Report

```http
GET /api/v2/audit/{audit_id}
```

Retrieves the persisted, structured `ShelfAuditReport` for a given audit session UUID.

**Permissions:** Authenticated

---

### 5. List Shelf Audits

```http
GET /api/v2/audits
```

Returns a paginated list of shelf audits with high-level scores and status filters.

**Permissions:** Authenticated

---

### 6. Management Analytics Dashboard

```http
GET /api/v2/management/dashboard
```

Delivers aggregated executive business intelligence across 5 analytical modules: Executive Overview, Category Shelf Intelligence, Product Availability, Merchandiser Performance, and Exception-First Store Rankings.

**Permissions:** Executive / Territory Manager

---

### 7. List Retail Outlets

```http
GET /api/v2/outlets
```

Lists master retail outlets registered in the system along with target SOS and expected SKU configurations.

**Permissions:** Authenticated

---

### 8. Master Catalogue Hybrid Search

```http
GET /catalogue/search
```

Performs low-latency (<15ms) hybrid vector and BM25 search over the 1,954 products in `itemsdb.csv`. Combines 384-dimensional cosine similarity (`all-MiniLM-L6-v2`) with lexical keyword matching. Consumes 0 remote LLM tokens.

**Permissions:** Authenticated

---

### 9. Token Telemetry & Cost Accounting Endpoints

#### `GET /tokens/models`
Returns active AI models, context windows, and configured token pricing rates.

#### `GET /tokens/pricing`
Returns pricing per 1,000 and 1,000,000 tokens for prompt input and completion output.

#### `GET /tokens/stats`
Returns cumulative token consumption, total scans processed, and total USD expenditure.

#### `POST /tokens/estimate`
Pre-calculates estimated vision tokens, system prompt tokens, and expected USD cost for a given image resolution before execution.

---

## Field References & Calculation Logic

### 1. Brand Identity vs. SKU Identity Separation

In image-only mode, brand identity and SKU identity are strictly decoupled:

$$\text{BrandClass} \in \{\text{PRAN}, \text{COMPETITOR}, \text{UNKNOWN}\}$$
$$\text{SkuStatus} \in \{\text{MATCHED}, \text{CANDIDATE}, \text{UNKNOWN\_PRAN}, \text{NOT\_APPLICABLE}\}$$

- Front facing counts depend **strictly** on `brand_class`:
  $$\text{pran\_facings} = \sum_{d \in \text{detections}} d.\text{facing\_count} \quad [\text{where } d.\text{brand\_class} == \text{BrandClass.PRAN}]$$
  $$\text{comp\_facings} = \sum_{d \in \text{detections}} d.\text{facing\_count} \quad [\text{where } d.\text{brand\_class} == \text{BrandClass.COMPETITOR}]$$
  $$\text{unknown\_facings} = \sum_{d \in \text{detections}} d.\text{facing\_count} \quad [\text{where } d.\text{brand\_class} == \text{BrandClass.UNKNOWN}]$$
- A missing catalogue entry or poor embedding similarity score never flips the brand identity or corrupts Share of Shelf metrics. Unmatched PRAN products are classified as `unknown_pran` and still fully counted in PRAN facings.

### 2. Six Image-Provable RES Scoring Pillars

All outside assumptions (such as assumed store planograms, manual SKU lists, or pre-entered price lists) are removed. The Retail Execution Score evaluates only what can be proven from the photograph:

| Pillar | Metric Evaluated | Max Points | Exact Formula | Deduction Threshold |
|---|---|---|---|---|
| **1. Presence** | PRAN detected at all; number of distinct products & variety | 20.0 | $\min(20.0, 8.0 + (\text{distinct\_skus} \times 3.0))$ | Deduction if score < 14.0 pts: `[-] -X pts: Low PRAN product variety (N distinct SKU(s) visible)` |
| **2. Visibility / SOS** | PRAN linear share of shelf vs configurable benchmark (default: 30%) | 25.0 | $\min\left(25.0, \frac{\text{pran\_sos}}{\text{benchmark\_sos}} \times 25.0\right)$ | Deduction if SOS < 15.0%: `[-] -X pts: Low PRAN shelf share (X% vs reference benchmark 30.0%)` |
| **3. Placement** | PRAN facings at eye level (Golden Zone) vs top / bottom | 20.0 | $\left(\frac{\text{pran\_eye\_facings}}{\max(\text{pran\_facings}, 1)}\right) \times 20.0$ | Deduction if eye ratio < 35%: `[-] -X pts: Weak Golden Zone placement (X% of PRAN items at eye level)` |
| **4. Arrangement** | Front-facing labels, variant clustering, no stacking mess | 15.0 | $\left(\frac{\text{arrangement\_score}}{100.0}\right) \times 15.0$ | Deduction if arrangement < 70/100: `[-] -X pts: Product facings misaligned or irregular grouping` |
| **5. Stock Health** | Visible voids and empty shelf percentage around PRAN items | 10.0 | $\max(0.0, 10.0 - \min(\text{empty\_pct} \times 0.2, 5.0) - \min(\text{gaps} \times 2.0, 5.0))$ | Deduction if gaps > 0 or empty space > 5%: `[-] -X pts: Stock health penalty (N gap(s), X% empty space)` |
| **6. Price & Promo** | Visible shelf price tags near PRAN items; wobblers/talkers present | 10.0 | $(\text{price\_tag\_ratio} \times 7.0) + (3.0 \text{ if promo visible else } 0.0)$ | Deduction if no price tags (-7.0 pts) or no promo wobblers (-3.0 pts) |

---

### 3. Step-by-Step Scoring Mechanics

#### Case A: PRAN Products Visible on Shelf (`pran_facings > 0`)

1. **Pillar 1: Presence (Max 20.0 pts)**
   - Awarded base **8.0 points** for PRAN brand presence on the fixture.
   - Earns **+3.0 points** for each distinct PRAN SKU identified in frame (`pran_summary.distinct_products`).
   - Capped at **20.0 points** ($\ge 4$ distinct SKUs earns full points).
   - If earned score is below 14.0 points, logs deduction: `[-] -X pts: Low PRAN product variety`.

2. **Pillar 2: Visibility / Share of Shelf (Max 25.0 pts)**
   - Measures PRAN linear width percentage (`shelf_metrics.pran_shelf_share_pct`) against the category benchmark (`settings.sos_benchmark_pct`, default: `30.0%`).
   - Formula: $\text{score} = \min\left(25.0, \frac{\text{pran\_sos}}{30.0} \times 25.0\right)$. Reaching 30.0% SOS earns full 25.0 points.
   - If actual SOS falls below 15.0%, logs deduction: `[-] -X pts: Low PRAN shelf share` and generates a LOW severity issue.

3. **Pillar 3: Golden Zone Placement (Max 20.0 pts)**
   - Evaluates the proportion of PRAN facings positioned at eye level (`eye_level` shelf tier) vs. bottom or top tiers:
     $$\text{eye\_ratio} = \frac{\text{pran\_eye\_facings}}{\text{total\_pran\_facings}}$$
     $$\text{score} = \text{round}(\text{eye\_ratio} \times 20.0, 1)$$
   - If eye-level ratio is below 35%, logs deduction: `[-] -X pts: Weak Golden Zone placement` and flags a MEDIUM severity recommendation to relocate SKUs to eye level.

4. **Pillar 4: Shelf Arrangement (Max 15.0 pts)**
   - The Vision AI evaluates front-facing labels, packaging orientation, and flavor variant clustering on a 0–100 scale (`arrangement_score`).
   - Formula: $\text{score} = \text{round}\left(\frac{\text{arrangement\_score}}{100.0} \times 15.0, 1\right)$.
   - If arrangement is below 70/100, logs deduction: `[-] -X pts: Product facings misaligned or irregular grouping`.

5. **Pillar 5: Stock Health (Max 10.0 pts)**
   - Starts at **10.0 points**. Deducts points for visible voids and empty linear shelf space:
     $$\text{deduction} = \min(\text{empty\_shelf\_pct} \times 0.2, 5.0) + \min(\text{visible\_gaps\_count} \times 2.0, 5.0)$$
     $$\text{score} = \max(0.0, 10.0 - \text{deduction})$$
   - If gaps > 0 or empty space > 5.0%, logs deduction: `[-] -X pts: Stock health penalty`.

6. **Pillar 6: Price & Promo Visibility (Max 10.0 pts)**
   - Up to **7.0 points** for shelf price tag coverage near PRAN products ($\text{price\_tag\_ratio} \times 7.0$).
   - **+3.0 points** for active promotional talkers, wobblers, shelf strips, or contest tags (e.g. `'BELI & MENANG'`).
   - If no price tags are detected, deducts 7.0 pts. If no promotional wobblers are present, deducts 3.0 pts.

7. **Composite Score & Normalization**:
   $$\text{Earned Points} = \sum_{p \in \text{evaluated}} p.\text{score}$$
   $$\text{Possible Points} = \sum_{p \in \text{evaluated}} p.\text{max\_pts}$$
   $$\text{Retail Execution Score} = \text{round}\left(\frac{\text{Earned Points}}{\text{Possible Points}} \times 100.0, 1\right)$$

---

#### Case B: Special Case — Zero PRAN Products Detected (`pran_facings == 0`)

When a captured photo contains only competitor products, empty shelves, or non-PRAN grocery sections (such as in shopkeeper scenes or competitor-only fixtures):

1. **Why Retail Execution Score is `null` (Displayed as `N/A`)**:
   - Awarding a numerical score (e.g. 35/100) would be misleading because it implies PRAN execution was partially performed.
   - PRAN cannot score points for competitor price tags, competitor stock health, or competitor arrangement.
   - Therefore, `retail_execution_score.score = null` (rendered as **`N/A`** in the UI) and `grade = "No PRAN Detected"`.

2. **Why All 6 Pillars are `0.0`**:
   - `presence = 0.0 / 20.0` (zero PRAN SKUs)
   - `visibility_sos = 0.0 / 25.0` (0% PRAN linear share)
   - `placement = 0.0 / 20.0` (zero PRAN facings at eye level)
   - `arrangement = 0.0 / 15.0` (zero PRAN facings to arrange)
   - `stock_health = 0.0 / 10.0` (no PRAN inventory present to evaluate)
   - `price_promo = 0.0 / 10.0` (no PRAN price tags or PRAN promotional wobblers)

3. **Why `Shelf Arrangement Index` (e.g. 40/100) is Still Displayed**:
   - The arrangement index (`shelf_metrics.arrangement_score`) measures the general physical neatness and alignment of the entire visible shelf structure (even if it contains competitor products).
   - This provides perceptual context to supervisors without granting any unearned PRAN execution score points.

4. **Deductions & Audit Summary**:
   - `deductions`: Explicitly lists `["No PRAN products visible on shelf fixture"]`.
   - `audit_summary.status`: Set to `"no_pran_detected"`.
   - `pran_summary.pran_detected`: Set to `false`.
   - `shelf_metrics.pran_shelf_share_pct`: Locked to `0.0%`.
   - `recommended_actions`: Generates directive: `Verify shelf section and capture a photo containing PRAN inventory.`

---

#### Grade Thresholds:
$$\text{Grade} = \begin{cases}
\text{Excellent} & \ge 85.0 \\
\text{Good} & \ge 70.0 \\
\text{Needs Attention} & \ge 55.0 \\
\text{Critical Action Required} & < 55.0 \\
\text{No PRAN Detected} & \text{when } \text{pran\_facings} == 0 \quad (\text{score} = \text{null})
\end{cases}$$

### 3. SOS vs. Facing Ratio Consistency Check

To prevent discrepancies between monocular linear width estimation and package facing counts, the system runs an automatic consistency check:

$$\text{facing\_ratio} = \left(\frac{\text{pran\_facings}}{\max(\text{total\_facings}, 1)}\right) \times 100.0$$
$$\text{if } |\text{pran\_shelf\_share\_pct} - \text{facing\_ratio}| > 12.0\% \implies \text{flag "sos\_facing\_mismatch"}, \text{human\_verification\_required} = \text{true}$$

### 4. Catalogue Coverage Reference

The `catalogue_coverage` summary reports how well the shelf inventory is mapped to the canonical catalogue:
- `pran_facings_total`: Total front facings of PRAN items.
- `matched`: Facings matching canonical SKUs with high confidence (>= 0.78).
- `candidate`: Facings matching plausible candidates (>= 0.55).
- `unknown_pran`: Facings of verified PRAN items missing from `itemsdb.csv`. Valuable for expanding catalogue coverage.

---

## Model Reference

### 1. `ShelfAudit` (Table: `shelf_audits`)

Stores every individual shelf photograph audit session.

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | string (UUID) | System | Primary key. |
| `visit_id` | string (UUID) | Yes | Optional foreign key to `merchandiser_visits`. |
| `outlet_id` | string | Yes | Optional foreign key to `outlets`. |
| `audit_stage` | string | Yes | Audit phase: `STANDALONE`, `BEFORE`, or `AFTER`. |
| `image_url` | string | Yes | Image URL or storage reference. |
| `status` | string | System | Audit summary status (`acceptable`, `needs_attention`, `no_pran_detected`). |
| `res_score` | float | System | Retail Execution Score (nullable when no PRAN detected). |
| `pran_shelf_share_pct` | float | System | PRAN linear shelf share percentage. |
| `competitor_shelf_share_pct` | float | System | Competitor linear shelf share percentage. |
| `empty_shelf_share_pct` | float | System | Empty shelf percentage. |
| `pran_facing_count` | integer | System | Number of PRAN front facings. |
| `competitor_facing_count` | integer | System | Number of competitor front facings. |
| `arrangement_score` | float | System | Cleanliness / arrangement score (0-100). |
| `full_analysis` | JSON | System | Complete serialized `ShelfAuditReport` payload. |
| `input_tokens` | integer | System | Prompt tokens consumed by vision model. |
| `output_tokens` | integer | System | Completion tokens consumed by vision model. |
| `total_tokens` | integer | System | Total tokens consumed. |
| `estimated_cost_usd` | float | System | USD cost of vision API call. |
| `created_at` | datetime | System | UTC creation timestamp. |
| `updated_at` | datetime | System | UTC last update timestamp. |

---

## URL Configuration

| HTTP Method | URL Path | Handler / Name | Description |
|---|---|---|---|
| `GET` | `/health` | `health_check` | Server liveness probe. |
| `POST` | `/api/v2/audit/analyze` | `analyze_shelf_audit_v2` | Primary image-only shelf analysis from URL or Base64. |
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
| `422 Unprocessable Entity` | `IMAGE_DECODE_ERROR` | Image payload is corrupt or unreadable by PIL. | Ensure Base64 string is correctly padded and uncorrupted. |
| `500 Internal Server Error` | `RULES_ENGINE_FAILURE` | Error during deterministic scoring or catalogue linking. | Inspect server logs for details. |
| `502 Bad Gateway` | `UPSTREAM_VISION_ERROR` | Remote vision LLM API call timed out or failed. | Verify OpenRouter API key and upstream provider status. |

---

## Dependencies & Core Technologies

- **Web Framework**: FastAPI (Asynchronous Python 3.11+)
- **ORM & Database**: SQLAlchemy 2.0 with PostgreSQL 16
- **Vision AI Perception**: Google Gemini 3.7 Flash via OpenRouter API
- **In-Memory Image Processing**: Pillow 10.x (Lanczos Antialiasing, RAM-only streams)
- **Brand Classification**: Dynamic Token Registry (`app/brands.py`) with automatic expansion from `itemsdb.csv`
- **Local Catalogue Matching**: `sentence-transformers` (`all-MiniLM-L6-v2`) + `rank_bm25` (`BM25Okapi`) over 1,954 SKUs in `itemsdb.csv`
- **Data Validation**: Pydantic v2
