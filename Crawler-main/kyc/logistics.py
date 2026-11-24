"""Transport and MSME details of a KYC customer, read the same way from the form, a KYC document and a sheet.

The KYC form has:
  Transportation         free text, e.g. "VRL-TO PAY-DOOR" (transporter, freight terms, delivery)
  Preferred Transporter  ARC (default), VRL or another transporter
  Door / Go-Down Delivery
  MSME applicable        YES / NO
  Udyam Aadhaar No.      needed when MSME applies
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

TRANSPORTER_SUGGESTIONS = ("ARC", "VRL")  # offered while typing Transportation; anything else can be typed
DEFAULT_TRANSPORTER = "ARC"

_UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def transporter(value) -> Tuple[str, str]:
    """(preferred_transporter, other_transporter) for a written transporter: ARC, VRL or OTHER + its name."""
    text = _text(value)
    if not text:
        return "", ""
    words = set(re.findall(r"[A-Za-z]+", text.upper()))
    if "ARC" in words:
        return "ARC", ""
    if "VRL" in words:
        return "VRL", ""
    if text.upper() in ("OTHER", "OTHERS"):
        return "OTHER", ""
    return "OTHER", text[:100]


def delivery(value) -> str:
    """DOOR, GODOWN or '' for text such as "Door delivery", "Go-Down Delivery" or "VRL-TO PAY-DOOR"."""
    text = _text(value).upper()
    if not text:
        return ""
    if re.search(r"GO\s*-?\s*DOWN|GODOWN|GODWN", text):
        return "GODOWN"
    if re.search(r"\bDOOR\b", text):
        return "DOOR"
    return ""


def msme(value) -> Optional[bool]:
    """True / False when the form says Yes or No; None when it is blank or both are printed, as in "YES / NO"."""
    if isinstance(value, bool):
        return value
    text = _text(value).upper()
    if not text:
        return None
    has_yes = bool(re.search(r"\b(YES|Y|APPLICABLE|TRUE)\b", text)) and "NOT APPLICABLE" not in text
    has_no = bool(re.search(r"\b(NO|N|NOT APPLICABLE|NA|N/A|FALSE)\b", text))
    if has_yes and has_no:
        return None
    if has_yes:
        return True
    if has_no:
        return False
    return None


def udyam(value) -> str:
    """Udyam number in capitals without spaces, e.g. UDYAM-TN-12-0012345."""
    return re.sub(r"\s+", "", str(value or "")).upper()[:50]


def udyam_looks_wrong(number: str) -> bool:
    """A number written in the UDYAM-XX-00-0000000 style that does not match it (older numbers are allowed)."""
    return number.startswith("UDYAM") and not _UDYAM_RE.match(number)
