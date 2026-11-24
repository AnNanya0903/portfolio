"""Work out which team a contact belongs to from free text (a designation, a department, a sheet column)."""
from __future__ import annotations

import re

from .models import CustomerContact

_VALID = {key for key, _ in CustomerContact.CATEGORY_CHOICES}

# Checked in this order: "Director - Purchase" is top management, "Accounts Manager" is accounts.
_TEAM_PATTERNS = (
    (CustomerContact.CATEGORY_MANAGEMENT,
     re.compile(r"management|director|owner|proprietor|partner|chairman|president|founder|\b(md|ceo|cfo|coo|cto|vp)\b")),
    (CustomerContact.CATEGORY_ACCOUNTS, re.compile(r"account|finance|payment|billing")),
    (CustomerContact.CATEGORY_TECHNICAL,
     re.compile(r"technical|design|engineer|quality|r&d|development|production|\b(qa|qc)\b")),
    (CustomerContact.CATEGORY_SALES, re.compile(r"sales|marketing|purchas|procure|buyer|commercial|sourcing|stores")),
)
_PURCHASER = re.compile(r"purchas|procure|buyer|sourcing")
_YES = {"yes", "y", "true", "1", "x", "purchaser"}


def team_from_text(*values: str, default: str = CustomerContact.CATEGORY_SALES) -> str:
    """The team for the first value that says something recognisable; `default` when none does."""
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        key = re.sub(r"[^a-z]+", "_", text).strip("_")
        if key in _VALID:
            return key
        for team, pattern in _TEAM_PATTERNS:
            if pattern.search(text):
                return team
    return default


def looks_like_purchaser(designation: str) -> bool:
    return bool(_PURCHASER.search(str(designation or "").lower()))


def is_yes(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _YES
