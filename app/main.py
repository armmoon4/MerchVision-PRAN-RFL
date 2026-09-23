"""
app/main.py — FastAPI application entry point.

Endpoints:
    GET    /                        → interactive web UI dashboard
    GET    /ui                      → interactive web UI dashboard
    GET    /health                  → liveness check
    POST   /analyze                 → SINGLE-CALL: send image_url (S3 URL or Base64), get instant result
    POST   /analyze/url             → alias for /analyze
    POST   /analyze/file            → SINGLE-CALL: upload image file, get instant result
    POST   /uploads                 → async upload rack photo file, start AI analysis
    POST   /uploads/url             → async submit S3 URL/Base64, start AI analysis
    GET    /uploads                 → list all analysis results with filtering & pagination
    GET    /uploads/summary         → aggregate analysis metrics, token stats & top products
    GET    /uploads/{upload_id}     → get analysis result by ID
    GET    /uploads/{upload_id}/result→ poll for analysis result
    DELETE /uploads/{upload_id}     → delete upload record

Image processing uses the download-then-delete pattern:
  image is written to a secure temp file, analyzed by Gemini, then IMMEDIATELY deleted.
Images are served as static files at /media/...
"""
from __future__ import annotations

from collections import defaultdict
import logging
import os
import uuid
from pathlib import Path

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_db, init_db
from app.models import ProcessingStatus, RackUpload
from app.schemas import (
    AnalysisSummaryResponse,
    AnalyzeImageRequest,
    AnalyzeRequest,
    DeleteResponse,
    DirectAnalyzeResponse,
    HealthResponse,
    TokenUsage,
    TopProductItem,
    UploadListResponse,
    UploadResponse,
    UploadResultResponse,
    UploadUrlRequest,
)
from app.services.ai_service import AIServiceError, analyze_rack_image
# storage_service retained for legacy compatibility (unused in analyze flow)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

from app.routers.token_router import router as token_router

# ── App bootstrap ─────────────────────────────────────────────────────────────

settings = get_settings()

# Create all DB tables & migrate missing columns on startup (idempotent)
init_db()

# Ensure media directory exists
_MEDIA_ROOT = Path("media")
_MEDIA_ROOT.mkdir(exist_ok=True)
Path(settings.storage_media_dir).mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="PRAN-RFL Rack Recognition System",
    description=(
        "AI-powered retail merchandising backend with Gemini 3.7 Flash and Token Router API. "
        "Upload a rack photo to get structured PRAN-RFL product detections, token metrics, and cost estimation."
    ),
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Token Router API ──────────────────────────────────────────────────────────

app.include_router(token_router)
app.include_router(token_router, prefix="/api", include_in_schema=False)
app.include_router(token_router, prefix="/api/v1", include_in_schema=False)

# ── Static files (served uploaded images) ────────────────────────────────────

