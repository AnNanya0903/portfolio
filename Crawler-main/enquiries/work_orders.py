"""“Converted to Work Order” on the Enquiry Board: who may mark an enquiry, and marking it."""
from __future__ import annotations

from typing import Iterable, Dict, Tuple

from django.conf import settings

from kyc.departments import departments_of
from magicwand.user_utils import get_user_primary_email

from .models import WorkOrder

# Departments (KYC) whose people may mark an enquiry as converted to a work order. Nobody else may, superusers
# included: on production that is customerservice@ and rabin@ (Sales) and apai@ (Top Management).
ALLOWED_DEPARTMENTS = frozenset({"Sales", "Top Management"})


# People who may mark an enquiry whatever their department. Change with WORK_ORDER_EXTRA_EMAILS (comma separated).
EXTRA_EMAILS = frozenset({"rabin@avigiri.com"})


def _extra_emails() -> frozenset:
    raw = getattr(settings, "WORK_ORDER_EXTRA_EMAILS", None)
    if raw is None:
        return EXTRA_EMAILS
    items = raw.split(",") if isinstance(raw, str) else raw
    return frozenset(str(e).strip().lower() for e in items if str(e).strip())


def can_convert(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    email = (get_user_primary_email(user) or "").strip().lower()
    if email and email in _extra_emails():
        return True
    return any(d.is_active and d.name in ALLOWED_DEPARTMENTS for d in departments_of(user))


def convert(enquiry_id: str, user) -> Tuple[bool, WorkOrder]:
    """Mark the enquiry as converted. Returns (True, new row) the first time, (False, existing row) after that."""
    return WorkOrder.objects.get_or_create(
        enquiry_id=str(enquiry_id), defaults={"converted_by": user if getattr(user, "is_authenticated", False) else None})


def converted_for(enquiry_ids: Iterable[str]) -> Dict[str, WorkOrder]:
    ids = [str(e) for e in enquiry_ids if e]
    if not ids:
        return {}
    return {row.enquiry_id: row for row in WorkOrder.objects.filter(enquiry_id__in=ids).select_related("converted_by")}
