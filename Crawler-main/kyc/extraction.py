"""Read customer details out of an existing KYC document (photo, PDF or pasted text) with Gemini.

The result only pre-fills the form: a person always checks it before anything is saved.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from django.conf import settings

from docscanner import services as scanner

from . import logistics
from .teams import is_yes, looks_like_purchaser, team_from_text

logger = logging.getLogger(__name__)

MAX_CONTACTS = 20

# Field name -> longest value the customer form accepts.
_CUSTOMER_FIELDS = {"name": 255, "address": 1000, "city": 120, "state": 120, "pincode": 20, "country": 120,
                    "gstin": 15, "pan": 10, "phone": 40, "website": 255}
_CONTACT_FIELDS = {"name": 200, "designation": 200, "email": 254, "phone": 40}

_PROMPT = (
    "You read customer KYC / customer registration forms for a manufacturing company.\n"
    "The input is a photo, a PDF or pasted text of one customer's form. Return ONLY valid JSON, no Markdown.\n"
    "Copy values exactly as written. Use null when a value is not there. Do not guess.\n\n"
    "{\n"
    '  "customer": {"name": "company name", "address": "street address without city/state/PIN", "city": null,\n'
    '               "state": null, "pincode": null, "country": null, "gstin": null, "pan": null,\n'
    '               "phone": "company phone", "website": null,\n'
    '               "transportation": "as written, e.g. VRL-TO PAY-DOOR", "preferred_transporter": null,\n'
    '               "delivery": "door delivery or go-down delivery, as written", "msme": "yes, no or null",\n'
    '               "udyam_number": null},\n'
    '  "contacts": [{"name": "person", "designation": null, "department": null, "email": null, "phone": null,\n'
    '                "is_purchaser": false}]\n'
    "}\n\n"
    "List every person on the form as a contact. `department` is the section of the form they are under,\n"
    "for example purchase, accounts, design, quality, management. Set is_purchaser to true only for the\n"
    "purchase / procurement contact.\n"
    'For msme answer "yes" or "no" only when one is clearly marked, ticked or circled. If the form just\n'
    'prints both choices (for example "YES / NO") without marking one, use null.'
)


def logistics_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Transport and MSME values as the customer form expects them, from what the KYC form says."""
    transportation = _clean(raw.get("transportation"), 150)
    preferred, other = logistics.transporter(raw.get("preferred_transporter"))
    if not preferred:  # not filled in: the transporter written under Transportation, else ARC
        preferred, other = logistics.transporter(transportation)
    msme = logistics.msme(raw.get("msme"))
    return {
        "transportation": transportation,
        "preferred_transporter": preferred or logistics.DEFAULT_TRANSPORTER,
        "other_transporter": other,
        "delivery_type": logistics.delivery(raw.get("delivery")) or logistics.delivery(transportation),
        "msme_applicable": "" if msme is None else ("yes" if msme else "no"),
        "udyam_aadhar_no": logistics.udyam(raw.get("udyam_number")),
    }


class KycExtractionError(RuntimeError):
    """The message is shown to the user."""


def _clean(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(r"[ \t]+", " ", str(value)).strip()[:limit]


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn whatever the model returned into {"customer": {...}, "contacts": [...]} with safe strings."""
    raw_customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    customer = {field: _clean(raw_customer.get(field), limit) for field, limit in _CUSTOMER_FIELDS.items()}
    for field in ("gstin", "pan"):  # remove the spaces before cutting to length, not after
        compact = re.sub(r"\s+", "", _clean(raw_customer.get(field), 100)).upper()
        customer[field] = compact[:_CUSTOMER_FIELDS[field]]
    customer.update(logistics_fields(raw_customer))

    contacts: List[Dict[str, Any]] = []
    raw_contacts = payload.get("contacts") if isinstance(payload.get("contacts"), list) else []
    for raw in raw_contacts[:MAX_CONTACTS]:
        if not isinstance(raw, dict):
            continue
        contact: Dict[str, Any] = {field: _clean(raw.get(field), limit) for field, limit in _CONTACT_FIELDS.items()}
        contact["email"] = contact["email"].lower()
        if not (contact["name"] or contact["email"] or contact["phone"]):
            continue
        if not contact["name"]:
            contact["name"] = contact["email"] or contact["phone"]
        department = _clean(raw.get("department"), 200)
        contact["category"] = team_from_text(department, contact["designation"])
        contact["is_purchaser"] = bool(contact["email"]) and (
            is_yes(raw.get("is_purchaser")) or looks_like_purchaser(department)
            or looks_like_purchaser(contact["designation"]))
        contacts.append(contact)
    return {"customer": customer, "contacts": contacts}


def extract_kyc(*, file_bytes: Optional[bytes] = None, content_type: str = "", filename: str = "",
                text: str = "") -> Dict[str, Any]:
    api_key = (getattr(settings, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key or scanner.genai is None:
        raise KycExtractionError("Automatic reading is not set up on this server (GEMINI_API_KEY is missing).")

    parts: List[Any] = [_PROMPT + f"\n\nFilename: {filename or 'none'}"]
    if file_bytes:
        parts.append({"mime_type": content_type or "image/jpeg", "data": file_bytes})
    if text:
        parts.append("Pasted text of the form:\n" + text)
    try:
        scanner.genai.configure(api_key=api_key)
        model = scanner.genai.GenerativeModel(getattr(settings, "GEMINI_MODEL", "") or "gemini-2.5-flash")
        response = model.generate_content(parts, generation_config={"temperature": 0.1})
        payload = scanner._parse_json_response(getattr(response, "text", ""))
    except Exception as exc:
        logger.exception("KYC document extraction failed")
        raise KycExtractionError("The document could not be read automatically.") from exc
    return normalize(payload)


def has_details(data: Dict[str, Any]) -> bool:
    customer = dict((data or {}).get("customer") or {})
    if customer.get("preferred_transporter") == logistics.DEFAULT_TRANSPORTER and not customer.get("transportation"):
        customer.pop("preferred_transporter")  # the default, not something read from the document
    return any(customer.values()) or bool((data or {}).get("contacts"))
