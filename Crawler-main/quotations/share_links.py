"""Share links tied to one approved quotation version (the existing signed share link of the enquiry pages).

- create_for_version: a password-protected link to the approved PDF of a version (enquiries:share_pretty).
- revoke_older: when a newer version is approved, every link of the quotation's older versions stops working;
  its page only says the quotation was replaced.
- is_replaced: what the share page checks (also for links whose row was never saved).
- new_password: give a link a new password (when the password email failed and has to be sent again).
"""
from __future__ import annotations

import base64
import logging
import secrets
from typing import Any, Dict, Mapping, Optional

from django.contrib.auth.hashers import make_password
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import Quotation, QuotationEvent, QuotationVersion

logger = logging.getLogger(__name__)

SALT = "enquiry-share"  # the salt of every enquiry share link
# Keys of the signed data that are for the portal only, never shown on the share page.
HIDDEN_KEYS = frozenset({"quotation_id", "quotation_version_id"})


def _password() -> str:
    from enquiries.views import generate_share_password  # enquiries.views imports a lot

    return generate_share_password()


def payload_for(quotation: Quotation, version: QuotationVersion, pdf: bytes, *,
                values: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The signed data of a version's link (without the password): the approved PDF and what identifies it."""
    data = dict(values or {})
    data.update({
        "quotation_number": quotation.number,
        "quotation_version": version.number,
        "quotation_id": quotation.pk,
        "quotation_version_id": version.pk,
        "pdf_filename": f"{quotation.number}.pdf",
        "pdf_content_b64": base64.b64encode(pdf).decode("ascii"),
        "pdf_content_type": "application/pdf",
        "requires_password": True,
    })
    if quotation.enquiry_id:
        data["enquiry_id"] = quotation.enquiry_id
    return data


def create_for_version(quotation: Quotation, version: QuotationVersion, pdf: bytes, *, base_url: str,
                       values: Optional[Mapping[str, Any]] = None):
    """(ShareLink, url, password) for the approved PDF of `version`. `base_url` is the site, e.g.
    https://www.magicwand.biz."""
    from enquiries.models import ShareLink

    password = _password()
    data = payload_for(quotation, version, pdf, values=values)
    data["password_hash"] = make_password(password)
    token = signing.dumps(data, salt=SALT, compress=True)
    enq_no = slugify(quotation.enquiry_id or quotation.number).upper() or "ENQUIRY"
    record = ShareLink.objects.create(id=secrets.token_urlsafe(9), token=token, brand="magicwand", enq_no=enq_no,
                                      quotation_version_id=version.pk)
    url = base_url.rstrip("/") + reverse("enquiries:share_pretty", args=[record.brand, record.enq_no, record.id])
    return record, url, password


def new_password(record) -> str:
    """A new password for an existing link (the old one stops working)."""
    data = signing.loads(record.token, salt=SALT)
    password = _password()
    data["password_hash"] = make_password(password)
    record.token = signing.dumps(data, salt=SALT, compress=True)
    record.save(update_fields=["token"])
    return password


def revoke_older(quotation: Quotation, keep: QuotationVersion, *, user=None) -> int:
    """Revoke every unrevoked link of the quotation's versions other than `keep`. Returns how many."""
    from enquiries.models import ShareLink

    older = list(quotation.versions.exclude(pk=keep.pk).values_list("pk", flat=True))
    if not older:
        return 0
    count = ShareLink.objects.filter(quotation_version_id__in=older, revoked_at__isnull=True).update(
        revoked_at=timezone.now())
    if count:
        QuotationEvent.objects.create(
            quotation=quotation, version=keep, action=QuotationEvent.ACTION_LINKS_REVOKED, user=user,
            comment=f"{count} link{'s' if count != 1 else ''} of older versions now say the quotation was replaced.",
            details={"count": count})
    return count


def is_replaced(record, data: Mapping[str, Any]) -> bool:
    """True when the link shows a version that is no longer the one to use: revoked, or a newer version of its
    quotation was approved since (also catches links whose row was never saved)."""
    if record is not None and getattr(record, "revoked_at", None):
        return True
    version_id = (getattr(record, "quotation_version_id", None) if record is not None else None) \
        or data.get("quotation_version_id")
    if not version_id:
        return False
    try:
        version = QuotationVersion.objects.select_related("quotation").get(pk=int(version_id))
    except (QuotationVersion.DoesNotExist, TypeError, ValueError):
        return True  # the quotation was deleted
    return QuotationEvent.objects.filter(quotation=version.quotation, action=QuotationEvent.ACTION_APPROVED,
                                         version__number__gt=version.number).exists()


def latest_approved_link(quotation: Quotation):
    """The newest unrevoked link of the quotation's approved version, or None."""
    from enquiries.models import ShareLink

    active = quotation.active_version
    if active is None:
        return None
    return (ShareLink.objects.filter(quotation_version_id=active.pk, revoked_at__isnull=True)
            .order_by("-created_at").first())

