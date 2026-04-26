"""
KisanMitra — Distress Guardian
WhatsApp Alert Flow (Twilio)

Handles three alert types:
  1. Farmer alert    — sent to the farmer in their regional language
  2. Officer alert   — sent to the block agricultural officer
  3. NGO alert       — sent to registered NGO/KVK volunteers in the district

Message templates follow WhatsApp Business API approved format.

Dependencies:
    pip install twilio python-dotenv
"""

import os
import logging
from dataclasses import dataclass
from typing import Literal
from datetime import datetime

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # Twilio sandbox default

# Officer registry — in production this comes from the DB keyed by district
OFFICER_REGISTRY: dict[str, str] = {
    "Wardha":   "+919000000001",
    "Yavatmal": "+919000000002",
    "Amravati": "+919000000003",
}

NGO_REGISTRY: dict[str, list[str]] = {
    "Wardha": ["+919000000010", "+919000000011"],
}


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

# English template (translate at runtime using Google Translate API or pre-translated strings)
FARMER_ALERT_EN = """🚨 *KisanMitra Alert — Your Risk Score is HIGH*

Namaste {farmer_name},

Your current financial risk score is *{score}/100* (Severity: {severity}).

Key concerns:
{concern_bullets}

✅ *You qualify for these schemes:*
{scheme_list}

👨‍💼 Your Agricultural Officer has been informed.
📞 Call KVK helpline: 1800-180-1551 (free)

— KisanMitra Guardian System"""

MARATHI_FARMER_ALERT = """🚨 *किसानमित्र सूचना — आपला धोका उच्च आहे*

नमस्कार {farmer_name},

आपचा सध्याचा आर्थिक धोका स्कोर *{score}/100* आहे (तीव्रता: {severity}).

मुख्य चिंता:
{concern_bullets}

✅ *आपण या योजनांसाठी पात्र आहात:*
{scheme_list}

👨‍💼 आपल्या कृषी अधिकाऱ्यांना माहिती दिली आहे.
📞 KVK हेल्पलाइन: 1800-180-1551 (मोफत)

— किसानमित्र गार्डियन प्रणाली"""