app.mount("/media", StaticFiles(directory="media"), name="media")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_upload_result_response(row: RackUpload) -> UploadResultResponse:
    """Helper to convert a DB RackUpload row into a typed UploadResultResponse."""
    token_usage = None
    if (
        row.total_tokens is not None
        or row.input_tokens is not None
        or row.output_tokens is not None
    ):
        token_usage = TokenUsage(
            input_tokens=row.input_tokens or 0,
            output_tokens=row.output_tokens or 0,
            total_tokens=row.total_tokens or 0,
            estimated_cost_usd=row.estimated_cost_usd or 0.0,
        )

    return UploadResultResponse(
        upload_id=row.id,
        status=row.status,
        image_url=row.image_url,
        detected_products=row.detected_products,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        total_tokens=row.total_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        token_usage=token_usage,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── Background task ───────────────────────────────────────────────────────────


def process_upload(upload_id: str, image_input: str | bytes, image_ref: str = "") -> None:
    """
    Background task: download image to a temp file, call Gemini, write results to DB,
    then delete the temp file immediately (download-then-delete pattern).
    """
    db = SessionLocal()
    try:
        upload = db.query(RackUpload).filter(RackUpload.id == upload_id).first()
        if not upload:
            logger.error("process_upload: upload %s not found in DB", upload_id)
            return

        # ── Set PROCESSING ────────────────────────────────────────────────────
        upload.status = ProcessingStatus.PROCESSING
        db.commit()
        logger.info("Upload %s: status → PROCESSING", upload_id)

        # ── Call Gemini In-Memory ──────────────────────────────────────────────
        try:
            ai_result = analyze_rack_image(image_input)
            products = ai_result.get("products", [])
            usage = ai_result.get("token_usage", {})

            upload.detected_products = products
            upload.input_tokens = usage.get("input_tokens", 0)
            upload.output_tokens = usage.get("output_tokens", 0)
            upload.total_tokens = usage.get("total_tokens", 0)
            upload.estimated_cost_usd = usage.get("estimated_cost_usd", 0.0)
            upload.ai_raw_response = ai_result.get("raw_text")
            upload.status = ProcessingStatus.COMPLETED
            logger.info(
                "Upload %s: status → COMPLETED, %d products found, tokens: in=%d, out=%d, total=%d, cost=$%.6f",
                upload_id,
                len(products),
                upload.input_tokens,
                upload.output_tokens,
                upload.total_tokens,
                upload.estimated_cost_usd or 0.0,
            )
        except AIServiceError as exc:
            upload.status = ProcessingStatus.FAILED
            upload.error_message = str(exc)
            logger.error("Upload %s: status → FAILED — %s", upload_id, exc)

        db.commit()

    except Exception as exc:
        logger.exception("process_upload: unexpected error for upload %s: %s", upload_id, exc)
        try:
            upload = db.query(RackUpload).filter(RackUpload.id == upload_id).first()
            if upload:
                upload.status = ProcessingStatus.FAILED
                upload.error_message = f"Internal processing error: {exc}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ── System & UI Endpoints ─────────────────────────────────────────────────────


@app.get(
    "/",
    response_class=HTMLResponse,
    summary="Root redirect to Web UI",
    include_in_schema=False,
)
@app.get(
    "/ui",
    response_class=HTMLResponse,
    summary="Web UI tester & dashboard",
    include_in_schema=False,
)
def get_ui():
    """Serves the interactive web UI if testui.html is present."""
    ui_path = Path("testui.html")
    if ui_path.exists():
        return FileResponse(ui_path)
    return HTMLResponse("<h1>PRAN-RFL Rack Recognition System</h1><p><a href='/docs'>Swagger API Docs</a></p>")


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    tags=["System"],
)
@app.get(
    "/api/health",
    response_model=HealthResponse,
    include_in_schema=False,
)
def health_check() -> HealthResponse:
    """Returns `{"status": "ok"}` when the server is running."""
    return HealthResponse()


# ── Direct Single-API Analysis (Synchronous 1-Call, Zero Disk Storage) ────────


