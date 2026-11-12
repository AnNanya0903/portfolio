"""Several customer email addresses per enquiry.

EnquiryContact.customer_email is the main contact (the To address). Any further customer addresses are kept in
EnquiryContact.other_customer_emails and are CC'd on every email to the customer: manual and automatic
follow-ups, automatic customer emails, and Marketing campaigns.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import EnquiryContact

MAX_OTHER = 5  # besides the main email


def parse(values: Iterable[str], *, main: str = "") -> Tuple[List[str], str]:
    """Clean addresses typed in the form (several boxes, or commas in one box). Returns (addresses, error)."""
    main = (main or "").strip().lower()
    out: List[str] = []
    for value in values or []:
        for part in str(value or "").replace(";", ",").split(","):
            address = part.strip().lower()
            if not address:
                continue
            try:
                validate_email(address)
            except ValidationError:
                return [], f"“{part.strip()}” is not a valid email address."
            if address != main and address not in out:
                out.append(address)
    if len(out) > MAX_OTHER:
        return [], f"Please add at most {MAX_OTHER} other customer emails."
    return out, ""


def split(stored: str) -> List[str]:
    return [a.strip().lower() for a in str(stored or "").split(",") if a.strip()]


def others_for(enquiry_id: str) -> List[str]:
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("other_customer_emails").first()
    return split(contact.other_customer_emails) if contact else []


def save_others(enquiry_id: Optional[str], addresses: List[str], user=None) -> None:
    if not enquiry_id:
        return
    value = ", ".join(addresses)
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()
    if contact is None:
        if not value:
            return
        contact = EnquiryContact(enquiry_id=str(enquiry_id))
    if contact.other_customer_emails == value and contact.pk:
        return
    contact.other_customer_emails = value
    contact.updated_by = user if getattr(user, "is_authenticated", False) else None
    contact.save()


def with_customer_cc(enquiry_id: str, cc: Iterable[str], *, to: str = "", own: Iterable[str] = (),
                     limit: int = 10) -> List[str]:
    """The enquiry's other customer emails first, then the given CC; without the To address, our own
    addresses and duplicates, and at most `limit` addresses."""
    skip = {(to or "").strip().lower()} | {a.strip().lower() for a in own or []}
    out: List[str] = []
    for address in list(others_for(enquiry_id)) + [str(a).strip().lower() for a in cc or []]:
        if address and address not in skip and address not in out:
            out.append(address)
    return out[:limit]
