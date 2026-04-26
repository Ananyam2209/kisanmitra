"""
KisanMitra — Distress Guardian
Risk Scoring Engine (FROS Component)

Computes a Farmer Risk Score (0–100) from three mandatory dimensions:
  1. Rainfall Deficit Score     (weight: 35%)
  2. MSP Gap Score              (weight: 35%)
  3. Debt-to-Income Ratio Score (weight: 30%)

Plus optional modifiers that amplify risk:
  - Cash crop flag
  - Marginal land (<2 acres)
  - Informal lender (moneylender vs bank)
"""

from dataclasses import dataclass, field
from typing import Literal
from datetime import date


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FarmerProfile:
    farmer_id: str
    name: str
    district: str
    state: str
    land_acres: float
    primary_crop: str                          # e.g. "cotton", "soybean", "rice"
    crop_category: Literal["cash", "food"]     # cash crops carry higher risk
    loan_amount_inr: float
    lender_type: Literal["bank", "cooperative", "moneylender"]
    annual_income_inr: float
    phone: str                                 # WhatsApp number (E.164 format)


@dataclass
class DistrictWeatherData:
    district: str
    normal_rainfall_mm: float      # long-period average for the season
    actual_rainfall_mm: float      # rainfall received so far this season
    season: Literal["kharif", "rabi", "zaid"]
    as_of_date: date


@dataclass
class MarketPriceData:
    crop: str
    msp_inr_per_quintal: float     # government Minimum Support Price
    mandi_price_inr_per_quintal: float   # actual local mandi price today
    district: str
    as_of_date: date


@dataclass
class RiskResult:
    farmer_id: str
    total_score: float             # 0–100 (higher = more at risk)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    rainfall_score: float
    msp_gap_score: float
    debt_score: float
    modifier_points: float
    breakdown: dict
    flags: list[str]
    computed_on: date = field(default_factory=date.today)


# ---------------------------------------------------------------------------
# Individual dimension scorers  (each returns 0–100)
# ---------------------------------------------------------------------------

def _rainfall_deficit_score(weather: DistrictWeatherData) -> tuple[float, str]:
    """
    Score based on how much rainfall has fallen vs normal.
    deficit_pct = (normal - actual) / normal * 100
      0–10%  deficit → score 0–15   (negligible)
     10–25%  deficit → score 15–40  (watch)
     25–50%  deficit → score 40–70  (stress)
     50%+    deficit → score 70–100 (crisis)
    Surplus rainfall (actual > normal) gives 0.
    """
    if weather.actual_rainfall_mm >= weather.normal_rainfall_mm:
        return 0.0, "Rainfall at or above normal — no deficit risk"

    deficit_pct = (
        (weather.normal_rainfall_mm - weather.actual_rainfall_mm)
        / weather.normal_rainfall_mm
        * 100
    )

    if deficit_pct <= 10:
        score = deficit_pct * 1.5
        note = f"Minor deficit {deficit_pct:.1f}% — low risk"
    elif deficit_pct <= 25:
        score = 15 + (deficit_pct - 10) * (25 / 15)
        note = f"Moderate deficit {deficit_pct:.1f}% — monitor closely"
    elif deficit_pct <= 50:
        score = 40 + (deficit_pct - 25) * (30 / 25)
        note = f"Significant deficit {deficit_pct:.1f}% — crop stress likely"
    else:
        score = min(70 + (deficit_pct - 50) * 0.6, 100)
        note = f"Severe deficit {deficit_pct:.1f}% — drought conditions"

    return round(score, 2), note


