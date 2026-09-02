"""
app/main.py — FastAPI application entry point.

Endpoints:
    GET    /                          → interactive web UI dashboard
    GET    /ui                        → interactive web UI dashboard
    GET    /health                    → liveness check
    POST   /analyze                   → SINGLE-CALL direct analysis (file upload) with token cost
    POST   /analyze/url               → SINGLE-CALL direct analysis (image URL) with token cost
    POST   /uploads                   → async upload rack photo, start AI analysis
    POST   /uploads/url               → async upload rack photo URL, start AI analysis
    GET    /uploads                   → list all analysis results with filtering & pagination
    GET    /uploads/summary           → aggregate analysis metrics, token stats & top products
    GET    /uploads/{upload_id}       → get analysis result by ID
    GET    /uploads/{upload_id}/result→ poll for analysis result
    DELETE /uploads/{upload_id}       → delete upload record & file

Images are served as static files at /media/...
"""
from __future__ import annotations

from collections import defaultdict
import logging
import os
import uuid
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, engine, get_db, init_db
from app.models import ProcessingStatus, RackUpload
from app.schemas import (
    AnalysisSummaryResponse,
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
from app.services.storage_service import (
    InvalidImageURLError,
    StorageError,
    download_and_save_image,
    save_image,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

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
        "AI-powered retail merchandising backend with token usage and cost analysis. "
        "Upload a rack photo to get structured PRAN-RFL product detections, token metrics, and cost estimation."
    ),
    version="1.1.0",
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
        shop_id=row.shop_id,
        merchandiser_id=row.merchandiser_id,
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


def process_upload(upload_id: str, image_url: str) -> None:
    """
    Background task: call Gemini via OpenRouter and write results & token usage to the DB.

    This runs AFTER the HTTP 202 response has already been sent to the client.
    Uses its own DB session (not the request-scoped one, which is closed).
    """
    from app.database import SessionLocal  # local import to avoid circular refs at module level

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

        # ── Call Gemini ───────────────────────────────────────────────────────
        try:
            ai_result = analyze_rack_image(image_url)
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
        # Catch-all so an unexpected crash doesn't silently leave PROCESSING forever
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


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get(
    "/",
    response_class=HTMLResponse,
    summary="Web UI tester & dashboard",
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
def health_check() -> HealthResponse:
    """Returns `{"status": "ok"}` when the server is running."""
    return HealthResponse()


# ── Direct Single-API Analysis (Synchronous 1-Call) ───────────────────────────


@app.post(
    "/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyze rack photo directly in one single API call (returns detections + token costs)",
    tags=["Single API Analysis"],
)
@app.post(
    "/uploads/analyze",
    response_model=DirectAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def analyze_image_direct(
    file: UploadFile = File(..., description="Rack photo (JPEG, PNG, or WebP)"),
    shop_id: str | None = Form(None, description="Shop identifier"),
    merchandiser_id: str | None = Form(None, description="Merchandiser identifier"),
    db: Session = Depends(get_db),
) -> DirectAnalyzeResponse:
    """
    **Single API Call Endpoint**:
    Accepts an uploaded image file, saves it, performs AI vision recognition immediately,
    and returns detected products, input tokens, output tokens, total tokens, and estimated cost
    in a single synchronous HTTP response without needing polling.
    """
    # ── Validate content type ─────────────────────────────────────────────────
    content_type = (file.content_type or "").lower()
    if content_type not in settings.allowed_image_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type: '{content_type}'. "
                f"Allowed types: {', '.join(settings.allowed_image_types)}"
            ),
        )

    # ── Read & validate file size ─────────────────────────────────────────────
    image_bytes = await file.read()
    if len(image_bytes) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File too large ({len(image_bytes) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {settings.max_upload_size_mb} MB."
            ),
        )

    # ── Save to local storage ─────────────────────────────────────────────────
    try:
        image_url, image_key = save_image(
            image_bytes=image_bytes,
            content_type=content_type,
            shop_id=shop_id,
        )
    except StorageError as exc:
        logger.error("Storage error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the uploaded image. Please try again.",
        )

    # ── Create DB row ─────────────────────────────────────────────────────────
    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        shop_id=shop_id,
        merchandiser_id=merchandiser_id,
        image_url=image_url,
        image_key=image_key,
        status=ProcessingStatus.PROCESSING,
    )
    db.add(db_row)
    db.commit()

    # ── Run AI detection synchronously ────────────────────────────────────────
    try:
        ai_res = analyze_rack_image(image_url)
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
            shop_id=db_row.shop_id,
            merchandiser_id=db_row.merchandiser_id,
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
    summary="Analyze rack image URL directly in one single API call (returns detections + token costs)",
    tags=["Single API Analysis"],
)
async def analyze_image_url_direct(
    payload: UploadUrlRequest,
    db: Session = Depends(get_db),
) -> DirectAnalyzeResponse:
    """
    **Single API Call Endpoint (URL)**:
    Accepts an image URL, downloads and validates it, performs AI vision recognition immediately,
    and returns detected products, input tokens, output tokens, total tokens, and estimated cost
    in a single synchronous HTTP response.
    """
    try:
        image_url, image_key = download_and_save_image(
            url=payload.image_url,
            shop_id=payload.shop_id,
        )
    except InvalidImageURLError as exc:
        logger.warning("Invalid image URL '%s': %s", payload.image_url, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except StorageError as exc:
        logger.error("Storage error processing URL '%s': %s", payload.image_url, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the image from the provided URL.",
        )

    # ── Create DB row ─────────────────────────────────────────────────────────
    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        shop_id=payload.shop_id,
        merchandiser_id=payload.merchandiser_id,
        image_url=image_url,
        image_key=image_key,
        status=ProcessingStatus.PROCESSING,
    )
    db.add(db_row)
    db.commit()

    # ── Run AI detection synchronously ────────────────────────────────────────
    try:
        ai_res = analyze_rack_image(image_url)
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
            shop_id=db_row.shop_id,
            merchandiser_id=db_row.merchandiser_id,
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


