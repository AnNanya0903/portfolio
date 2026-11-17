"""Departments: the seed data, which department a user is in, who may see PAN, and the audit trail."""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from django.db import transaction

from magicwand.access import is_super_admin
from magicwand.user_utils import get_user_primary_email

from .models import Department, DepartmentContact, KycAuditLog, UserDepartment

# (name, emails). The first email is the primary contact. One address may be in several departments.
DEPARTMENT_SEED = (
    ("Sales", ("customerservice@avigiri.com", "rabin@avigiri.com")),
    ("Marketing", ("kumar@avigiri.com", "manju@avigiri.com", "apai@avigiri.com")),
    ("Accounts", ("accounts1@avigiri.com", "accounts@avigiri.com")),
    ("Technical/Design", ("design@avigiri.com", "sagar@avigiri.com")),
    ("Top Management", ("apai@avigiri.com",)),
)

# Every department sees every customer's details, PAN included. Set this to True to mask PAN (ABCXX1234F)
# for everyone except the departments in FULL_PAN_DEPARTMENTS (and administrators).
MASK_PAN = False
FULL_PAN_DEPARTMENTS = frozenset({"Accounts", "Top Management"})


def seed_departments() -> dict:
    """Create the departments and their emails; safe to run again (nothing is duplicated or overwritten).

    A department or address that is already there is left as it is (a deactivated department stays off, a primary
    chosen on the admin page stays), but a seed address that was removed is added back.
    """
    created_departments = created_emails = 0
    with transaction.atomic():
        for order, (name, emails) in enumerate(DEPARTMENT_SEED, start=1):
            department, made = Department.objects.get_or_create(name=name, defaults={"sort_order": order})
            created_departments += made
            has_primary = department.contacts.filter(is_primary=True).exists()
            for index, email in enumerate(emails):
                _, made = DepartmentContact.objects.get_or_create(
                    department=department, email=email,
                    defaults={"is_primary": index == 0 and not has_primary})
                created_emails += made
    return {"departments": created_departments, "emails": created_emails}


def departments_of(user) -> List[Department]:
    """The user's departments: the one an admin set, otherwise those whose contact emails include theirs."""
    if not getattr(user, "is_authenticated", False):
        return []
    chosen = UserDepartment.objects.filter(user=user).select_related("department").first()
    if chosen:
        return [chosen.department]
    email = get_user_primary_email(user)
    if not email:
        return []
    return list(Department.objects.filter(contacts__email__iexact=email).distinct())


def can_see_full_pan(user) -> bool:
    if not MASK_PAN:
        return True
    if is_super_admin(user):
        return True
    return any(d.is_active and d.name in FULL_PAN_DEPARTMENTS for d in departments_of(user))


def mask_pan(pan: str) -> str:
    """ABCDE1234F -> ABCXX1234F: the owner type, the digits and the check letter stay; the name part goes."""
    pan = (pan or "").strip()
    if len(pan) == 10:
        return pan[:3] + "XX" + pan[5:]
    return "X" * max(len(pan) - 4, 0) + pan[-4:] if pan else ""


def pan_for(user, pan: str) -> str:
    """The PAN as this user may see it. Used by every page, so the masking lives on the server."""
    return pan if can_see_full_pan(user) else mask_pan(pan)


def log_audit(user, customer, action: str, fields: Iterable[str] = ()) -> None:
    """Record a view or edit: one row per changed field, or one row when no field applies. Never raises."""
    try:
        who = ", ".join(d.name for d in departments_of(user)) or ("Admin" if is_super_admin(user) else "")
        label = f"{customer.code} - {customer.name}"
        names = [f for f in fields if f] or [""]
        KycAuditLog.objects.bulk_create([
            KycAuditLog(user=user if getattr(user, "pk", None) else None, username=getattr(user, "username", ""),
                        department=who, customer=customer if customer.pk else None, customer_label=label,
                        action=action, field_changed=name)
            for name in names])
    except Exception:  # the audit trail must never stop a page from loading
        import logging
        logging.getLogger(__name__).exception("KYC audit log could not be written")


def audit_field_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_: .-]", "", str(name))[:200]
