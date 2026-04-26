"""
KisanMitra — Distress Guardian
Agent using latest LangChain + LangGraph + Google Gemini
"""

import os
import json
from datetime import date

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.tools import tool
from langgraph.prebuilt import create_react_agent

from risk_scorer import (
    FarmerProfile,
    DistrictWeatherData,
    MarketPriceData,
    compute_farmer_risk_score,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Mock data (replace with real IMD / eNAM API later)
# ---------------------------------------------------------------------------

def _fetch_weather(district: str) -> dict:
    return {
        "district": district,
        "normal_rainfall_mm": 820,
        "actual_rainfall_mm": 490,
        "season": "kharif",
        "as_of_date": str(date.today()),
    }

def _fetch_price(crop: str, district: str) -> dict:
    msp   = {"cotton": 6620, "soybean": 4600, "rice": 2300, "wheat": 2275}
    mandi = {"cotton": 5200, "soybean": 3900, "rice": 2100, "wheat": 2400}
    return {
        "crop": crop,
        "msp_inr_per_quintal": msp.get(crop.lower(), 4000),
        "mandi_price_inr_per_quintal": mandi.get(crop.lower(), 3500),
        "district": district,
        "as_of_date": str(date.today()),
    }

SCHEMES = [
    {"name": "PMFBY",    "benefit": "Crop insurance for yield loss",          "url": "https://pmfby.gov.in",    "crops": ["cotton","soybean","rice","wheat"]},
    {"name": "PM-KISAN", "benefit": "₹6,000/year direct income support",      "url": "https://pmkisan.gov.in",  "crops": ["all"]},
    {"name": "KCC",      "benefit": "Credit at 4% vs 36% moneylender rate",   "url": "https://nabard.org",      "crops": ["all"]},
]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def get_weather_data(district: str) -> str:
    """Get rainfall data for a district. Input: district name e.g. Wardha"""
    return json.dumps(_fetch_weather(district))

@tool
def get_mandi_price(crop: str, district: str) -> str:
    """Get mandi price vs MSP for a crop in a district."""
    return json.dumps(_fetch_price(crop, district))

@tool
def compute_risk_score(
    farmer_id: str, name: str, district: str, state: str,
    land_acres: float, primary_crop: str, crop_category: str,
    loan_amount_inr: float, lender_type: str, annual_income_inr: float,
    phone: str, normal_rainfall_mm: float, actual_rainfall_mm: float,
    season: str, msp_inr_per_quintal: float, mandi_price_inr_per_quintal: float
) -> str:
    """Compute the Farmer Risk Score (0-100). Provide all farmer and weather/price fields directly."""
    today = str(date.today())
    farmer = FarmerProfile(
        farmer_id=farmer_id, name=name, district=district, state=state,
        land_acres=land_acres, primary_crop=primary_crop, crop_category=crop_category,
        loan_amount_inr=loan_amount_inr, lender_type=lender_type,
        annual_income_inr=annual_income_inr, phone=phone,
    )
    weather = DistrictWeatherData(
        district=district, normal_rainfall_mm=normal_rainfall_mm,
        actual_rainfall_mm=actual_rainfall_mm, season=season,
        as_of_date=date.fromisoformat(today),
    )
    market = MarketPriceData(
        crop=primary_crop, msp_inr_per_quintal=msp_inr_per_quintal,
        mandi_price_inr_per_quintal=mandi_price_inr_per_quintal,
        district=district, as_of_date=date.fromisoformat(today),
    )
    r = compute_farmer_risk_score(farmer, weather, market)
    return json.dumps({
        "total_score": r.total_score,
        "severity": r.severity,
        "flags": r.flags,
        "rainfall_score": r.rainfall_score,
        "msp_gap_score": r.msp_gap_score,
        "debt_score": r.debt_score,
    })

@tool
def find_eligible_schemes(crop: str) -> str:
    """Find government schemes the farmer qualifies for. Input: crop name."""
    return json.dumps([s for s in SCHEMES if "all" in s["crops"] or crop.lower() in s["crops"]])

@tool
def check_alert_threshold(score: float) -> str:
    """Check if risk score crosses alert threshold of 70. Input: numeric score."""
    if float(score) >= 70:
        return json.dumps({"alert": True,  "message": f"🚨 Score {score} >= 70. ESCALATE to agricultural officer NOW."})
    return json.dumps({"alert": False, "message": f"Score {score} is below 70. Continue monitoring."})


# ---------------------------------------------------------------------------
# Build and run
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        temperature=0,
        google_api_key=os.getenv("GOOGLE_API_KEY"),
    )

    tools = [get_weather_data, get_mandi_price, compute_risk_score,
             find_eligible_schemes, check_alert_threshold]

    agent = create_react_agent(llm, tools)

    print("\nStarting Distress Guardian assessment for Rajesh Patil...\n")

    result = agent.invoke({
        "messages": [{
            "role": "user",
            "content": """You are KisanMitra's Distress Guardian agent. Assess this farmer:

Farmer: Rajesh Patil (MH-WD-001)
District: Wardha, State: Maharashtra
Crop: cotton (cash crop), Land: 2.3 acres
Loan: 120000 INR from moneylender, Income: 48000 INR/year
Phone: +919876543210

Do these steps in order:
1. Call get_weather_data with district='Wardha'
2. Call get_mandi_price with crop='cotton', district='Wardha'
3. Call compute_risk_score with all the farmer details plus the numbers from steps 1 and 2
4. Call find_eligible_schemes with crop='cotton'
5. Call check_alert_threshold with the score from step 3
6. Write a final summary with: score, severity, risk reasons, schemes to apply for, and whether officer alert is needed."""
        }]
    })

    print("\n" + "="*60)
    print("  DISTRESS GUARDIAN — FINAL ASSESSMENT")
    print("="*60)
    # Extract the last message content
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content:
            print(msg.content)
            break
    print("="*60)
