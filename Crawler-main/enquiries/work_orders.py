"""“Converted to Work Order” on the Enquiry Board: who may mark an enquiry, and marking it."""
from __future__ import annotations

from typing import Iterable, Dict, Tuple

from kyc.departments import departments_of

from .models import WorkOrder

# Departments (KYC) whose people may mark an enquiry as converted to a work order. Superusers may too.
ALLOWED_DEPARTMENTS = frozenset({"Sales", "Top Management"})


def can_convert(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
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