@app.post(
    "/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyze rack photo — send image_url (S3 URL or Base64), get result immediately",
    tags=["Single API Analysis"],
)
@app.post(
    "/api/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@app.post(
    "/uploads/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@app.post(
    "/api/uploads/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def analyze_image_direct(
    payload: AnalyzeRequest,
    db: Session = Depends(get_db),
) -> DirectAnalyzeResponse:
    """
    **Single-Call Rack Analysis** (download-then-delete pattern):

    - Send a JSON body with `image_url` containing an **S3 presigned URL**, **remote HTTP/HTTPS URL**, or a **Base64 image string**.
    - The server downloads the image to a secure temp file, runs Gemini AI analysis,
      **deletes the temp file immediately** after analysis, and returns the result.
    - No image is ever permanently stored on the server disk.

    **Request body:**
    ```json
    {
      "image_url": "https://s3.amazonaws.com/bucket/image.jpg"
    }
    ```
    Or with Base64:
    ```json
    {
      "image_url": "/9j/4AAQSkZJRgAB..."
    }
    ```

    **Response:** detected products, token usage, and estimated cost.
    """
    image_input = payload.image_url or payload.image
    if not image_input or not image_input.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_url is required. Provide an S3/HTTP URL or Base64 image string in 'image_url'.",
        )

    image_ref = (
        image_input
        if (image_input.startswith(("http://", "https://", "s3://")) and len(image_input) < 2000)
        else "base64_upload"
    )

    # ── Create DB record for analytics / history ───────────────────────────────
    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        image_url=image_ref,
        image_key="",
        status=ProcessingStatus.PROCESSING,
    )
    db.add(db_row)
    db.commit()

    # ── Download → temp file → Gemini → delete → result ───────────────────────
    try:
        ai_res = await run_in_threadpool(analyze_rack_image, image_input)
        products = ai_res.get("products", [])
        usage = ai_res.get("token_usage", {})

        db_row.detected_products = products
        db_row.input_tokens = usage.get("input_tokens", 0)
        db_row.output_tokens = usage.get("output_tokens", 0)
        db_row.total_tokens = usage.get("total_tokens", 0)
        db_row.estimated_cost_usd = usage.get("estimated_cost_usd", 0.0)
        db_row.ai_raw_response = ai_res.get("raw_text")
        db_row.status = ProcessingStatus.COMPLETED
        db.commit()

        token_usage_obj = TokenUsage(
            input_tokens=db_row.input_tokens or 0,
            output_tokens=db_row.output_tokens or 0,
            total_tokens=db_row.total_tokens or 0,
            estimated_cost_usd=db_row.estimated_cost_usd or 0.0,
        )

        return DirectAnalyzeResponse(
            upload_id=db_row.id,
            status=ProcessingStatus.COMPLETED,
            image_url=db_row.image_url,
            detected_products=products,
            token_usage=token_usage_obj,
            input_tokens=token_usage_obj.input_tokens,
            output_tokens=token_usage_obj.output_tokens,
            total_tokens=token_usage_obj.total_tokens,
            estimated_cost_usd=token_usage_obj.estimated_cost_usd,
            error_message=None,
            created_at=db_row.created_at,
        )
    except AIServiceError as exc:
        db_row.status = ProcessingStatus.FAILED
        db_row.error_message = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI Vision Analysis failed: {exc}",
        )


@app.post(
    "/analyze/file",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyze uploaded image file directly in-memory (zero disk storage)",
    tags=["Single API Analysis"],
)
@app.post(
    "/analyze/upload",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def analyze_file_direct(
    file: UploadFile = File(..., description="Rack photo (JPEG, PNG, or WebP)"),
    db: Session = Depends(get_db),
) -> DirectAnalyzeResponse:
    """
    Accepts an uploaded image file, processes it purely in-memory (no saving to disk),
    and returns detected products, token metrics, and estimated USD cost.
    """
    content_type = (file.content_type or "").lower()
    if content_type not in settings.allowed_image_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type: '{content_type}'. "
                f"Allowed types: {', '.join(settings.allowed_image_types)}"
            ),
        )

    image_bytes = await file.read()
    if len(image_bytes) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File too large ({len(image_bytes) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {settings.max_upload_size_mb} MB."
            ),
        )

    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        image_url="direct_file_upload_in_memory",
        image_key="",
        status=ProcessingStatus.PROCESSING,
    )
    db.add(db_row)
    db.commit()

    try:
        ai_res = await run_in_threadpool(analyze_rack_image, image_bytes)
        products = ai_res.get("products", [])
        usage = ai_res.get("token_usage", {})

        db_row.detected_products = products
        db_row.input_tokens = usage.get("input_tokens", 0)
        db_row.output_tokens = usage.get("output_tokens", 0)
        db_row.total_tokens = usage.get("total_tokens", 0)
        db_row.estimated_cost_usd = usage.get("estimated_cost_usd", 0.0)
        db_row.ai_raw_response = ai_res.get("raw_text")
        db_row.status = ProcessingStatus.COMPLETED
        db.commit()

        token_usage_obj = TokenUsage(
            input_tokens=db_row.input_tokens or 0,
            output_tokens=db_row.output_tokens or 0,
            total_tokens=db_row.total_tokens or 0,
            estimated_cost_usd=db_row.estimated_cost_usd or 0.0,
        )

        return DirectAnalyzeResponse(
            upload_id=db_row.id,
            status=ProcessingStatus.COMPLETED,
            image_url=db_row.image_url,
            detected_products=products,
            token_usage=token_usage_obj,
            input_tokens=token_usage_obj.input_tokens,
            output_tokens=token_usage_obj.output_tokens,
            total_tokens=token_usage_obj.total_tokens,
            estimated_cost_usd=token_usage_obj.estimated_cost_usd,
            error_message=None,
            created_at=db_row.created_at,
        )
    except AIServiceError as exc:
        db_row.status = ProcessingStatus.FAILED
        db_row.error_message = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI Vision Analysis failed: {exc}",
        )


