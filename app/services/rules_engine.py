"""
app/services/rules_engine.py — Decoupled Business Rules & Retail Execution Scoring Engine.

Separates computer vision perception from PRAN business evaluation logic:
  1. Assortment & On-Shelf Availability (OSA)
  2. Planogram & Shelf-Level (Golden Zone) Placement
  3. Share of Shelf (SOS) Target Compliance
  4. Price & Promotional Compliance
  5. Retail Execution Score (RES, 0–100) with explainable deduction breakdowns
  6. Actionable Merchandiser Checklist Generation
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import (
    AvailabilityAlerts,
    DetectedProductDetail,
    IssueDetected,
    PricingAndPromotions,
    RetailExecutionScore,
    ShelfMetrics,
)

logger = logging.getLogger(__name__)

# Default benchmark target for PRAN share of shelf
DEFAULT_TARGET_PRAN_SOS = 45.0

# Standard fallback catalog of core expected SKUs if none provided
DEFAULT_EXPECTED_CORE_SKUS = [
    "PRAN Frooto Mango Juice 250ml",
    "PRAN Drinko Float 330ml",
    "PRAN Mr. Noodles Magic Masala",
    "PRAN Potata Spicy Biscuit",
    "PRAN Lassi 200ml",
]


def evaluate_retail_execution(
    detected_products: List[DetectedProductDetail],
    shelf_metrics: ShelfMetrics,
    availability_alerts: AvailabilityAlerts,
    pricing_and_promotions: List[PricingAndPromotions],
    expected_skus: Optional[List[str]] = None,
    target_pran_sos: float = DEFAULT_TARGET_PRAN_SOS,
    approved_prices: Optional[Dict[str, float]] = None,
) -> Tuple[RetailExecutionScore, List[IssueDetected], List[str], List[str]]:
    """
    Evaluates raw AI detections against PRAN business rules.
    Returns:
      - RetailExecutionScore (0-100 with deduction breakdown)
      - Enhanced List of IssueDetected (sorted by HIGH, MEDIUM, LOW)
      - Recommended Actions (for merchandiser checklist)
      - Management Insights (for business analytics)
    """
    deductions: List[str] = []
    generated_issues: List[IssueDetected] = []
    recommended_actions: List[str] = []
    management_insights: List[str] = []

    # ──────────────────────────────────────────────────────────────────────────
    # 1. AVAILABILITY PILLAR (Max 35 points)
    # ──────────────────────────────────────────────────────────────────────────
    availability_score = 35.0
    active_expected = expected_skus if (expected_skus and len(expected_skus) > 0) else []

    # Match detected products against expected SKUs (when expected SKUs are specified)
    detected_names = [p.product_name.lower() for p in detected_products if p.product_name]
    missing_skus: List[str] = []

    if active_expected:
        for exp_sku in active_expected:
            exp_lower = exp_sku.lower()
            words = [w for w in exp_lower.split() if len(w) > 3]
            found = any(
                (exp_lower in det or (words and sum(1 for w in words if w in det) >= len(words) - 1))
                for det in detected_names
            )
            if not found:
                missing_skus.append(exp_sku)
    elif availability_alerts and availability_alerts.missing_expected_skus:
        missing_skus = [s for s in availability_alerts.missing_expected_skus if isinstance(s, str) and s.strip()]

    # Sync availability alerts missing list
    availability_alerts.missing_expected_skus = missing_skus

    # Deduct for missing expected SKUs (5 pts per missing SKU, up to 20 pts)
    if missing_skus:
        missing_penalty = min(len(missing_skus) * 5.0, 20.0)
        availability_score -= missing_penalty
        deductions.append(f"[-] -{missing_penalty:.0f} pts: {len(missing_skus)} Expected Assortment SKU(s) missing from shelf ({', '.join(missing_skus[:2])}{'...' if len(missing_skus) > 2 else ''})")
        
        for m_sku in missing_skus:
            generated_issues.append(
                IssueDetected(
                    issue=f"Expected product not visible: {m_sku}",
                    evidence="SKU is assigned to store planogram but zero facings detected on shelf",
                    severity="HIGH",
                    confidence="high",
                    recommended_action=f"Verify backroom stock for {m_sku} and restock assigned shelf space.",
                )
            )
            recommended_actions.append(f"Check backroom inventory for {m_sku} and replenish.")

    # Deduct for visible shelf gaps / voids (4 pts per gap, up to 15 pts)
    visible_gaps_count = len(availability_alerts.visible_gaps)
    if visible_gaps_count > 0:
        gap_penalty = min(visible_gaps_count * 4.0, 15.0)
        availability_score -= gap_penalty
        deductions.append(f"[-] -{gap_penalty:.0f} pts: {visible_gaps_count} empty shelf facing gap(s) detected")
        generated_issues.append(
            IssueDetected(
                issue=f"{visible_gaps_count} empty shelf space(s) detected",
                evidence="Empty shelf void visible with potential stockout risk",
                severity="HIGH" if visible_gaps_count >= 2 else "MEDIUM",
                confidence="high",
                recommended_action="Front-face existing products or replenish empty facings from inventory.",
            )
        )
        recommended_actions.append(f"Refill {visible_gaps_count} empty facing gap(s) where stock is available.")

    availability_score = max(0.0, min(35.0, availability_score))

    # ──────────────────────────────────────────────────────────────────────────
    # 2. PLANOGRAM & ARRANGEMENT PILLAR (Max 25 points)
    # ──────────────────────────────────────────────────────────────────────────
    planogram_score = 25.0
    arr_score = shelf_metrics.arrangement_score if shelf_metrics.arrangement_score is not None else 75.0
    if 0 < arr_score <= 10.0:
        arr_score = arr_score * 10.0

    # Scale arrangement (0-100 scale to 0-15 points)
    arrangement_pillar_val = (arr_score / 100.0) * 15.0
    arr_deduction = 15.0 - arrangement_pillar_val
    if arr_deduction > 2.0:
        deductions.append(f"[-] -{arr_deduction:.0f} pts: Product facings misaligned or grouping irregular")
        generated_issues.append(
            IssueDetected(
                issue="Shelf arrangement needs alignment",
                evidence=f"Shelf arrangement score assessed at {arr_score:.0f}/100",
                severity="MEDIUM" if arr_score < 70 else "LOW",
                confidence="medium",
                recommended_action="Align product labels forward and group same-brand variants together.",
            )
        )
        recommended_actions.append("Correct product facing alignment and group identical variants.")

    planogram_score -= arr_deduction

    # Golden Zone (Eye-Level) placement check (10 points)
    eye_level_pran = sum(p.facing_count or 1 for p in detected_products if (p.brand or "").upper() == "PRAN" and p.shelf_level == "eye_level")
    eye_level_comp = sum(p.facing_count or 1 for p in detected_products if (p.brand or "").upper() != "PRAN" and p.shelf_level == "eye_level")
    total_eye_level = eye_level_pran + eye_level_comp

    if total_eye_level > 0:
        pran_eye_ratio = (eye_level_pran / total_eye_level) * 100.0
        if pran_eye_ratio < 30.0:
            planogram_score -= 6.0
            deductions.append(f"[-] -6 pts: Poor eye-level placement (PRAN occupies only {pran_eye_ratio:.0f}% of prime Golden Zone)")
            generated_issues.append(
                IssueDetected(
                    issue="Competitor dominates prime eye-level shelf space",
                    evidence=f"PRAN has only {eye_level_pran} eye-level facings vs {eye_level_comp} competitor facings",
                    severity="MEDIUM",
                    confidence="high",
                    recommended_action="Negotiate shifting PRAN high-margin SKUs to eye-level shelf with retailer.",
                )
            )
            management_insights.append(f"PRAN has weak Golden Zone presence ({pran_eye_ratio:.0f}%) compared to competing brands.")
        else:
            management_insights.append(f"PRAN commands {pran_eye_ratio:.0f}% of prime eye-level shelf presence.")

    planogram_score = max(0.0, min(25.0, planogram_score))

    # ──────────────────────────────────────────────────────────────────────────
    # 3. SHARE OF SHELF (SOS) PILLAR (Max 20 points)
    # ──────────────────────────────────────────────────────────────────────────
    sos_score = 20.0
    actual_sos = shelf_metrics.pran_shelf_share_pct if shelf_metrics.pran_shelf_share_pct is not None else 0.0

    if actual_sos < target_pran_sos:
        sos_deficit = target_pran_sos - actual_sos
        penalty = min(sos_deficit * 0.5, 12.0)
        sos_score -= penalty
        deductions.append(f"[-] -{penalty:.0f} pts: Shelf share ({actual_sos:.1f}%) is below agreed target ({target_pran_sos:.0f}%)")
        generated_issues.append(
            IssueDetected(
                issue="PRAN shelf presence below target",
                evidence=f"Current PRAN shelf share is {actual_sos:.1f}% vs target {target_pran_sos:.0f}%",
                severity="MEDIUM" if sos_deficit > 15 else "LOW",
                confidence="high",
                recommended_action="Review agreed shelf allocation and seek additional facings for core products.",
            )
        )
        recommended_actions.append("Review agreed shelf allocation and negotiate additional facings.")
        management_insights.append(f"SOS deficit of {sos_deficit:.1f}% indicates headroom for additional facings.")
    else:
        management_insights.append(f"PRAN meets or exceeds shelf share target ({actual_sos:.1f}% vs {target_pran_sos:.0f}%).")

    sos_score = max(0.0, min(20.0, sos_score))

    # ──────────────────────────────────────────────────────────────────────────
    # 4. PRICING COMPLIANCE PILLAR (Max 10 points)
    # ──────────────────────────────────────────────────────────────────────────
    pricing_score = 10.0
    price_violations_count = 0

    if approved_prices and pricing_and_promotions:
        for p_item in pricing_and_promotions:
            if not p_item.product or p_item.visible_price is None:
                continue
            for app_name, app_price in approved_prices.items():
                if app_name.lower() in p_item.product.lower():
                    if abs(p_item.visible_price - app_price) > 0.05 * app_price:
                        price_violations_count += 1
                        p_item.pricing_violation = "confirmed"
                        deductions.append(f"[-] -5 pts: Price discrepancy on {p_item.product} (Tag: ৳{p_item.visible_price:.2f} vs Approved: ৳{app_price:.2f})")
                        generated_issues.append(
                            IssueDetected(
                                issue=f"Price violation detected for {p_item.product}",
                                evidence=f"Visible tag shows ৳{p_item.visible_price:.2f}, approved rate is ৳{app_price:.2f}",
                                severity="HIGH",
                                confidence="high",
                                recommended_action="Notify store management to replace incorrect shelf price label.",
                            )
                        )
                        recommended_actions.append(f"Correct shelf price tag for {p_item.product}.")

    pricing_score = max(0.0, pricing_score - (price_violations_count * 5.0))

    # ──────────────────────────────────────────────────────────────────────────
    # 5. PROMOTION & POSM EXECUTION PILLAR (Max 10 points)
    # ──────────────────────────────────────────────────────────────────────────
    promotion_score = 10.0
    has_promo = any(p.promotion_detected for p in pricing_and_promotions)

    if not has_promo:
        promotion_score -= 3.0
        deductions.append("[-] -3 pts: No visible promotional signage or shelf talkers detected")
        management_insights.append("Promotional visibility can be boosted with shelf talkers / POSM materials.")
    else:
        management_insights.append("Promotional materials / POSM present and active on shelf.")

    promotion_score = max(0.0, min(10.0, promotion_score))

    # ──────────────────────────────────────────────────────────────────────────
    # COMPOSITE SCORE CALCULATION
    # ──────────────────────────────────────────────────────────────────────────
    total_score = round(availability_score + planogram_score + sos_score + pricing_score + promotion_score, 1)

    if total_score >= 88.0:
        grade = "Excellent"
    elif total_score >= 75.0:
        grade = "Good"
    elif total_score >= 60.0:
        grade = "Needs Attention"
    else:
        grade = "Critical Action Required"

    # Sort issues: HIGH first, then MEDIUM, then LOW
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    generated_issues.sort(key=lambda x: severity_order.get(x.severity, 3))

    # Final checklist items
    # Deduplicate recommended actions
    seen = set()
    deduped_actions = []
    for act in recommended_actions:
        if act not in seen:
            seen.add(act)
            deduped_actions.append(act)

    res = RetailExecutionScore(
        score=total_score,
        grade=grade,
        availability_score=round(availability_score, 1),
        planogram_score=round(planogram_score, 1),
        sos_score=round(sos_score, 1),
        pricing_score=round(pricing_score, 1),
        promotion_score=round(promotion_score, 1),
        deductions=deductions,
    )

    return res, generated_issues, deduped_actions, management_insights