OFFICER_ALERT_EN = """🔴 *KisanMitra — High-Risk Farmer Alert*

Officer {officer_name},

A farmer in your block has crossed the critical risk threshold and requires immediate attention.

👤 Farmer: {farmer_name}
📍 District: {district}
📊 Risk Score: *{score}/100* — {severity}
📱 Contact: {farmer_phone}

Risk Factors:
{risk_flags}

Recommended Actions:
1. Contact farmer within 24 hours
2. Verify PMFBY enrollment status
3. Facilitate emergency KCC application if needed
4. Connect to nearest KVK for agronomic support

Automated by KisanMitra — {timestamp}"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AlertPayload:
    farmer_id: str
    farmer_name: str
    farmer_phone: str          # E.164, e.g. +919876543210
    district: str
    state: str
    risk_score: float
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    risk_flags: list[str]
    eligible_schemes: list[dict]
    language: Literal["en", "mr", "hi", "te", "kn", "ta"] = "mr"


@dataclass
class AlertResult:
    farmer_id: str
    farmer_message_sid: str | None
    officer_message_sids: list[str]
    ngo_message_sids: list[str]
    success: bool
    errors: list[str]


# ---------------------------------------------------------------------------
# Helper formatters
# ---------------------------------------------------------------------------

def _format_concern_bullets(flags: list[str]) -> str:
    return "\n".join(f"• {f}" for f in flags) if flags else "• General financial stress indicators"


def _format_scheme_list(schemes: list[dict]) -> str:
    lines = []
    for s in schemes[:3]:   # cap at 3 to keep message concise
        lines.append(f"• *{s['name']}* — {s['benefit']}\n  Apply: {s['apply_url']}")
    return "\n".join(lines) if lines else "• Check pmkisan.gov.in for available schemes"


def _select_farmer_template(language: str) -> str:
    return MARATHI_FARMER_ALERT if language == "mr" else FARMER_ALERT_EN


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def _send_whatsapp(client: Client, to: str, body: str) -> tuple[str | None, str | None]:
    """
    Send a single WhatsApp message.
    Returns (message_sid, error_string).
    """
    try:
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{to}",
            body=body,
        )
        logger.info(f"  ✓ Sent to {to} — SID: {msg.sid}")
        return msg.sid, None
    except TwilioRestException as e:
        logger.error(f"  ✗ Failed to send to {to}: {e}")
        return None, str(e)


# ---------------------------------------------------------------------------
# Public alert functions
# ---------------------------------------------------------------------------

def send_farmer_alert(client: Client, payload: AlertPayload) -> tuple[str | None, str | None]:
    """Send a risk alert to the farmer in their preferred language."""
    template = _select_farmer_template(payload.language)
    body = template.format(
        farmer_name=payload.farmer_name,
        score=int(payload.risk_score),
        severity=payload.severity,
        concern_bullets=_format_concern_bullets(payload.risk_flags),
        scheme_list=_format_scheme_list(payload.eligible_schemes),
    )
    logger.info(f"Sending farmer alert to {payload.farmer_phone}...")
    return _send_whatsapp(client, payload.farmer_phone, body)


def send_officer_alert(client: Client, payload: AlertPayload) -> list[tuple[str | None, str | None]]:
    """Send escalation alert to the block agricultural officer for this district."""
    officer_phone = OFFICER_REGISTRY.get(payload.district)
    if not officer_phone:
        logger.warning(f"No officer registered for district: {payload.district}")
        return []

    body = OFFICER_ALERT_EN.format(
        officer_name="Officer",   # enrich from officer DB in production
        farmer_name=payload.farmer_name,
        district=payload.district,
        score=int(payload.risk_score),
        severity=payload.severity,
        farmer_phone=payload.farmer_phone,
        risk_flags=_format_concern_bullets(payload.risk_flags),
        timestamp=datetime.now().strftime("%d %b %Y, %H:%M IST"),
    )
    logger.info(f"Sending officer alert to {officer_phone}...")
    return [_send_whatsapp(client, officer_phone, body)]


def send_ngo_alerts(client: Client, payload: AlertPayload) -> list[tuple[str | None, str | None]]:
    """Send notification to NGO/KVK volunteers registered in this district."""
    phones = NGO_REGISTRY.get(payload.district, [])
    results = []
    for phone in phones:
        body = (
            f"📢 KisanMitra Alert: Farmer {payload.farmer_name} in {payload.district} "
            f"has risk score {int(payload.risk_score)} ({payload.severity}). "
            f"Please reach out at {payload.farmer_phone}."
        )
        results.append(_send_whatsapp(client, phone, body))
    return results


# ---------------------------------------------------------------------------
# Orchestrator — call this from the agent
# ---------------------------------------------------------------------------

def trigger_distress_alerts(payload: AlertPayload) -> AlertResult:
    """
    Main entry point. Sends all required alerts based on severity.
    - MEDIUM+: Farmer alert only
    - HIGH+:   Farmer + Officer alert
    - CRITICAL: Farmer + Officer + NGO alerts
    """
    errors: list[str] = []
    farmer_sid = None
    officer_sids: list[str] = []
    ngo_sids: list[str] = []

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error("Twilio credentials not configured — set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")
        return AlertResult(
            farmer_id=payload.farmer_id,
            farmer_message_sid=None,
            officer_message_sids=[],
            ngo_message_sids=[],
            success=False,
            errors=["Twilio credentials missing"],
        )

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # Always alert farmer if severity is MEDIUM or above
    if payload.severity in ("MEDIUM", "HIGH", "CRITICAL"):
        sid, err = send_farmer_alert(client, payload)
        farmer_sid = sid
        if err:
            errors.append(f"Farmer alert: {err}")

    # Alert officer if HIGH or CRITICAL
    if payload.severity in ("HIGH", "CRITICAL"):
        results = send_officer_alert(client, payload)
        for sid, err in results:
            if sid:
                officer_sids.append(sid)
            if err:
                errors.append(f"Officer alert: {err}")

    # Alert NGOs if CRITICAL
    if payload.severity == "CRITICAL":
        results = send_ngo_alerts(client, payload)
        for sid, err in results:
            if sid:
                ngo_sids.append(sid)
            if err:
                errors.append(f"NGO alert: {err}")

    return AlertResult(
        farmer_id=payload.farmer_id,
        farmer_message_sid=farmer_sid,
        officer_message_sids=officer_sids,
        ngo_message_sids=ngo_sids,
        success=len(errors) == 0,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Demo (dry run — logs what WOULD be sent without hitting Twilio)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.DEBUG)

    sample_payload = AlertPayload(
        farmer_id="MH-WD-001",
        farmer_name="Rajesh Patil",
        farmer_phone="+919876543210",
        district="Wardha",
        state="Maharashtra",
        risk_score=78.5,
        severity="HIGH",
        risk_flags=[
            "CASH_CROP: cotton — price volatility risk",
            "MARGINAL_LAND: 2.3 acres — limited buffer",
            "INFORMAL_DEBT: Moneylender loan — high interest rate risk",
        ],
        eligible_schemes=[
            {"name": "PMFBY", "benefit": "Crop insurance", "apply_url": "https://pmfby.gov.in"},
            {"name": "KCC", "benefit": "Credit at 4% interest", "apply_url": "https://nabard.org"},
            {"name": "PM-KISAN", "benefit": "₹6,000/year support", "apply_url": "https://pmkisan.gov.in"},
        ],
        language="mr",
    )

    print("\n📋 DRY RUN — Message Previews\n")
    print("--- FARMER MESSAGE (Marathi) ---")
    template = _select_farmer_template("mr")
    print(template.format(
        farmer_name=sample_payload.farmer_name,
        score=int(sample_payload.risk_score),
        severity=sample_payload.severity,
        concern_bullets=_format_concern_bullets(sample_payload.risk_flags),
        scheme_list=_format_scheme_list(sample_payload.eligible_schemes),
    ))

    print("\n--- OFFICER MESSAGE ---")
    print(OFFICER_ALERT_EN.format(
        officer_name="Officer",
        farmer_name=sample_payload.farmer_name,
        district=sample_payload.district,
        score=int(sample_payload.risk_score),
        severity=sample_payload.severity,
        farmer_phone=sample_payload.farmer_phone,
        risk_flags=_format_concern_bullets(sample_payload.risk_flags),
        timestamp=datetime.now().strftime("%d %b %Y, %H:%M IST"),
    ))

    print("\n✅ Dry run complete. Set Twilio env vars and call trigger_distress_alerts() to go live.")