@app.post(
    "/analyze/url",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Alias for /analyze — send image_url (S3 URL or Base64), get result immediately",
    tags=["Single API Analysis"],
)
@app.post(
    "/api/analyze/url",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def analyze_image_url_direct(
    payload: AnalyzeRequest,
    db: Session = Depends(get_db),
) -> DirectAnalyzeResponse:
    """
    Alias for POST /analyze.
    Accepts an S3 URL, HTTP/HTTPS URL, or Base64 string.
    Downloads image to temp file, runs Gemini AI analysis, deletes temp file, returns result.
    """
    return await analyze_image_direct(payload=payload, db=db)


# ── Asynchronous Endpoints (Zero Disk Storage) ────────────────────────────────


@app.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a rack photo file and start async AI analysis in-memory",
    tags=["Async Uploads"],
)
@app.post(
    "/api/uploads",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def create_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Rack photo (JPEG, PNG, or WebP)"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """
    Accept a rack photo file, read into memory, create DB row with status=PENDING,
    return 202 immediately, then run AI analysis in the background without writing to disk.
    """
    content_type = (file.content_type or "").lower()
    if content_type not in settings.allowed_image_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type: '{content_type}'. "
                f"Allowed types: {', '.join(settings.allowed_image_types)}"
            ),
        )

    image_bytes = await file.read()
    if len(image_bytes) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File too large ({len(image_bytes) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {settings.max_upload_size_mb} MB."
            ),
        )

    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        image_url="async_upload_in_memory",
        image_key="",
        status=ProcessingStatus.PENDING,
    )
    db.add(db_row)
    db.commit()

    # ── Schedule background analysis with in-memory bytes ──────────────────────
    background_tasks.add_task(process_upload, upload_id, image_bytes)

    return UploadResponse(
        upload_id=upload_id,
        status=ProcessingStatus.PENDING,
        message="Image received in-memory. Processing started.",
    )


@app.post(
    "/uploads/url",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a rack photo S3 URL/Base64 and start async AI analysis in-memory",
    tags=["Async Uploads"],
)
@app.post(
    "/api/uploads/url",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def create_upload_from_url(
    payload: UploadUrlRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> UploadResponse:
    """
    Accept an S3 URL, HTTP image URL, or Base64 string, create DB record with status=PENDING,
    return 202 immediately, and trigger in-memory AI analysis in the background without disk writes.
    """
    image_input = payload.image_url or payload.image
    if not image_input or not image_input.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image input is required. Provide an S3/HTTP image URL or Base64 string in 'image_url'.",
        )

    image_ref = image_input if (image_input.startswith(("http://", "https://", "s3://")) and len(image_input) < 2000) else "base64_in_memory"

    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        image_url=image_ref,
        image_key="",
        status=ProcessingStatus.PENDING,
    )
    db.add(db_row)
    db.commit()

    # ── Schedule background analysis ──────────────────────────────────────────
    background_tasks.add_task(process_upload, upload_id, image_input)

    return UploadResponse(
        upload_id=upload_id,
        status=ProcessingStatus.PENDING,
        message="Image input received. Processing started.",
    )


@app.get(
    "/uploads",
    response_model=UploadListResponse,
    summary="List all uploads and analysis results",
    tags=["Uploads & History"],
)
@app.get(
    "/api/uploads",
    response_model=UploadListResponse,
    include_in_schema=False,
)
@app.get(
    "/analysis",
    response_model=UploadListResponse,
    include_in_schema=False,
)
@app.get(
    "/api/analysis",
    response_model=UploadListResponse,
    include_in_schema=False,
)
@app.get(
    "/results",
    response_model=UploadListResponse,
    include_in_schema=False,
)
@app.get(
    "/api/results",
    response_model=UploadListResponse,
    include_in_schema=False,
)
def list_uploads(
    status: ProcessingStatus | None = Query(None, description="Filter by processing status"),
    search: str | None = Query(None, description="Search across upload ID or error message"),
    limit: int = Query(50, ge=1, le=100, description="Max number of items to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
) -> UploadListResponse:
    """
    Fetch paginated list of all rack photo uploads, detected products, and token usage metrics.
    Supports filtering by status and search.
    """
    query = db.query(RackUpload)

    if status:
        query = query.filter(RackUpload.status == status)

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                RackUpload.id.ilike(term),
                RackUpload.error_message.ilike(term),
            )
        )

    total = query.count()
    rows = query.order_by(RackUpload.created_at.desc()).offset(offset).limit(limit).all()

    items = [_build_upload_result_response(row) for row in rows]

    return UploadListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


@app.get(
    "/uploads/summary",
    response_model=AnalysisSummaryResponse,
    summary="Get aggregated summary metrics, token totals, and top products",
    tags=["Analytics"],
)
@app.get(
    "/api/uploads/summary",
    response_model=AnalysisSummaryResponse,
    include_in_schema=False,
)
@app.get(
    "/analysis/summary",
    response_model=AnalysisSummaryResponse,
    include_in_schema=False,
)
@app.get(
    "/api/analysis/summary",
    response_model=AnalysisSummaryResponse,
    include_in_schema=False,
)
def get_analysis_summary(db: Session = Depends(get_db)) -> AnalysisSummaryResponse:
    """
    Returns high-level statistics across all scanned racks, including total input tokens,
    output tokens, total tokens, total USD cost, average tokens per image, and top detected items.
    """
    total_scans = db.query(RackUpload).count()
    completed_scans = db.query(RackUpload).filter(RackUpload.status == ProcessingStatus.COMPLETED).count()
    processing_scans = db.query(RackUpload).filter(RackUpload.status == ProcessingStatus.PROCESSING).count()
    pending_scans = db.query(RackUpload).filter(RackUpload.status == ProcessingStatus.PENDING).count()
    failed_scans = db.query(RackUpload).filter(RackUpload.status == ProcessingStatus.FAILED).count()

    # Aggregate completed rows for detection and token statistics
    completed_rows = (
        db.query(RackUpload)
        .filter(RackUpload.status == ProcessingStatus.COMPLETED)
        .all()
    )

    product_totals: dict[str, int] = defaultdict(int)
    product_appearances: dict[str, int] = defaultdict(int)
    total_products_detected = 0

    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    total_cost_usd = 0.0

    for row in completed_rows:
        total_input_tokens += row.input_tokens or 0
        total_output_tokens += row.output_tokens or 0
        total_tokens += row.total_tokens or 0
        total_cost_usd += row.estimated_cost_usd or 0.0

        products_json = row.detected_products
        if not isinstance(products_json, list):
            continue
        seen_in_scan: set[str] = set()
        for item in products_json:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product_name", "Unknown Product")).strip()
            qty_raw = item.get("quantity_visible")
            qty = int(qty_raw) if isinstance(qty_raw, (int, float)) and qty_raw > 0 else 1

            product_totals[name] += qty
            total_products_detected += qty

            if name not in seen_in_scan:
                product_appearances[name] += 1
                seen_in_scan.add(name)

    top_products = [
        TopProductItem(
            product_name=name,
            total_quantity=qty,
            scan_appearances=product_appearances[name],
        )
        for name, qty in sorted(product_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    avg_tokens_per_scan = (
        round(total_tokens / completed_scans, 2) if completed_scans > 0 else 0.0
    )
    avg_cost_per_scan_usd = (
        round(total_cost_usd / completed_scans, 6) if completed_scans > 0 else 0.0
    )

    recent_rows = db.query(RackUpload).order_by(RackUpload.created_at.desc()).limit(5).all()
    recent_uploads = [_build_upload_result_response(row) for row in recent_rows]

    return AnalysisSummaryResponse(
        total_scans=total_scans,
        completed_scans=completed_scans,
        processing_scans=processing_scans,
        pending_scans=pending_scans,
        failed_scans=failed_scans,
        total_products_detected=total_products_detected,
        unique_products_count=len(product_totals),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_tokens=total_tokens,
        total_estimated_cost_usd=round(total_cost_usd, 6),
        avg_tokens_per_scan=avg_tokens_per_scan,
        avg_cost_per_scan_usd=avg_cost_per_scan_usd,
        top_products=top_products,
        recent_uploads=recent_uploads,
    )


@app.get(
    "/uploads/{upload_id}",
    response_model=UploadResultResponse,
    summary="Get analysis result & token metrics for an upload by ID",
    tags=["Uploads & History"],
)
@app.get(
    "/api/uploads/{upload_id}",
    response_model=UploadResultResponse,
    include_in_schema=False,
)
@app.get(
    "/uploads/{upload_id}/result",
    response_model=UploadResultResponse,
    summary="Get analysis result for an upload (polling endpoint)",
    tags=["Uploads & History"],
)
@app.get(
    "/api/uploads/{upload_id}/result",
    response_model=UploadResultResponse,
    include_in_schema=False,
)
def get_upload_result(
    upload_id: str,
    db: Session = Depends(get_db),
) -> UploadResultResponse:
    """
    Fetch the current status, AI product detections, and token usage metrics for a given upload ID.
    Poll this endpoint until `status` is `COMPLETED` or `FAILED`.
    """
    row = db.query(RackUpload).filter(RackUpload.id == upload_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload '{upload_id}' not found.",
        )

    return _build_upload_result_response(row)


@app.delete(
    "/uploads/{upload_id}",
    response_model=DeleteResponse,
    summary="Delete an upload record and its image",
    tags=["Uploads & History"],
)
@app.delete(
    "/api/uploads/{upload_id}",
    response_model=DeleteResponse,
    include_in_schema=False,
)
def delete_upload(
    upload_id: str,
    db: Session = Depends(get_db),
) -> DeleteResponse:
    """
    Permanently delete a rack upload record and remove the stored image file from disk.
    """
    row = db.query(RackUpload).filter(RackUpload.id == upload_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload '{upload_id}' not found.",
        )

    # Delete local image file if it exists
    if row.image_key:
        try:
            media_root = Path(settings.storage_media_dir).parent
            file_path = media_root / row.image_key
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                logger.info("Deleted image file %s for upload %s", file_path, upload_id)
        except OSError as exc:
            logger.warning("Could not delete file for upload %s: %s", upload_id, exc)

    db.delete(row)
    db.commit()
    logger.info("Upload %s deleted from database", upload_id)

    return DeleteResponse(
        upload_id=upload_id,
        message=f"Upload '{upload_id}' and associated media deleted successfully.",
    )


# ── Frontend Reverse Proxy (Must remain at the very bottom of the file) ───────


@app.get("/{file_path:path}", include_in_schema=False)
async def proxy_frontend_fallback(file_path: str):
    """
    Catch-all route to proxy remaining GET requests to an external service (e.g. Next.js / Vite on port 3000).
    Placed at the bottom so it only handles paths that do not match existing API routes.
    """
    target_url = f"http://localhost:3000/{file_path}"
    async with httpx.AsyncClient() as client:
        try:
            external_res = await client.get(target_url)
            incoming_content_type = external_res.headers.get("content-type", "application/octet-stream")
            return Response(
                content=external_res.content,
                media_type=incoming_content_type,
                status_code=external_res.status_code,
            )
        except httpx.RequestError as exc:
            logger.error("Proxy connection failed to target %s: %s", target_url, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to frontend server at http://localhost:3000. Error: {exc}",
            )