def _msp_gap_score(market: MarketPriceData) -> tuple[float, str]:
    """
    Score based on how far mandi price has fallen below MSP.
    gap_pct = (MSP - mandi) / MSP * 100
    Selling above MSP → 0 risk.
    Gap 0–10%  → score 0–20
    Gap 10–25% → score 20–55
    Gap 25–50% → score 55–85
    Gap 50%+   → score 85–100
    """
    gap = market.msp_inr_per_quintal - market.mandi_price_inr_per_quintal

    if gap <= 0:
        return 0.0, f"Mandi price ₹{market.mandi_price_inr_per_quintal} exceeds MSP ₹{market.msp_inr_per_quintal} — no gap"

    gap_pct = gap / market.msp_inr_per_quintal * 100

    if gap_pct <= 10:
        score = gap_pct * 2
        note = f"Minor price gap {gap_pct:.1f}% below MSP"
    elif gap_pct <= 25:
        score = 20 + (gap_pct - 10) * (35 / 15)
        note = f"Moderate price gap {gap_pct:.1f}% — income impact significant"
    elif gap_pct <= 50:
        score = 55 + (gap_pct - 25) * (30 / 25)
        note = f"Large price gap {gap_pct:.1f}% — severe income loss"
    else:
        score = min(85 + (gap_pct - 50) * 0.3, 100)
        note = f"Extreme price gap {gap_pct:.1f}% — distress sale territory"

    return round(score, 2), note


def _debt_to_income_score(farmer: FarmerProfile) -> tuple[float, str]:
    """
    Debt-to-Income ratio: loan / annual_income
    < 0.5x  → score 0–15   (manageable)
    0.5–1x  → score 15–40  (watch)
    1–2x    → score 40–70  (stressed)
    2–3x    → score 70–85  (severe)
    > 3x    → score 85–100 (crisis)
    """
    if farmer.annual_income_inr <= 0:
        return 100.0, "Income recorded as zero — immediate crisis flag"

    dti = farmer.loan_amount_inr / farmer.annual_income_inr

    if dti < 0.5:
        score = dti * 30
        note = f"DTI {dti:.2f}x — debt manageable"
    elif dti < 1.0:
        score = 15 + (dti - 0.5) * 50
        note = f"DTI {dti:.2f}x — moderate debt burden"
    elif dti < 2.0:
        score = 40 + (dti - 1.0) * 30
        note = f"DTI {dti:.2f}x — high debt burden"
    elif dti < 3.0:
        score = 70 + (dti - 2.0) * 15
        note = f"DTI {dti:.2f}x — severe debt burden"
    else:
        score = min(85 + (dti - 3.0) * 5, 100)
        note = f"DTI {dti:.2f}x — crisis-level debt"

    return round(score, 2), note


# ---------------------------------------------------------------------------
# Risk modifiers  (additive penalty points, capped)
# ---------------------------------------------------------------------------

def _compute_modifiers(farmer: FarmerProfile) -> tuple[float, list[str]]:
    """
    Three binary modifiers that amplify underlying risk.
    Each adds up to 5 points. Max combined modifier: 15 points.
    """
    points = 0.0
    flags = []

    if farmer.crop_category == "cash":
        points += 5
        flags.append(f"CASH_CROP: {farmer.primary_crop} — price volatility risk")

    if farmer.land_acres < 2.0:
        points += 5
        flags.append(f"MARGINAL_LAND: {farmer.land_acres:.1f} acres — limited buffer")

    if farmer.lender_type == "moneylender":
        points += 5
        flags.append("INFORMAL_DEBT: Moneylender loan — high interest rate risk")

    return min(points, 15), flags


# ---------------------------------------------------------------------------
# Severity classifier
# ---------------------------------------------------------------------------

