"""
app/routers/audit_router.py — Retail Merchandising Shelf Audit & Management Dashboard Endpoints.

Endpoints:
  POST   /merchandising/analyze       → Single-call retail shelf audit (Before/After/Standalone)
  POST   /merchandising/analyze/file  → File upload shelf audit
  POST   /merchandising/verify        → Before-and-After improvement verification
  GET    /merchandising/audit/{audit_id} → Get shelf audit report by ID
  GET    /merchandising/audits        → List shelf audits with pagination & filters
  GET    /merchandising/dashboard     → 5-Module Management Analytics Dashboard with Exception-First Ranking
  GET    /merchandising/outlets       → List retail outlets master data

Also aliased under /api/merchandising/... and /api/v2/... for full backward compatibility.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditIssueRecord, AuditStage, Outlet, ShelfAudit, Visit
from app.schemas import (
    AuditAnalyzeRequest,
    AuditVerifyRequest,
    AuditVerifyResponse,
    ExceptionOutletItem,
    ExecutiveOverviewModule,
    FieldForcePerformanceModule,
    ManagementDashboardResponse,
    ProductAvailabilityModule,
    ShelfAuditReport,
    ShelfIntelligenceModule,
    TrendsAndAlertsModule,
)
from app.services.ai_service import AIServiceError, analyze_shelf_audit

logger = logging.getLogger(__name__)

# Primary merchandising router: /merchandising/...
router = APIRouter(prefix="/merchandising", tags=["Retail Merchandising Audit & Analytics"])

# Backward-compatibility router for existing integrations: /api/v2/...
compat_v2_router = APIRouter(prefix="/api/v2", tags=["Retail Merchandising Audit & Analytics"], include_in_schema=False)

# ── Seed demo outlets if empty ──────────────────────────────────────────────────

DEMO_OUTLETS: List[Dict[str, Any]] = []


def _ensure_outlets_seeded(db: Session) -> None:
    """No-op: Outlets are optional and bulk seeding is disabled."""
    pass


def _persist_shelf_audit_results(
    db: Session,
    report: Any,
    image_url: str = "",
    outlet_id: Optional[str] = None,
    visit_id: Optional[str] = None,
    merchandiser_id: Optional[str] = None,
    merchandiser_name: Optional[str] = None,
) -> ShelfAudit:
    """
    Persists a ShelfAudit, all its AuditIssueRecord entries, and associates/creates Visit & Outlet.
    Unified across JSON-payload and File-upload endpoints.
    """
    resolved_outlet_id = outlet_id or getattr(report, "outlet_id", None)
    if resolved_outlet_id:
        outlet_rec = db.query(Outlet).filter(Outlet.id == resolved_outlet_id).first()
        if not outlet_rec:
            outlet_rec = Outlet(
                id=resolved_outlet_id,
                outlet_name=f"Store {resolved_outlet_id}",
                territory="Dhaka Central",
                distributor_name="PRAN Regional Distribution",
            )
            db.add(outlet_rec)
            db.flush()

    audit_rec = ShelfAudit(
        id=report.audit_id,
        visit_id=visit_id or report.visit_id,
        outlet_id=resolved_outlet_id,
        audit_stage=report.audit_stage or "STANDALONE",
        image_url=image_url or report.image_url or "",
        status=report.audit_summary.status,
        res_score=report.retail_execution_score.score,
        pran_shelf_share_pct=report.shelf_metrics.pran_shelf_share_pct,
        competitor_shelf_share_pct=report.shelf_metrics.competitor_shelf_share_pct,
        empty_shelf_share_pct=report.shelf_metrics.empty_shelf_share_pct,
        pran_facing_count=report.shelf_metrics.pran_facing_count,
        competitor_facing_count=report.shelf_metrics.competitor_facing_count,
        arrangement_score=report.shelf_metrics.arrangement_score,
        missing_skus_count=len(report.availability_alerts.missing_expected_skus),
        visible_gaps_count=len(report.availability_alerts.visible_gaps),
        issues_count=len(report.issues_detected),
        full_analysis=report.model_dump(mode="json"),
        input_tokens=report.token_usage.input_tokens if report.token_usage else 0,
        output_tokens=report.token_usage.output_tokens if report.token_usage else 0,
        total_tokens=report.token_usage.total_tokens if report.token_usage else 0,
        estimated_cost_usd=report.token_usage.estimated_cost_usd if report.token_usage else 0.0,
    )
    db.add(audit_rec)

    # Persist all flagged issues
    for issue in report.issues_detected:
        issue_rec = AuditIssueRecord(
            id=str(uuid.uuid4()),
            audit_id=report.audit_id,
            severity=issue.severity,
            issue=issue.issue,
            evidence=issue.evidence,
            recommended_action=issue.recommended_action,
            is_resolved=0,
        )
        db.add(issue_rec)

    # Track visit
    resolved_visit_id = visit_id or report.visit_id
    if resolved_visit_id:
        visit_rec = db.query(Visit).filter(Visit.id == resolved_visit_id).first()
        if not visit_rec:
            visit_rec = Visit(
                id=resolved_visit_id,
                outlet_id=resolved_outlet_id or "OUT-GENERAL",
                merchandiser_id=merchandiser_id or "M-1001",
                merchandiser_name=merchandiser_name or "Field Merchandiser",
                status="IN_PROGRESS",
                initial_score=report.retail_execution_score.score,
            )
            db.add(visit_rec)
        elif not visit_rec.initial_score:
            visit_rec.initial_score = report.retail_execution_score.score
    elif merchandiser_name or merchandiser_id or resolved_outlet_id:
        new_visit_id = str(uuid.uuid4())
        visit_rec = Visit(
            id=new_visit_id,
            outlet_id=resolved_outlet_id or "OUT-GENERAL",
            merchandiser_id=merchandiser_id or "M-1001",
            merchandiser_name=merchandiser_name or "Field Merchandiser",
            status="COMPLETED",
            initial_score=report.retail_execution_score.score,
            final_score=report.retail_execution_score.score,
        )
        db.add(visit_rec)
        audit_rec.visit_id = new_visit_id

    db.commit()
    return audit_rec


# ── Audit Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/analyze",
    response_model=ShelfAuditReport,
    summary="Analyze Shelf Photo (URL or Base64)",
    description="Full retail merchandising shelf audit: checks multi-brand facings, width Share of Shelf, planogram, missing SKUs, pricing & generates Retail Execution Score.",
)
@router.post(
    "/audit/analyze",
    response_model=ShelfAuditReport,
    include_in_schema=False,
)
@compat_v2_router.post(
    "/audit/analyze",
    response_model=ShelfAuditReport,
    include_in_schema=False,
)
def run_shelf_audit(
    payload: AuditAnalyzeRequest,
    db: Session = Depends(get_db),
) -> ShelfAuditReport:
    img_input = payload.image_url or payload.image
    if not img_input:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="image_url or image base64 payload is required",
        )

    # Optional outlet master data if present
    target_sos = 45.0
    expected_skus = payload.expected_skus
    if payload.outlet_id:
        outlet_rec = db.query(Outlet).filter(Outlet.id == payload.outlet_id).first()
        if outlet_rec:
            target_sos = outlet_rec.target_pran_sos_pct or target_sos
            if not expected_skus and outlet_rec.expected_skus:
                expected_skus = outlet_rec.expected_skus

    # Execute vision + decoupled rules engine
    try:
        report = analyze_shelf_audit(
            image_input=img_input,
            outlet_id=payload.outlet_id,
            visit_id=payload.visit_id,
            audit_stage=payload.audit_stage or "STANDALONE",
            merchandiser_id=payload.merchandiser_id,
            merchandiser_name=payload.merchandiser_name,
            expected_skus=expected_skus,
            planogram_rules=payload.planogram_rules,
            approved_prices=payload.approved_prices,
            target_pran_sos=target_sos,
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during shelf audit: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Audit failed: {exc}")

    _persist_shelf_audit_results(
        db=db,
        report=report,
        image_url=report.image_url or "",
        outlet_id=payload.outlet_id,
        visit_id=payload.visit_id,
        merchandiser_id=payload.merchandiser_id,
        merchandiser_name=payload.merchandiser_name,
    )
    return report


@router.post(
    "/analyze/file",
    response_model=ShelfAuditReport,
    summary="Analyze Shelf Photo from File Upload",
    description="Full retail merchandising shelf audit from uploaded photo file.",
)
@router.post(
    "/audit/analyze/file",
    response_model=ShelfAuditReport,
    include_in_schema=False,
)
@compat_v2_router.post(
    "/audit/analyze/file",
    response_model=ShelfAuditReport,
    include_in_schema=False,
)
async def run_shelf_audit_file(
    file: UploadFile = File(...),
    outlet_id: Optional[str] = Form(None),
    visit_id: Optional[str] = Form(None),
    audit_stage: Optional[str] = Form("STANDALONE"),
    merchandiser_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> ShelfAuditReport:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    payload = AuditAnalyzeRequest(
        image=None,
        image_url=None,
        outlet_id=outlet_id,
        visit_id=visit_id,
        audit_stage=audit_stage,
        merchandiser_id=merchandiser_id,
    )

    target_sos = 45.0
    expected_skus = None
    if outlet_id:
        outlet_rec = db.query(Outlet).filter(Outlet.id == outlet_id).first()
        if outlet_rec:
            target_sos = outlet_rec.target_pran_sos_pct or target_sos
            expected_skus = outlet_rec.expected_skus

    try:
        report = await run_in_threadpool(
            analyze_shelf_audit,
            image_input=content,
            outlet_id=outlet_id,
            visit_id=visit_id,
            audit_stage=audit_stage or "STANDALONE",
            merchandiser_id=merchandiser_id,
            expected_skus=expected_skus,
            target_pran_sos=target_sos,
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during shelf audit file upload: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Audit failed: {exc}")

    _persist_shelf_audit_results(
        db=db,
        report=report,
        image_url="",
        outlet_id=outlet_id,
        visit_id=visit_id,
        merchandiser_id=merchandiser_id,
        merchandiser_name="Field Merchandiser",
    )
    return report


@router.post(
    "/verify",
    response_model=AuditVerifyResponse,
    summary="Verify Shelf Corrections (Before-and-After)",
    description="Compares today's BEFORE photo with the AFTER photo captured following merchandiser fixes.",
)
@router.post(
    "/audit/verify",
    response_model=AuditVerifyResponse,
    include_in_schema=False,
)
@compat_v2_router.post(
    "/audit/verify",
    response_model=AuditVerifyResponse,
    include_in_schema=False,
)
def verify_shelf_corrections(
    payload: AuditVerifyRequest,
    db: Session = Depends(get_db),
) -> AuditVerifyResponse:
    # 1. Fetch BEFORE audit
    before_audit = db.query(ShelfAudit).filter(ShelfAudit.id == payload.audit_id).first()
    if not before_audit:
        raise HTTPException(status_code=404, detail=f"Before audit '{payload.audit_id}' not found")

    after_img = payload.after_image_url or payload.after_image
    if not after_img:
        raise HTTPException(status_code=422, detail="after_image_url or after_image is required")

    # 2. Run AFTER audit
    after_report = analyze_shelf_audit(
        image_input=after_img,
        outlet_id=before_audit.outlet_id,
        visit_id=before_audit.visit_id,
        audit_stage="AFTER",
    )

    before_score = before_audit.res_score or 70.0
    after_score = after_report.retail_execution_score.score
    score_improvement = round(after_score - before_score, 1)

    before_sos = before_audit.pran_shelf_share_pct or 40.0
    after_sos = after_report.shelf_metrics.pran_shelf_share_pct or 40.0
    sos_delta = round(after_sos - before_sos, 1)

    # 3. Mark issues resolved
    db.query(AuditIssueRecord).filter(AuditIssueRecord.audit_id == before_audit.id).update({"is_resolved": 1})
    resolved = payload.completed_actions or [
        "Replenished missing expected SKUs from inventory",
        "Realigned misaligned product facings on middle shelf",
    ]
    remaining = [iss.issue for iss in after_report.issues_detected if iss.severity == "HIGH"]

    is_passed = after_score >= 80.0 or score_improvement >= 10.0

    # 4. Save AFTER audit record
    after_rec = ShelfAudit(
        id=after_report.audit_id,
        visit_id=before_audit.visit_id,
        outlet_id=before_audit.outlet_id,
        audit_stage="AFTER",
        image_url=after_report.image_url or "",
        status="acceptable" if is_passed else "needs_attention",
        res_score=after_score,
        pran_shelf_share_pct=after_sos,
        competitor_shelf_share_pct=after_report.shelf_metrics.competitor_shelf_share_pct,
        empty_shelf_share_pct=after_report.shelf_metrics.empty_shelf_share_pct,
        pran_facing_count=after_report.shelf_metrics.pran_facing_count,
        competitor_facing_count=after_report.shelf_metrics.competitor_facing_count,
        arrangement_score=after_report.shelf_metrics.arrangement_score,
        missing_skus_count=len(after_report.availability_alerts.missing_expected_skus),
        visible_gaps_count=len(after_report.availability_alerts.visible_gaps),
        issues_count=len(after_report.issues_detected),
        full_analysis=after_report.model_dump(mode="json"),
        input_tokens=after_report.token_usage.input_tokens if after_report.token_usage else 0,
        output_tokens=after_report.token_usage.output_tokens if after_report.token_usage else 0,
        total_tokens=after_report.token_usage.total_tokens if after_report.token_usage else 0,
        estimated_cost_usd=after_report.token_usage.estimated_cost_usd if after_report.token_usage else 0.0,
    )
    db.add(after_rec)

    # Persist any remaining issues from AFTER audit
    for issue in after_report.issues_detected:
        issue_rec = AuditIssueRecord(
            id=str(uuid.uuid4()),
            audit_id=after_report.audit_id,
            severity=issue.severity,
            issue=issue.issue,
            evidence=issue.evidence,
            recommended_action=issue.recommended_action,
            is_resolved=0,
        )
        db.add(issue_rec)

    # Update visit status
    if before_audit.visit_id:
        visit_rec = db.query(Visit).filter(Visit.id == before_audit.visit_id).first()
        if visit_rec:
            visit_rec.final_score = after_score
            visit_rec.status = "COMPLETED"

    db.commit()

    return AuditVerifyResponse(
        visit_id=before_audit.visit_id or str(uuid.uuid4()),
        before_audit_id=before_audit.id,
        after_audit_id=after_report.audit_id,
        before_score=before_score,
        after_score=after_score,
        score_improvement=score_improvement,
        is_audit_passed=is_passed,
        resolved_issues=resolved,
        remaining_issues=remaining,
        pran_sos_delta_pct=sos_delta,
        after_report=after_report,
    )


@router.get("/audit/{audit_id}", response_model=ShelfAuditReport, summary="Get Shelf Audit by ID")
@router.get("/audits/{audit_id}", response_model=ShelfAuditReport, include_in_schema=False)
@compat_v2_router.get("/audit/{audit_id}", response_model=ShelfAuditReport, include_in_schema=False)
def get_audit_by_id(audit_id: str, db: Session = Depends(get_db)) -> ShelfAuditReport:
    rec = db.query(ShelfAudit).filter(ShelfAudit.id == audit_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Shelf audit not found")
    if rec.full_analysis:
        return ShelfAuditReport(**rec.full_analysis)
    raise HTTPException(status_code=404, detail="Full audit report data unavailable")


@router.get("/audits", summary="List Shelf Audits")
@compat_v2_router.get("/audits", include_in_schema=False)
def list_shelf_audits(
    outlet_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    query = db.query(ShelfAudit)
    if outlet_id:
        query = query.filter(ShelfAudit.outlet_id == outlet_id)
    total = query.count()
    items = query.order_by(desc(ShelfAudit.created_at)).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "audit_id": it.id,
                "visit_id": it.visit_id,
                "outlet_id": it.outlet_id,
                "audit_stage": it.audit_stage,
                "status": it.status,
                "res_score": it.res_score,
                "pran_shelf_share_pct": it.pran_shelf_share_pct,
                "competitor_shelf_share_pct": it.competitor_shelf_share_pct,
                "missing_skus_count": it.missing_skus_count,
                "created_at": it.created_at,
            }
            for it in items
        ],
    }


# ── Management Dashboard Endpoint (5 Modules + Exception-First) ───────────────


@router.get(
    "/dashboard",
    response_model=ManagementDashboardResponse,
    summary="5-Module Management Analytics Dashboard",
    description="Aggregates executive KPIs, shelf intelligence, availability, field-force productivity, and exception-first outlet rankings directly from database audits.",
)
@router.get(
    "/management/dashboard",
    response_model=ManagementDashboardResponse,
    include_in_schema=False,
)
@compat_v2_router.get(
    "/management/dashboard",
    response_model=ManagementDashboardResponse,
    include_in_schema=False,
)
def get_management_dashboard(
    territory: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> ManagementDashboardResponse:
    # Base query for audits
    audits_query = db.query(ShelfAudit)

    if territory:
        audits_query = audits_query.join(Outlet, ShelfAudit.outlet_id == Outlet.id).filter(Outlet.territory == territory)

    matching_audits = audits_query.order_by(desc(ShelfAudit.created_at)).all()

    # Filter by category if specified (inspecting detected_products)
    if category and category.lower() != "all categories":
        cat_lower = category.lower()
        filtered_audits = []
        for a in matching_audits:
            if a.full_analysis and "detected_products" in a.full_analysis:
                if any(cat_lower in (p.get("category") or "").lower() or cat_lower in (p.get("product_name") or "").lower() for p in a.full_analysis["detected_products"]):
                    filtered_audits.append(a)
        matching_audits = filtered_audits

    total_audits = len(matching_audits)

    # Outlets covered
    distinct_outlet_ids = {a.outlet_id for a in matching_audits if a.outlet_id}
    outlets_covered = len(distinct_outlet_ids)

    # Aggregates from audits
    if total_audits > 0:
        avg_sos = sum((a.pran_shelf_share_pct or 0.0) for a in matching_audits) / total_audits
        avg_score = sum((a.res_score or 0.0) for a in matching_audits) / total_audits
        failed_count = sum(1 for a in matching_audits if (a.res_score or 0.0) < 65.0)
        total_gaps = sum((a.visible_gaps_count or 0) for a in matching_audits)
    else:
        avg_sos = 0.0
        avg_score = 0.0
        failed_count = 0
        total_gaps = 0

    # Scoped audit IDs
    matching_audit_ids = [a.id for a in matching_audits]

    # Dynamic Issue Resolution Rate (strictly scoped to matching audits)
    if matching_audit_ids:
        total_issues = db.query(AuditIssueRecord).filter(AuditIssueRecord.audit_id.in_(matching_audit_ids)).count()
        resolved_issues = (
            db.query(AuditIssueRecord)
            .filter(AuditIssueRecord.audit_id.in_(matching_audit_ids))
            .filter(AuditIssueRecord.is_resolved == 1)
            .count()
        )
        issue_resolution_rate = round((resolved_issues / total_issues * 100.0), 1) if total_issues > 0 else 0.0
    else:
        total_issues = 0
        resolved_issues = 0
        issue_resolution_rate = 0.0

    # Module 1: Executive Overview
    exec_overview = ExecutiveOverviewModule(
        total_audits=total_audits,
        outlets_covered=outlets_covered,
        avg_pran_sos_pct=round(float(avg_sos), 1),
        avg_execution_score=round(float(avg_score), 1),
        issue_resolution_rate_pct=issue_resolution_rate,
        failed_audits_count=failed_count,
    )

    # Module 2: Shelf Intelligence (Calculate Golden Shelf Share & Category Shares from detected facings)
    total_eye_facings = 0
    pran_eye_facings = 0
    cat_dict: Dict[str, Dict[str, int]] = {}

    for a in matching_audits:
        if a.full_analysis and "detected_products" in a.full_analysis:
            for p in a.full_analysis["detected_products"]:
                facings = int(p.get("facing_count") or 1)
                brand = str(p.get("brand") or "PRAN").upper()
                cname = p.get("category") or "General"

                if cname not in cat_dict:
                    cat_dict[cname] = {"pran": 0, "comp": 0}

                if "PRAN" in brand:
                    cat_dict[cname]["pran"] += facings
                else:
                    cat_dict[cname]["comp"] += facings

                if p.get("shelf_level") == "eye_level":
                    total_eye_facings += facings
                    if "PRAN" in brand:
                        pran_eye_facings += facings

    if total_eye_facings > 0:
        golden_shelf_eye_level_pran_pct = round((pran_eye_facings / total_eye_facings) * 100.0, 1)
    elif total_audits > 0 and avg_sos > 0:
        golden_shelf_eye_level_pran_pct = round(float(avg_sos), 1)
    else:
        golden_shelf_eye_level_pran_pct = 0.0

    category_shares = []
    for cname, counts in cat_dict.items():
        tot = counts["pran"] + counts["comp"]
        if tot > 0:
            category_shares.append({
                "category": cname,
                "pran_share_pct": round((counts["pran"] / tot) * 100.0, 1),
                "competitor_share_pct": round((counts["comp"] / tot) * 100.0, 1),
            })

    shelf_intel = ShelfIntelligenceModule(
        pran_overall_sos_pct=round(float(avg_sos), 1),
        competitor_overall_sos_pct=round(max(0.0, 100.0 - float(avg_sos)), 1) if avg_sos > 0 else 0.0,
        golden_shelf_eye_level_pran_pct=golden_shelf_eye_level_pran_pct,
        top_competitors=[],
        category_shares=category_shares,
    )

    # Module 3: Product Availability (Scoping missing issues & true OSA)
    missing_issues = []
    if matching_audit_ids:
        missing_issues = (
            db.query(AuditIssueRecord.issue)
            .filter(AuditIssueRecord.audit_id.in_(matching_audit_ids))
            .filter(AuditIssueRecord.issue.like("Expected product not visible: %"))
            .all()
        )

    top_missing_skus = []
    if missing_issues:
        sku_counter = Counter([iss[0].replace("Expected product not visible: ", "").strip() for iss in missing_issues])
        for sku_name, count in sku_counter.most_common(4):
            cat = "Beverages" if any(k in sku_name for k in ["Juice", "Float", "Lassi", "Frooto", "Drinko"]) else (
                  "Noodles" if "Noodles" in sku_name else ("Biscuits" if any(k in sku_name for k in ["Biscuit", "Potata"]) else "Culinary"))
            pct = round((count / max(1, total_audits)) * 100.0, 1)
            top_missing_skus.append({
                "sku_name": sku_name,
                "category": cat,
                "outlets_affected_pct": min(100.0, pct)
            })

    # True OSA: (total_expected_skus_evaluated - missing_skus) / total_expected_skus_evaluated * 100
    total_expected_count = 0
    total_missing_count = 0
    for a in matching_audits:
        if a.full_analysis:
            alerts = a.full_analysis.get("availability_alerts") or {}
            missing_list = alerts.get("missing_expected_skus") or []
            total_missing_count += len(missing_list)
            plano = a.full_analysis.get("category_planogram") or {}
            expected_list = plano.get("core_skus_expected") or []
            total_expected_count += max(len(expected_list), len(missing_list) + 3)

    if total_expected_count > 0:
        overall_osa_pct = round(max(0.0, min(100.0, (total_expected_count - total_missing_count) / total_expected_count * 100.0)), 1)
    elif total_audits > 0:
        overall_osa_pct = 100.0
    else:
        overall_osa_pct = 0.0

    product_avail = ProductAvailabilityModule(
        overall_osa_pct=overall_osa_pct,
        total_gaps_flagged=int(total_gaps),
        top_missing_skus=top_missing_skus,
        chronic_stockout_outlets_count=0,
    )

    # Module 4: Field-Force Performance (Scoping visits & dynamic merchandiser territory)
    visits_q = db.query(Visit)
    if territory:
        visits_q = visits_q.join(Outlet, Visit.outlet_id == Outlet.id).filter(Outlet.territory == territory)

    total_visits = visits_q.count()
    completed_visits = visits_q.filter(Visit.status == "COMPLETED").count()
    comp_rate = round((completed_visits / total_visits * 100.0), 1) if total_visits > 0 else 0.0

    top_merch_query = (
        db.query(
            Visit.merchandiser_name,
            func.count(Visit.id).label("audits_done"),
            func.avg(func.coalesce(Visit.final_score, Visit.initial_score)).label("avg_score"),
            func.max(Outlet.territory).label("territory")
        )
        .outerjoin(Outlet, Visit.outlet_id == Outlet.id)
    )
    if territory:
        top_merch_query = top_merch_query.filter(Outlet.territory == territory)

    top_merch_rows = (
        top_merch_query
        .group_by(Visit.merchandiser_name)
        .order_by(desc("audits_done"))
        .limit(4)
        .all()
    )
    top_merchandisers = []
    for m in top_merch_rows:
        name = m[0] or "Field Merchandiser"
        resolved_territory = m[3] or territory or "General Territory"
        top_merchandisers.append({
            "name": name,
            "territory": resolved_territory,
            "audits_done": int(m[1]),
            "avg_score": round(float(m[2] or 0.0), 1),
        })

    field_force = FieldForcePerformanceModule(
        total_visits_planned=total_visits,
        total_visits_completed=completed_visits,
        completion_rate_pct=comp_rate,
        avg_visit_duration_mins=0.0,
        first_time_fix_rate_pct=issue_resolution_rate,
        top_merchandisers=top_merchandisers,
    )

    # Module 5: Trends and Alerts (OUTER JOIN to include standalone audits)
    territory_query = (
        db.query(
            func.coalesce(Outlet.territory, "Unassigned Territory").label("territory"),
            func.avg(ShelfAudit.pran_shelf_share_pct).label("avg_sos"),
            func.count(ShelfAudit.id).label("audits_count")
        )
        .outerjoin(Outlet, ShelfAudit.outlet_id == Outlet.id)
        .group_by(func.coalesce(Outlet.territory, "Unassigned Territory"))
        .order_by(desc("avg_sos"))
        .all()
    )
    territory_rankings = []
    for row in territory_query:
        sos_val = round(float(row[1] or 0.0), 1)
        st = "Leader" if sos_val >= 45.0 else ("On Track" if sos_val >= 40.0 else ("Needs Attention" if sos_val >= 38.0 else "Critical Gap"))
        territory_rankings.append({
            "territory": row[0],
            "avg_pran_sos_pct": sos_val,
            "status": st
        })

    trends = TrendsAndAlertsModule(
        territory_rankings=territory_rankings,
        critical_alerts_count=failed_count,
        week_over_week_sos_gain_pct=0.0,
    )

    # Exception-First Outlets (OUTER JOIN to include unassigned / standalone audits)
    exception_query = (
        db.query(
            func.coalesce(Outlet.id, ShelfAudit.id).label("oid"),
            func.coalesce(Outlet.outlet_name, "General Retail Shelf").label("oname"),
            func.coalesce(Outlet.territory, "Unassigned Territory").label("oterr"),
            func.coalesce(Outlet.distributor_name, "Regional PRAN Hub").label("odist"),
            func.avg(ShelfAudit.res_score).label("avg_res"),
            func.max(ShelfAudit.missing_skus_count).label("max_missing"),
            func.avg(ShelfAudit.competitor_shelf_share_pct).label("avg_comp_sos")
        )
        .outerjoin(Outlet, ShelfAudit.outlet_id == Outlet.id)
    )
    if territory:
        exception_query = exception_query.filter(Outlet.territory == territory)

    exception_rows = (
        exception_query
        .group_by(
            func.coalesce(Outlet.id, ShelfAudit.id),
            func.coalesce(Outlet.outlet_name, "General Retail Shelf"),
            func.coalesce(Outlet.territory, "Unassigned Territory"),
            func.coalesce(Outlet.distributor_name, "Regional PRAN Hub")
        )
        .order_by(func.avg(ShelfAudit.res_score).asc())
        .limit(4)
        .all()
    )
    exception_outlets = []
    for row in exception_rows:
        oid, oname, oterr, odist, avg_res, max_miss, avg_comp = row
        score_val = round(float(avg_res or 0.0), 1)
        miss_count = int(max_miss or 0)
        comp_share = round(float(avg_comp or 0.0), 1)
        urgency = round(min(99.0, max(0.0, (100.0 - score_val) * 0.7 + (miss_count * 9.0) + (comp_share - 50.0) * 0.5)), 1)
        issue_desc = f"{miss_count} core expected SKU(s) missing; competitor shelf presence at {comp_share}%"
        exception_outlets.append(ExceptionOutletItem(
            outlet_id=oid,
            outlet_name=oname,
            territory=oterr,
            distributor_name=odist or "Regional PRAN Hub",
            last_score=score_val,
            urgency_index=urgency,
            missing_skus_count=miss_count,
            competitor_share_pct=comp_share,
            primary_issue=issue_desc,
        ))

    return ManagementDashboardResponse(
        executive_overview=exec_overview,
        shelf_intelligence=shelf_intel,
        product_availability=product_avail,
        field_force_performance=field_force,
        trends_and_alerts=trends,
        exception_outlets=exception_outlets,
    )


@router.get("/outlets", summary="List Retail Outlets")
@compat_v2_router.get("/outlets", include_in_schema=False)
def list_outlets(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    _ensure_outlets_seeded(db)
    outlets = db.query(Outlet).all()
    return [
        {
            "id": o.id,
            "outlet_name": o.outlet_name,
            "outlet_type": o.outlet_type,
            "territory": o.territory,
            "distributor_name": o.distributor_name,
            "tier": o.tier,
            "target_pran_sos_pct": o.target_pran_sos_pct,
            "expected_skus": o.expected_skus,
        }
        for o in outlets
    ]
