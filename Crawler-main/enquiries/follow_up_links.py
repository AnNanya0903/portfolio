"""The online share link and password that every enquiry follow-up (1-4) repeats.

Always the enquiry's OWN link for its LATEST quotation / proforma invoice revision (the newest share link
created for that enquiry), checked against the signed link data so another enquiry's link is never used:
1. the link and password sent with the quotation / PI email (FollowUpStart) or saved for follow-ups
   earlier (EnquiryContact), when they still open the newest revision;
2. otherwise a new link is generated from the newest revision with a new password, and saved on the
   enquiry so the next follow-ups repeat the same link and password.
When there is no revision to link to, ShareLinkMissing is raised and the follow-up is not sent.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core import signing
from django.urls import reverse
from django.utils.text import slugify

from dashboard.follow_up_templates import MARKETING_STAGE

from .models import EnquiryContact, FollowUpStart, ShareLink

logger = logging.getLogger(__name__)

SALT = "enquiry-share"
_SCAN_LIMIT = 200  # newest share links looked at per enquiry number


class ShareLinkMissing(RuntimeError):
    """No share link can be found or generated for this enquiry."""


def _enq_no_slugs(enquiry_id: str, enquiry: Optional[Mapping[str, Any]]) -> set:
    values = {str(enquiry_id or ""), str((enquiry or {}).get("enq_number") or "")}
    return {slugify(v).upper() for v in values if v.strip()} - {""}


def _load(record: ShareLink) -> Optional[dict]:
    try:
        return signing.loads(record.token, salt=SALT)
    except signing.BadSignature:
        return None


def latest_revision(enquiry_id: str, enquiry: Optional[Mapping[str, Any]] = None) -> Optional[Tuple[ShareLink, dict]]:
    """The newest share link whose signed data belongs to this enquiry (its latest revision)."""
    slugs = _enq_no_slugs(enquiry_id, enquiry)
    if not slugs:
        return None
    from quotations.share_links import is_replaced

    for record in (ShareLink.objects.filter(enq_no__in=slugs, revoked_at__isnull=True)
                   .order_by("-created_at")[:_SCAN_LIMIT]):
        data = _load(record)
        if data and str(data.get("enquiry_id") or "") == str(enquiry_id) and data.get("pdf_content_b64"):
            if is_replaced(record, data):  # a version replaced since: never send it again
                continue
            return record, data
    return None


def _share_id(url: str) -> str:
    path = urlparse(url or "").path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _same_revision(url: str, password: str, enquiry_id: str, latest: ShareLink) -> bool:
    """True when `url` + `password` open this enquiry's latest revision."""
    from django.contrib.auth.hashers import check_password

    if not url or not password:
        return False
    record = ShareLink.objects.filter(id=_share_id(url)).first()
    if record is None:
        return False
    data = _load(record)
    if not data or str(data.get("enquiry_id") or "") != str(enquiry_id):
        return False
    if not check_password(password, data.get("password_hash") or ""):
        return False
    # A copy made for follow-ups is itself the newest link, so only the newest link counts.
    return record.id == latest.id


def _base_url(enquiry_id: str) -> str:
    """Where generated links point: ENQUIRY_SHARE_BASE_URL, KYC_LINK_BASE_URL, else the site of earlier links."""
    for name in ("ENQUIRY_SHARE_BASE_URL", "KYC_LINK_BASE_URL"):
        value = str(getattr(settings, name, "") or "").strip().rstrip("/")
        if value:
            return value
    for url in (FollowUpStart.objects.exclude(share_url="").order_by("-started_at")
                .values_list("share_url", flat=True)[:20]):
        parts = urlparse(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def _generate(enquiry_id: str, latest: ShareLink, data: dict) -> Tuple[str, str]:
    """A new link to the same revision, with a new password."""
    import secrets

    from .views import generate_share_password  # views imports a lot

    base = _base_url(enquiry_id)
    if not base:
        raise ShareLinkMissing("no site address is known for share links; set ENQUIRY_SHARE_BASE_URL")
    password = generate_share_password()
    payload = {**data, "password_hash": make_password(password), "requires_password": True}
    token = signing.dumps(payload, salt=SALT, compress=True)
    record = ShareLink.objects.create(id=secrets.token_urlsafe(9), token=token, brand=latest.brand or "magicwand",
                                      enq_no=latest.enq_no, quotation_version_id=latest.quotation_version_id)
    url = base + reverse("enquiries:share_pretty", args=[record.brand, record.enq_no, record.id])
    logger.info("Generated a follow-up share link for enquiry %s (revision %s)", enquiry_id, latest.id)
    return url, password


def for_follow_up(enquiry_id: str, enquiry: Optional[Mapping[str, Any]] = None, *,
                  start: Optional[FollowUpStart] = None) -> Tuple[str, str]:
    """(share_url, password) to put in this enquiry's next follow-up. Raises ShareLinkMissing."""
    enquiry_id = str(enquiry_id)
    found = latest_revision(enquiry_id, enquiry)
    if found is None:
        raise ShareLinkMissing("no quotation or proforma invoice share link exists for this enquiry")
    latest, data = found

    known = []
    if start is not None and start.stage != MARKETING_STAGE:
        known.append((start.share_url, start.share_password))
    for row in (FollowUpStart.objects.filter(enquiry_id=enquiry_id).exclude(stage=MARKETING_STAGE)
                .exclude(share_url="").order_by("-started_at")[:5]):
        known.append((row.share_url, row.share_password))
    contact = EnquiryContact.objects.filter(enquiry_id=enquiry_id).first()
    if contact is not None:
        known.append((contact.follow_up_share_url, contact.follow_up_share_password))
    for url, password in known:
        if _same_revision(url, password, enquiry_id, latest):
            return url, password

    url, password = _generate(enquiry_id, latest, data)
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=enquiry_id)
    contact.follow_up_share_url = url[:500]
    contact.follow_up_share_password = password[:64]
    contact.save(update_fields=["follow_up_share_url", "follow_up_share_password", "updated_at"])
    return url, password
