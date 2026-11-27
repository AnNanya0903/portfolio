"""Tell the CRO / Customer Service when a quotation version is approved and ready to send.

Who: users given the "Quotations: email to customer (CRO)" section explicitly (restricted access with
quotation_send), and the members of the KYC departments in QUOTATION_READY_NOTIFY_DEPARTMENTS (default "Sales",
the customerservice@ team). In the app (push notification) always; by email too when QUOTATION_READY_NOTIFY_EMAIL
is on. Never fails the approval.
"""
from __future__ import annotations

import logging
from typing import List, Set

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse

from . import roles
from .models import Quotation, QuotationVersion

logger = logging.getLogger(__name__)


def _department_names() -> List[str]:
    raw = getattr(settings, "QUOTATION_READY_NOTIFY_DEPARTMENTS", "Sales")
    names = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    return [n.strip() for n in names if str(n).strip()]


def cro_users() -> List:
    """Active users who send quotations to customers (see the module docstring)."""
    User = get_user_model()
    ids: Set[int] = set()
    try:
        from dashboard.models import UserServiceAccess

        for access in UserServiceAccess.objects.filter(restrict=True).only("user_id", "allowed_services"):
            if roles.SEND in (access.allowed_services or []):
                ids.add(access.user_id)
    except Exception as exc:
        logger.warning("Quotations: could not read who has the CRO section: %s", exc)
    names = _department_names()
    if names:
        try:
            from kyc.models import DepartmentContact, UserDepartment

            ids.update(UserDepartment.objects.filter(department__name__in=names, department__is_active=True)
                       .values_list("user_id", flat=True))
            emails = {e.lower() for e in DepartmentContact.objects.filter(
                department__name__in=names, department__is_active=True).values_list("email", flat=True)}
            if emails:
                match = Q()
                for email in emails:
                    match |= Q(email__iexact=email)
                ids.update(User.objects.filter(match, is_active=True).values_list("pk", flat=True))
        except Exception as exc:
            logger.warning("Quotations: could not read the departments to notify: %s", exc)
    return list(User.objects.filter(pk__in=ids, is_active=True).order_by("id"))


def ready_message(quotation: Quotation, version: QuotationVersion) -> str:
    enquiry = quotation.enquiry_id or "no enquiry linked"
    return f"{quotation.number} ({enquiry}, {quotation.customer_name}) v{version.number} approved – ready to send"


def ready_to_send(quotation: Quotation, version: QuotationVersion) -> int:
    """Notify the CRO users. Returns how many were told."""
    users = cro_users()
    if not users:
        logger.info("Quotations: %s approved; nobody to notify (no CRO users found)", quotation.number)
        return 0
    text = ready_message(quotation, version)
    url = reverse("quotations:detail", args=[quotation.pk])
    try:
        from push_notifications.services import send_push_to_users

        send_push_to_users(users, title="Quotation ready to send", body=text[:180], url=url)
    except Exception as exc:
        logger.warning("Quotations: push notification for %s failed: %s", quotation.number, exc)
    if getattr(settings, "QUOTATION_READY_NOTIFY_EMAIL", False):
        addresses = sorted({(u.email or "").strip().lower() for u in users if (u.email or "").strip()})
        if addresses:
            try:
                from . import mailer

                base = str(getattr(settings, "ENQUIRY_SHARE_BASE_URL", "") or "").rstrip("/")
                body = f"{text}.\n\nOpen it to send it to the customer: {base}{url}\n\nMagicWand"
                mailer.send_email(addresses, subject=text, body=body)
            except Exception as exc:
                logger.warning("Quotations: notification email for %s failed: %s", quotation.number, exc)
    return len(users)