def _classify_severity(score: float) -> Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
    if score < 30:
        return "LOW"
    elif score < 50:
        return "MEDIUM"
    elif score < 70:
        return "HIGH"
    else:
        return "CRITICAL"


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def compute_farmer_risk_score(
    farmer: FarmerProfile,
    weather: DistrictWeatherData,
    market: MarketPriceData,
) -> RiskResult:
    """
    Compute the composite Farmer Risk Score (FROS) for a single farmer.

    Returns a RiskResult with full breakdown and severity classification.
    """
    # --- Dimension scores (0–100 each) ---
    rainfall_score, rainfall_note = _rainfall_deficit_score(weather)
    msp_score, msp_note = _msp_gap_score(market)
    debt_score, debt_note = _debt_to_income_score(farmer)

    # --- Weighted composite (before modifiers) ---
    weighted = (
        rainfall_score * 0.35
        + msp_score    * 0.35
        + debt_score   * 0.30
    )

    # --- Modifiers ---
    modifier_pts, flags = _compute_modifiers(farmer)

    # --- Final score, capped at 100 ---
    total = min(weighted + modifier_pts, 100.0)
    total = round(total, 1)

    severity = _classify_severity(total)

    breakdown = {
        "rainfall": {
            "raw_score": rainfall_score,
            "weight": 0.35,
            "weighted": round(rainfall_score * 0.35, 2),
            "note": rainfall_note,
        },
        "msp_gap": {
            "raw_score": msp_score,
            "weight": 0.35,
            "weighted": round(msp_score * 0.35, 2),
            "note": msp_note,
        },
        "debt": {
            "raw_score": debt_score,
            "weight": 0.30,
            "weighted": round(debt_score * 0.30, 2),
            "note": debt_note,
        },
        "modifiers": {
            "points_added": modifier_pts,
            "flags": flags,
        },
        "weighted_base": round(weighted, 2),
        "final_score": total,
        "severity": severity,
    }

    return RiskResult(
        farmer_id=farmer.farmer_id,
        total_score=total,
        severity=severity,
        rainfall_score=rainfall_score,
        msp_gap_score=msp_score,
        debt_score=debt_score,
        modifier_points=modifier_pts,
        breakdown=breakdown,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Quick demo / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    farmer = FarmerProfile(
        farmer_id="MH-WD-001",
        name="Rajesh Patil",
        district="Wardha",
        state="Maharashtra",
        land_acres=2.3,
        primary_crop="cotton",
        crop_category="cash",
        loan_amount_inr=120_000,
        lender_type="moneylender",
        annual_income_inr=48_000,
        phone="+919876543210",
    )

    weather = DistrictWeatherData(
        district="Wardha",
        normal_rainfall_mm=820,
        actual_rainfall_mm=490,   # 40% deficit — significant stress
        season="kharif",
        as_of_date=date(2026, 8, 15),
    )

    market = MarketPriceData(
        crop="cotton",
        msp_inr_per_quintal=6620,
        mandi_price_inr_per_quintal=5200,   # ~21% below MSP
        district="Wardha",
        as_of_date=date(2026, 8, 15),
    )

    result = compute_farmer_risk_score(farmer, weather, market)

    print(f"\n{'='*55}")
    print(f"  KisanMitra — Distress Guardian Risk Score")
    print(f"{'='*55}")
    print(f"  Farmer  : {farmer.name} ({farmer.farmer_id})")
    print(f"  District: {farmer.district}, {farmer.state}")
    print(f"  Score   : {result.total_score} / 100")
    print(f"  Severity: {result.severity}")
    print(f"\n  Score Breakdown:")
    for dim in ["rainfall", "msp_gap", "debt"]:
        b = result.breakdown[dim]
        print(f"    {dim:<12} raw={b['raw_score']:>5.1f}  weighted={b['weighted']:>5.2f}  — {b['note']}")
    print(f"\n  Modifier Flags:")
    for f_ in result.flags:
        print(f"    ⚠  {f_}")
    print(f"\n  Base (weighted): {result.breakdown['weighted_base']}")
    print(f"  Modifier pts   : +{result.modifier_points}")
    print(f"  FINAL SCORE    : {result.total_score}")
    print(f"{'='*55}\n")

    if result.total_score >= 70:
        print("  🚨  ALERT THRESHOLD BREACHED — escalate to agricultural officer")
    print()
