"""
app/services/rules_engine.py — Image-Only Business Rules & Retail Execution Scoring Engine.

Pure image-provable merchandising evaluation without external assumptions:
  1. Presence: PRAN products detected, distinct SKUs, variety (Max 20)
  2. Visibility / SOS: PRAN share of shelf vs configurable benchmark (Max 25)
  3. Placement: PRAN facings at eye level (Golden Zone) vs top/bottom (Max 20)
  4. Arrangement: Product alignment, variant grouping, front-facing presentation (Max 15)
  5. Stock Health: Visible voids and empty shelf percentage around PRAN items (Max 10)
  6. Price & Promo Visibility: Visible readable price labels and wobbler / POSM presence (Max 10)

Special Case:
  If no PRAN products are detected, returns score: None (null) and status: "no_pran_detected"
  to prevent awarding free points from unearned secondary pillars.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.brands import is_known_pran_token
from app.config import get_settings
from app.schemas import (
    AvailabilityAlerts,
    BrandClass,
    DetectedProductDetail,
    IssueDetected,
    PricingAndPromotions,
    RetailExecutionScore,
    ShelfMetrics,
)

logger = logging.getLogger(__name__)


@dataclass
class Pillar:
    name: str
    score: float
    max_pts: float
    evaluated: bool = True


def stock_health(empty_pct: float, gaps_near_pran: int) -> Pillar:
    """Stock health pillar: 10.0 pts base minus voids and empty shelf share."""
    s = 10.0 - min(empty_pct * 0.2, 5.0) - min(gaps_near_pran * 2.0, 5.0)
    return Pillar("stock_health", max(0.0, min(10.0, s)), 10.0)


def price_promo(price_tag_ratio: float, promo_detected: bool) -> Pillar:
    """Price & promo visibility: up to 7 pts for price tag coverage, 3 pts for POSM/wobblers."""
    s = price_tag_ratio * 7.0 + (3.0 if promo_detected else 0.0)
    return Pillar("price_promo", max(0.0, min(10.0, s)), 10.0)


def compute_res(pillars: List[Pillar]) -> Dict[str, Any]:
    """Calculates normalized Retail Execution Score across all evaluated image-provable pillars."""
    ev = [p for p in pillars if p.evaluated]
    possible = sum(p.max_pts for p in ev)
    earned = sum(p.score for p in ev)
    score = round(earned / possible * 100.0, 1) if possible > 0 else None
    return {
        "score": score,
        "pillars": {p.name: round(p.score, 1) for p in pillars},
        "mode": "image_only",
    }


def evaluate_retail_execution(
    detected_products: List[DetectedProductDetail],
    shelf_metrics: ShelfMetrics,
    availability_alerts: AvailabilityAlerts,
    pricing_and_promotions: List[PricingAndPromotions],
    expected_skus: Optional[List[str]] = None,
    target_pran_sos: Optional[float] = None,
    approved_prices: Optional[Dict[str, float]] = None,
) -> Tuple[RetailExecutionScore, List[IssueDetected], List[str], List[str], str]:
    """
    Image-only evaluation. Ignores external assumptions and scores purely what is provable in the image.
    Returns:
      - RetailExecutionScore (with 6 image-only pillars and composite score)
      - List of IssueDetected
      - Recommended Actions (merchandiser checklist)
      - Management Insights
      - Overall Status ('acceptable', 'needs_attention', or 'no_pran_detected')
    """
    settings = get_settings()
    benchmark_sos = float(getattr(settings, "sos_benchmark_pct", 30.0))
    issue_sos_threshold = float(getattr(settings, "sos_issue_threshold_pct", 15.0))

    generated_issues: List[IssueDetected] = []
    recommended_actions: List[str] = []
    management_insights: List[str] = []
    deductions: List[str] = []

    # Filter PRAN products strictly by BrandClass.PRAN
    pran_items = [p for p in detected_products if p.brand_class == BrandClass.PRAN]
    total_pran_facings = sum(p.facing_count or 1 for p in pran_items)

    # ──────────────────────────────────────────────────────────────────────────
    # SPECIAL CASE: No PRAN detected on shelf fixture
    # ──────────────────────────────────────────────────────────────────────────
    if total_pran_facings == 0:
        logger.info("Image-only audit: zero PRAN products detected on shelf")
        management_insights.append("No PRAN products visible; this may be a competitor-only fixture or the wrong photo.")
        generated_issues.append(
            IssueDetected(
                severity="MEDIUM",
                issue="No PRAN products detected",
                evidence="Zero PRAN front facings identified in shelf image",
                recommended_action="Verify this is an authorized PRAN category fixture and re-audit.",
            )
        )
        res = RetailExecutionScore(
            score=None,
            grade="No PRAN Detected",
            mode="image_only",
            pillars={
                "presence": 0.0,
                "visibility_sos": 0.0,
                "placement": 0.0,
                "arrangement": 0.0,
                "stock_health": 0.0,
                "price_promo": 0.0,
            },
            availability_score=0.0,
            planogram_score=0.0,
            sos_score=0.0,
            pricing_score=0.0,
            promotion_score=0.0,
            deductions=["No PRAN products visible on shelf fixture"],
        )
        return res, generated_issues, ["Verify shelf section and capture a photo containing PRAN inventory."], management_insights, "no_pran_detected"

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 1: Presence (Max 20 pts)
    # PRAN products detected at all; number of distinct products & variety
    # ──────────────────────────────────────────────────────────────────────────
    distinct_names = {p.product_name.lower() for p in pran_items if p.product_name}
    distinct_count = len(distinct_names)
    # Base 8 pts for presence, +3 pts per distinct product up to max 20 pts
    presence_score = min(20.0, 8.0 + (distinct_count * 3.0))
    pillar_presence = Pillar("presence", presence_score, 20.0)
    if presence_score < 14.0:
        deductions.append(f"[-] -{20.0 - presence_score:.1f} pts: Low PRAN product variety ({distinct_count} distinct SKU(s) visible)")
        management_insights.append(f"PRAN variety is limited to {distinct_count} SKU(s) on this shelf fixture.")
    else:
        management_insights.append(f"Strong product variety with {distinct_count} distinct PRAN items visible.")

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 2: Visibility / SOS (Max 25 pts)
    # PRAN share of shelf vs configurable benchmark
    # ──────────────────────────────────────────────────────────────────────────
    actual_sos = shelf_metrics.pran_shelf_share_pct if shelf_metrics.pran_shelf_share_pct is not None else 0.0
    # Score proportional to benchmark (e.g. 30% SOS earns full 25 points)
    sos_score = min(25.0, (actual_sos / max(benchmark_sos, 1.0)) * 25.0)
    pillar_sos = Pillar("visibility_sos", sos_score, 25.0)

    if actual_sos < issue_sos_threshold:
        deductions.append(f"[-] -{25.0 - sos_score:.1f} pts: Low PRAN shelf share ({actual_sos:.1f}% vs reference benchmark {benchmark_sos:.1f}%)")
        generated_issues.append(
            IssueDetected(
                severity="LOW",
                issue="Low PRAN shelf presence",
                evidence=f"PRAN SOS {actual_sos:.1f}% (reference benchmark {benchmark_sos:.1f}%, default)",
                recommended_action="Review shelf space and explore adding additional PRAN facings.",
            )
        )
        recommended_actions.append("Explore expanding PRAN linear shelf space if store policy allows.")

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 3: Placement (Max 20 pts)
    # PRAN facings at eye level (Golden Zone) vs top / bottom
    # ──────────────────────────────────────────────────────────────────────────
    pran_eye_facings = sum(p.facing_count or 1 for p in pran_items if p.shelf_level == "eye_level")
    eye_ratio = (pran_eye_facings / max(total_pran_facings, 1))
    placement_score = round(eye_ratio * 20.0, 1)
    pillar_placement = Pillar("placement", placement_score, 20.0)

    if eye_ratio < 0.35:
        deductions.append(f"[-] -{20.0 - placement_score:.1f} pts: Weak Golden Zone placement ({eye_ratio * 100:.0f}% of PRAN items at eye level)")
        generated_issues.append(
            IssueDetected(
                severity="MEDIUM",
                issue="Low eye-level Golden Zone presence",
                evidence=f"Only {pran_eye_facings} of {total_pran_facings} PRAN facings are placed at prime eye level",
                recommended_action="Negotiate moving high-margin PRAN core items to eye-level shelf tiers.",
            )
        )
        recommended_actions.append("Relocate high-velocity PRAN SKUs to middle eye-level shelf tier.")
    else:
        management_insights.append(f"Healthy eye-level placement: {pran_eye_facings} facings ({eye_ratio * 100:.0f}%) situated in the Golden Zone.")

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 4: Arrangement (Max 15 pts)
    # Alignment, variant grouping, front-facing, no stacking mess
    # ──────────────────────────────────────────────────────────────────────────
    raw_arr = shelf_metrics.arrangement_score if shelf_metrics.arrangement_score is not None else 75.0
    if 0 < raw_arr <= 10.0:
        raw_arr = raw_arr * 10.0
    arrangement_pts = round((raw_arr / 100.0) * 15.0, 1)
    pillar_arr = Pillar("arrangement", arrangement_pts, 15.0)

    if raw_arr < 70.0:
        deductions.append(f"[-] -{15.0 - arrangement_pts:.1f} pts: Product facings misaligned or irregular grouping")
        generated_issues.append(
            IssueDetected(
                severity="MEDIUM" if raw_arr < 60.0 else "LOW",
                issue="Shelf arrangement needs alignment",
                evidence=f"Arrangement assessed at {raw_arr:.0f}/100",
                recommended_action="Align product labels forward and group same-brand variants together.",
            )
        )
        recommended_actions.append("Align package labels forward and cluster identical flavor variants.")

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 5: Stock Health (Max 10 pts)
    # Visible gaps and empty shelf % around PRAN products
    # ──────────────────────────────────────────────────────────────────────────
    empty_pct = float(shelf_metrics.empty_shelf_share_pct or 0.0)
    gaps_count = len(availability_alerts.visible_gaps)
    pillar_stock = stock_health(empty_pct, gaps_count)

    if gaps_count > 0 or empty_pct > 5.0:
        deductions.append(f"[-] -{10.0 - pillar_stock.score:.1f} pts: Stock health penalty ({gaps_count} gap(s), {empty_pct:.1f}% empty space)")
        generated_issues.append(
            IssueDetected(
                severity="HIGH" if gaps_count >= 2 else "MEDIUM",
                issue=f"{gaps_count} empty shelf space(s) detected",
                evidence="Visible voids detected on shelf indicating stockout risk",
                recommended_action="Front-face existing units or replenish empty slots from store stock.",
            )
        )
        recommended_actions.append("Refill empty shelf gaps from available inventory or pull products forward.")

    # ──────────────────────────────────────────────────────────────────────────
    # PILLAR 6: Price & Promo Visibility (Max 10 pts)
    # Price tag present near PRAN items; wobbler/POSM present
    # ──────────────────────────────────────────────────────────────────────────
    pran_priced = 0
    for pr in pricing_and_promotions:
        if pr.visible_price is not None:
            if pr.product and (is_known_pran_token(pr.product) or "pran" in pr.product.lower()):
                pran_priced += 1
    price_tag_ratio = min(1.0, pran_priced / max(distinct_count, 1)) if distinct_count > 0 else 0.0
    has_promo = any(pr.promotion_detected for pr in pricing_and_promotions)

    pillar_price_promo = price_promo(price_tag_ratio, has_promo)
    if not has_promo:
        deductions.append("[-] -3.0 pts: No visible promotional signage, talkers, or wobblers detected")
    if price_tag_ratio < 0.5:
        deductions.append(f"[-] -{7.0 * (1.0 - price_tag_ratio):.1f} pts: Missing visible shelf price tags on PRAN items")

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTE COMPOSITE RES
    # ──────────────────────────────────────────────────────────────────────────
    pillars = [pillar_presence, pillar_sos, pillar_placement, pillar_arr, pillar_stock, pillar_price_promo]
    res_data = compute_res(pillars)
    final_score = res_data["score"]

    if final_score is None:
        grade = "No PRAN Detected"
        status = "no_pran_detected"
    elif final_score >= 85.0:
        grade = "Excellent"
        status = "acceptable"
    elif final_score >= 70.0:
        grade = "Good"
        status = "acceptable"
    elif final_score >= 55.0:
        grade = "Needs Attention"
        status = "needs_attention"
    else:
        grade = "Critical Action Required"
        status = "needs_attention"

    # Deduplicate actions
    seen_act = set()
    deduped_actions = []
    for a in recommended_actions:
        if a not in seen_act:
            seen_act.add(a)
            deduped_actions.append(a)

    res = RetailExecutionScore(
        score=final_score,
        grade=grade,
        mode="image_only",
        pillars=res_data["pillars"],
        availability_score=round(pillar_presence.score, 1),
        planogram_score=round(pillar_placement.score + pillar_arr.score, 1),
        sos_score=round(pillar_sos.score, 1),
        pricing_score=round(pillar_price_promo.score, 1),
        promotion_score=round(3.0 if has_promo else 0.0, 1),
        deductions=deductions,
    )

    return res, generated_issues, deduped_actions, management_insights, status
