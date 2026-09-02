# MerchVision-PRAN-RFL

> AI-powered retail merchandising backend — field merchandisers upload a photo of a product rack, Gemini 3.7 Flash identifies every PRAN-RFL product visible, and returns a structured list with quantities.
>
> 📖 **Full API Reference**: See [API_DOCUMENTATION.md](API_DOCUMENTATION.md) for detailed payload schemas, sequencing, and code examples.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Quick Start — Local (No Docker)](#quick-start--local-no-docker)
- [Quick Start — Docker](#quick-start--docker)
- [Configuration Reference](#configuration-reference)
- [API Reference](#api-reference)
- [Dedicated API Docs (API_DOCUMENTATION.md)](API_DOCUMENTATION.md)
- [Project Structure](#project-structure)
- [Switching Between SQLite and PostgreSQL](#switching-between-sqlite-and-postgresql)

---

## How It Works

```
Client → POST /uploads (image + shop_id + merchandiser_id)
              │
              ├─ Saves image to ./media/uploads/{shop_id}/
              ├─ Creates DB row  (status = PENDING)
              └─ Returns 202 immediately  ← upload_id

Background Task (runs after response)
              │
              ├─ Sets status = PROCESSING
              ├─ Calls Gemini 3.7 Flash via OpenRouter
              └─ Sets status = COMPLETED (products list)
                          or FAILED (error_message)

Client → GET /uploads/{upload_id}/result   (poll until COMPLETED/FAILED)
```

---

## Prerequisites

### For Local (No Docker)

| Requirement | Version | Check |
|---|---|---|
| Python | 3.13+ | `python --version` |
| Pipenv | any | `pipenv --version` |
| OpenRouter API Key | — | [openrouter.ai/keys](https://openrouter.ai/keys) |

### For Docker

| Requirement | Version | Check |
|---|---|---|
| Docker Desktop | any | `docker --version` |
| Docker Compose | v2+ | `docker compose version` |
| OpenRouter API Key | — | [openrouter.ai/keys](https://openrouter.ai/keys) |

---

## Quick Start — Local (No Docker)

Uses **SQLite** (zero setup) + local file storage. Perfect for development.

### Step 1 — Clone and enter the project

```bash
cd Prism
```

### Step 2 — Install dependencies

```bash
pipenv install
```

### Step 3 — Configure environment

```bash
# Copy the template
copy .env.example .env        # Windows
cp .env.example .env          # Mac/Linux
```

Open `.env` and **set your OpenRouter API key** (the only required change):

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx
```

Everything else works out of the box with defaults.

### Step 4 — Start the server

```bash
pipenv run uvicorn app.main:app --reload
```

You should see:

```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

### Step 5 — Verify it's running

```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

Open **http://localhost:8000/docs** in your browser for the interactive Swagger UI — you can upload images directly from there.

> **What gets created automatically on first run:**
> - `pran_rfl.db` — SQLite database file (all uploads and results)
> - `media/` — directory where uploaded images are stored

---

## Quick Start — Docker

Uses **PostgreSQL 16** + local file storage mounted as a volume.

### Step 1 — Configure environment

```bash
copy .env.example .env        # Windows
cp .env.example .env          # Mac/Linux
```

Open `.env` and **set your OpenRouter API key**:

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx
```

### Step 2 — Build and start

```bash
docker compose up --build
```

First run takes ~60 seconds to pull the Postgres image and build the Python image. Subsequent starts are instant.

You should see both services start:

```
pran_rfl_db       | database system is ready to accept connections
pran_rfl_backend  | INFO:     Application startup complete.
```

### Step 3 — Verify it's running

```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

Open **http://localhost:8000/docs** for the Swagger UI.

### Useful Docker commands

```bash
# Start in background (detached)
docker compose up -d --build

# View logs
docker compose logs -f backend
docker compose logs -f db

# Stop everything
docker compose down

# Stop and wipe the database volume (full reset)
docker compose down -v

# Rebuild after code changes
docker compose up --build
```

> **Uploaded images** are persisted in `./media/` on your host machine (mounted as a volume), so they survive container restarts.

---

## Configuration Reference

All settings live in `.env`. The file is read automatically on startup.

| Variable | Default | Required | Description |
|---|---|---|---|
| `DATABASE_URL` | `sqlite:///./pran_rfl.db` | No | Postgres: `postgresql://user:pass@host:5432/db` |
| `OPENROUTER_API_KEY` | — | **Yes** | Your OpenRouter key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | No | OpenRouter endpoint |
| `OPENROUTER_MODEL` | `google/gemini-3.7-flash` | No | Any vision model on OpenRouter |
| `OPENROUTER_TIMEOUT_SECONDS` | `30` | No | Seconds before AI call times out |
| `STORAGE_BASE_URL` | `http://localhost:8000` | No | Public URL prefix for image links |
| `MAX_UPLOAD_SIZE_MB` | `10` | No | Max accepted image size |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000,...` | No | Comma-separated allowed origins |

---

## API Reference

### `GET /health`

Liveness check.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok"}
```

---

### `POST /uploads`

Upload a rack photo and start AI analysis. Returns **202** immediately.

**Request** — `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | image file | ✅ | JPEG, PNG, or WebP — max 10 MB |
| `shop_id` | string | No | Shop identifier |
| `merchandiser_id` | string | No | Merchandiser identifier |

```bash
curl -X POST http://localhost:8000/uploads \
  -F "file=@rack_photo.jpg" \
  -F "shop_id=SHOP-102" \
  -F "merchandiser_id=MER-45"
```

**Response 202**

```json
{
  "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
  "status": "PENDING",
  "message": "Image received. Processing started."
}
```

**Errors**

| Status | Cause |
|---|---|
| `400` | Wrong file type or file exceeds size limit |
| `500` | Could not save the file to disk |

---

### `POST /uploads/url`

Provide a publicly accessible rack photo URL to start AI analysis. Returns **202** immediately.

**Request** — `application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `image_url` | string | ✅ | HTTP or HTTPS URL to JPEG, PNG, or WebP image |
| `shop_id` | string | No | Shop identifier |
| `merchandiser_id` | string | No | Merchandiser identifier |

```bash
curl -X POST http://localhost:8000/uploads/url \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://example.com/rack_photo.jpg",
    "shop_id": "SHOP-102",
    "merchandiser_id": "MER-45"
  }'
```

**Response 202**

```json
{
  "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
  "status": "PENDING",
  "message": "Image URL received. Processing started."
}
```

**Errors**

| Status | Cause |
|---|---|
| `400` | Invalid URL, unreachable host, wrong file type, or file exceeds size limit |
| `500` | Could not save downloaded image to disk |

---

### `GET /uploads`

List all rack upload analysis results with filtering and pagination.

**Query Parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | string | `None` | Filter by `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED` |
| `shop_id` | string | `None` | Filter by shop identifier (substring match) |
| `merchandiser_id` | string | `None` | Filter by merchandiser identifier (substring match) |
| `search` | string | `None` | Search query across shop ID, merchandiser, ID, errors |
| `limit` | integer | `50` | Max number of items (1 to 100) |
| `offset` | integer | `0` | Pagination offset |

```bash
curl "http://localhost:8000/uploads?status=COMPLETED&limit=10"
```

**Response 200**

```json
{
  "total": 42,
  "limit": 10,
  "offset": 0,
  "items": [
    {
      "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
      "status": "COMPLETED",
      "shop_id": "SHOP-102",
      "merchandiser_id": "MER-45",
      "image_url": "http://localhost:8000/media/uploads/SHOP-102/abc123.jpg",
      "detected_products": [
        {"product_name": "PRAN Mango Juice 250ml", "quantity_visible": 6}
      ],
      "error_message": null,
      "created_at": "2026-08-30T10:00:00Z",
      "updated_at": "2026-08-30T10:00:07Z"
    }
  ]
}
```

---

### `GET /uploads/summary`

Aggregated analytics across all rack scans (total scans, success count, products detected count, and top detected items).

```bash
curl http://localhost:8000/uploads/summary
```

**Response 200**

```json
{
  "total_scans": 25,
  "completed_scans": 22,
  "processing_scans": 1,
  "pending_scans": 0,
  "failed_scans": 2,
  "total_products_detected": 142,
  "unique_products_count": 18,
  "top_products": [
    {
      "product_name": "PRAN Mango Juice 250ml",
      "total_quantity": 48,
      "scan_appearances": 14
    }
  ],
  "recent_uploads": [...]
}
```

---

### `GET /uploads/{upload_id}` or `GET /uploads/{upload_id}/result`

Fetch the current status and AI result for a specific upload. **Poll this until status is `COMPLETED` or `FAILED`.**

```bash
curl http://localhost:8000/uploads/b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d
```

**Response 200 — completed**

```json
{
  "upload_id": "b3f1c2a4-...",
  "status": "COMPLETED",
  "shop_id": "SHOP-102",
  "merchandiser_id": "MER-45",
  "image_url": "http://localhost:8000/media/uploads/SHOP-102/abc123.jpg",
  "detected_products": [
    {"product_name": "PRAN Mango Juice 250ml", "quantity_visible": 6},
    {"product_name": "RFL Water Bottle 1L",    "quantity_visible": 3}
  ],
  "error_message": null,
  "created_at": "2026-08-30T10:00:00Z",
  "updated_at": "2026-08-30T10:00:07Z"
}
```

---

### `DELETE /uploads/{upload_id}`

Delete an upload record from the database and remove its stored image from disk.

```bash
curl -X DELETE http://localhost:8000/uploads/b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d
```

**Response 200**

```json
{
  "upload_id": "b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d",
  "message": "Upload 'b3f1c2a4-1234-4a5b-8c9d-0e1f2a3b4c5d' and associated media deleted successfully."
}
```

---

### `GET /media/{path}`

Serves uploaded images as static files.

The `image_url` field in every result response already contains the full URL — just open it in the browser or `<img src={image_url} />` in your frontend.

---

### Interactive Docs

FastAPI auto-generates Swagger UI and ReDoc:

| UI | URL |
|---|---|
| Swagger (interactive, try it) | http://localhost:8000/docs |
| ReDoc (readable reference) | http://localhost:8000/redoc |

---

## Project Structure

```
Prism/
├── .env                        ← your secrets (never commit this)
├── .env.example                ← safe template to commit
├── Pipfile                     ← pipenv dependency spec
├── requirements.txt            ← pip deps for Docker
├── Dockerfile                  ← Python 3.13-slim image
├── docker-compose.yml          ← backend + Postgres 16
└── app/
    ├── config.py               ← all settings (pydantic-settings)
    ├── database.py             ← SQLAlchemy engine + session factory
    ├── models.py               ← RackUpload ORM model + ProcessingStatus enum
    ├── schemas.py              ← Pydantic API request/response models
    ├── main.py                 ← FastAPI app: 3 endpoints + background task
    └── services/
        ├── storage_service.py  ← writes images to ./media/, returns URL
        └── ai_service.py       ← calls Gemini via OpenRouter, parses JSON
```

---

## Switching Between SQLite and PostgreSQL

### SQLite (local, default)

```env
DATABASE_URL=sqlite:///./pran_rfl.db
```

No extra setup. DB file is created automatically.

### PostgreSQL (Docker or external)

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pran_rfl
```

When using `docker compose up`, this is set automatically — you don't need to change `.env` at all.

Tables are created automatically on startup via `Base.metadata.create_all()`.

---