# ── Asynchronous Endpoints ───────────────────────────────────────────────────


@app.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a rack photo and start async AI analysis",
    tags=["Async Uploads"],
)
async def create_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Rack photo (JPEG, PNG, or WebP)"),
    shop_id: str | None = Form(None, description="Shop identifier"),
    merchandiser_id: str | None = Form(None, description="Merchandiser identifier"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """
    Accept a rack photo, save it locally, create a DB row with status=PENDING,
    return 202 immediately, then run AI analysis in the background.
    """
    # ── Validate content type ─────────────────────────────────────────────────
    content_type = (file.content_type or "").lower()
    if content_type not in settings.allowed_image_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type: '{content_type}'. "
                f"Allowed types: {', '.join(settings.allowed_image_types)}"
            ),
        )

    # ── Read & validate file size ─────────────────────────────────────────────
    image_bytes = await file.read()
    if len(image_bytes) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File too large ({len(image_bytes) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {settings.max_upload_size_mb} MB."
            ),
        )

    # ── Save to local storage ─────────────────────────────────────────────────
    try:
        image_url, image_key = save_image(
            image_bytes=image_bytes,
            content_type=content_type,
            shop_id=shop_id,
        )
    except StorageError as exc:
        logger.error("Storage error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the uploaded image. Please try again.",
        )

    # ── Create DB row ─────────────────────────────────────────────────────────
    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        shop_id=shop_id,
        merchandiser_id=merchandiser_id,
        image_url=image_url,
        image_key=image_key,
        status=ProcessingStatus.PENDING,
    )
    db.add(db_row)
    db.commit()
    logger.info(
        "Upload %s created (shop=%s, merchandiser=%s)", upload_id, shop_id, merchandiser_id
    )

    # ── Schedule background analysis ──────────────────────────────────────────
    background_tasks.add_task(process_upload, upload_id, image_url)

    return UploadResponse(
        upload_id=upload_id,
        status=ProcessingStatus.PENDING,
        message="Image received. Processing started.",
    )


@app.post(
    "/uploads/url",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a rack photo URL and start async AI analysis",
    tags=["Async Uploads"],
)
async def create_upload_from_url(
    payload: UploadUrlRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> UploadResponse:
    """
    Accept a publicly accessible rack photo image URL, download and validate the image,
    save it locally, create a DB record with status=PENDING, return 202 immediately,
    and trigger AI analysis in the background.
    """
    # ── Download and save image from URL ──────────────────────────────────────
    try:
        image_url, image_key = download_and_save_image(
            url=payload.image_url,
            shop_id=payload.shop_id,
        )
    except InvalidImageURLError as exc:
        logger.warning("Invalid image URL '%s': %s", payload.image_url, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except StorageError as exc:
        logger.error("Storage error processing URL '%s': %s", payload.image_url, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the image from the provided URL.",
        )

    # ── Create DB row ─────────────────────────────────────────────────────────
    upload_id = str(uuid.uuid4())
    db_row = RackUpload(
        id=upload_id,
        shop_id=payload.shop_id,
        merchandiser_id=payload.merchandiser_id,
        image_url=image_url,
        image_key=image_key,
        status=ProcessingStatus.PENDING,
    )
    db.add(db_row)
    db.commit()
    logger.info(
        "Upload %s created from URL (shop=%s, merchandiser=%s)",
        upload_id,
        payload.shop_id,
        payload.merchandiser_id,
    )

    # ── Schedule background analysis ──────────────────────────────────────────
    background_tasks.add_task(process_upload, upload_id, image_url)

    return UploadResponse(
        upload_id=upload_id,
        status=ProcessingStatus.PENDING,
        message="Image URL received. Processing started.",
    )


@app.get(
    "/uploads",
    response_model=UploadListResponse,
    summary="List all uploads and analysis results",
    tags=["Uploads & History"],
)
@app.get(
    "/analysis",
    response_model=UploadListResponse,
    include_in_schema=False,
)
@app.get(
    "/results",
    response_model=UploadListResponse,
    include_in_schema=False,
)
def list_uploads(
    status: ProcessingStatus | None = Query(None, description="Filter by processing status"),
    shop_id: str | None = Query(None, description="Filter by shop ID (case-insensitive substring)"),
    merchandiser_id: str | None = Query(None, description="Filter by merchandiser ID (case-insensitive substring)"),
    search: str | None = Query(None, description="Search across shop ID, merchandiser ID, or error message"),
    limit: int = Query(50, ge=1, le=100, description="Max number of items to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
) -> UploadListResponse:
    """
    Fetch paginated list of all rack photo uploads, detected products, and token usage metrics.
    Supports filtering by status, shop ID, merchandiser ID, and generic search.
    """
    query = db.query(RackUpload)

    if status:
        query = query.filter(RackUpload.status == status)

    if shop_id and shop_id.strip():
        query = query.filter(RackUpload.shop_id.ilike(f"%{shop_id.strip()}%"))

    if merchandiser_id and merchandiser_id.strip():
        query = query.filter(RackUpload.merchandiser_id.ilike(f"%{merchandiser_id.strip()}%"))

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                RackUpload.shop_id.ilike(term),
                RackUpload.merchandiser_id.ilike(term),
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
    "/analysis/summary",
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
    "/uploads/{upload_id}/result",
    response_model=UploadResultResponse,
    summary="Get analysis result for an upload (polling endpoint)",
    tags=["Uploads & History"],
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
