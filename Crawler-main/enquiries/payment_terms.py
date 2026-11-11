"""Credit period for accepted orders and CC addresses for follow-ups."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import List, Optional, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from .models import EnquiryContact

MAX_CREDIT_DAYS = 365
MAX_CC = 10


def credit_due_date(contact: Optional[EnquiryContact]) -> Optional[date]:
    """Payment due date: end of the credit period if one is set, otherwise the entered due date."""
    if contact is None:
        return None
    if contact.credit_period_days is not None and contact.credit_starts_on:
        return contact.credit_starts_on + timedelta(days=contact.credit_period_days)
    return contact.payment_due_on


def credit_hold_until(enquiry_id: str, today: Optional[date] = None) -> Optional[date]:
    """If the order is still inside its credit period, the date payment becomes due; otherwise None.

    Payment reminders are not sent before this date.
    """
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()
    if contact is None or not contact.credit_period_days or contact.payment_status == EnquiryContact.PAYMENT_PAID:
        return None
    due = credit_due_date(contact)
    today = today or timezone.localdate()
    return due if due and due > today else None


def payment_terms_for(enquiry_id: str) -> Tuple[Optional[date], Optional[int]]:
    """(due date, credit days) for the payment reminder text; (None, None) when nothing is set."""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()
    if contact is None:
        return None, None
    return credit_due_date(contact), contact.credit_period_days or None


def parse_cc(raw: str, *, exclude: str = "") -> Tuple[List[str], str]:
    """Split a CC field ("a@x.com, b@y.com") into valid unique addresses. Returns (addresses, error)."""
    seen, out = set(), []
    exclude = (exclude or "").strip().lower()
    for part in re.split(r"[,;\s]+", raw or ""):
        address = part.strip().strip("<>").lower()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError:
            return [], f"“{part.strip()}” is not a valid CC email address."
        if address == exclude or address in seen:
            continue
        seen.add(address)
        out.append(address)
    if len(out) > MAX_CC:
        return [], f"Please use at most {MAX_CC} CC addresses."
    return out, ""


def default_cc(enquiry_id: str) -> str:
    """CC to prefill on the compose page: what this enquiry used last, else FOLLOW_UP_DEFAULT_CC."""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("follow_up_cc").first()
    if contact and contact.follow_up_cc:
        return contact.follow_up_cc
    addresses, _ = parse_cc(getattr(settings, "FOLLOW_UP_DEFAULT_CC", "") or "")
    return ", ".join(addresses)
