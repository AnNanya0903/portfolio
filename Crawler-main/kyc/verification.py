"""Ask a customer to check the KYC details we hold: the email, the private link and their answer.

Test mode (KYC_VERIFICATION_DRY_RUN, on by default) records the email without sending it, and the link
then only shows a preview that cannot be answered.
"""
from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from dashboard.follow_up_templates import sender_signature
from dashboard.gmail_service import GmailAPIError, GmailService

from .models import Customer, CustomerContact, KycVerification

logger = logging.getLogger(__name__)

SUBJECT = "Please confirm your company details with AVIGIRI"

_STATUS_FOR_RESPONSE = {
    KycVerification.RESPONSE_CONFIRMED: Customer.KYC_VERIFIED,
    KycVerification.RESPONSE_CHANGES: Customer.KYC_CHANGES,
    KycVerification.RESPONSE_REJECTED: Customer.KYC_REJECTED,
}


def dry_run() -> bool:
    return bool(getattr(settings, "KYC_VERIFICATION_DRY_RUN", True))


def link_days() -> int:
    return max(1, int(getattr(settings, "KYC_VERIFICATION_LINK_DAYS", 30) or 30))


def default_cc() -> List[str]:
    raw = getattr(settings, "FOLLOW_UP_DEFAULT_CC", "") or ""
    return [a.strip() for a in raw.split(",") if "@" in a]


def _split_addresses(values: Iterable[str]) -> List[str]:
    """Flatten a list of address strings, splitting each on commas, lowercased and deduplicated."""
    found: List[str] = []
    for value in values:
        for part in str(value or "").split(","):
            part = part.strip().lower()
            if part and "@" in part and part not in found:
                found.append(part)
    return sorted(found)


def build_body(customer: Customer, link: str, valid_until) -> str:
    # The customer code is left out on purpose: it may double as a password for shared files.
    return "\n\n".join([
        "Dear Sir / Madam,",
        f"We are updating our customer records. Please check the company details and contact persons we "
        f"hold for {customer.name}, and tell us whether they are correct:",
        link,
        "On that page you can confirm the details, or tell us what needs to change. It takes about a minute.",
        f"The link is private to your company and works until {valid_until.day} {valid_until:%B %Y}.",
        sender_signature(),
    ])


def snapshot(customer: Customer) -> Dict[str, Any]:
    """The details shown to the customer, as plain data."""
    fields = ("name", "address", "city", "state", "pincode", "country", "gstin", "pan", "phone", "website")
    return {
        "customer": {field: getattr(customer, field) for field in fields},
        "contacts": [
            {"team": c.get_category_display(), "name": c.name, "designation": c.designation, "email": c.email,
             "phone": c.phone, "is_purchaser": c.is_purchaser}
            for c in customer.contacts.all()
        ],
    }


def contact_groups(customer: Customer) -> List[Dict[str, Any]]:
    contacts = list(customer.contacts.all())
    groups = [{"label": label, "contacts": [c for c in contacts if c.category == key]}
              for key, label in CustomerContact.CATEGORY_CHOICES]
    return [g for g in groups if g["contacts"]]


def _send_gmail(gmail: GmailService, to: List[str], cc: List[str], subject: str, body: str) -> Dict[str, Any]:
    if not gmail.service and not gmail.authenticate():
        raise GmailAPIError("Gmail sign-in failed")
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        return gmail.service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:  # googleapiclient HttpError and network errors
        raise GmailAPIError(f"Gmail could not send the email: {exc}") from exc


def send_verification(customer: Customer, to: List[str], user, build_link) -> KycVerification:
    """Record and send the verification email. `build_link(token)` gives the full address of the customer page.

    Never raises for a sending problem: the returned row says what happened (status and note).
    """
    now = timezone.now()
    to = _split_addresses(to)
    cc = [a for a in default_cc() if a.lower() not in to]
    valid_until = now + timedelta(days=link_days())
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        # A new email replaces the earlier ones: their links stop working.
        customer.verifications.filter(response="", expires_at__gt=now).update(expires_at=now)
        row = KycVerification.objects.create(
            customer=customer, token=token, sent_to=", ".join(to)[:1000], cc=", ".join(cc)[:1000],
            subject=SUBJECT, body=build_body(customer, build_link(token), valid_until),
            sent_by=user, expires_at=valid_until)

    if not to:
        row.status, row.note = KycVerification.STATUS_FAILED, "no email address was chosen"
    elif dry_run():
        row.status, row.note = KycVerification.STATUS_DRY_RUN, "test mode: nothing was sent"
    else:
        try:
            sent = _send_gmail(GmailService(user), to, cc, row.subject, row.body)
        except GmailAPIError as exc:
            row.status, row.note = KycVerification.STATUS_FAILED, str(exc)[:500]
        except Exception as exc:
            logger.exception("KYC verification email failed for customer %s", customer.pk)
            row.status, row.note = KycVerification.STATUS_FAILED, f"send failed: {exc}"[:500]
        else:
            row.status = KycVerification.STATUS_SENT
            row.sent_at = timezone.now()
            row.gmail_message_id = (sent or {}).get("id") or ""
    if row.status == KycVerification.STATUS_FAILED:
        row.expires_at = now  # a link nobody received should not stay open
    row.save()
    if row.status == KycVerification.STATUS_SENT:
        customer.kyc_status = Customer.KYC_SENT
        customer.save(update_fields=["kyc_status", "updated_at"])
    return row


def link_state(row: KycVerification) -> str:
    """'answered', 'expired', 'preview' (test mode: can be seen, not answered) or 'open'."""
    if row.response:
        return "answered"
    if row.expires_at <= timezone.now():
        return "expired"
    if row.status != KycVerification.STATUS_SENT:
        return "preview"
    return "open"


def record_response(token: str, response: str, comment: str) -> Optional[KycVerification]:
    """Save the customer's answer. Returns None when the link can no longer be answered."""
    with transaction.atomic():
        row = (KycVerification.objects.select_for_update().select_related("customer")
               .filter(token=token).first())
        if row is None or link_state(row) != "open":
            return None
        row.response = response
        row.response_comment = (comment or "").strip()
        row.responded_at = timezone.now()
        row.response_snapshot = snapshot(row.customer)
        row.save(update_fields=["response", "response_comment", "responded_at", "response_snapshot"])
        row.customer.kyc_status = _STATUS_FOR_RESPONSE[response]
        row.customer.save(update_fields=["kyc_status", "updated_at"])
    _notify(row)
    return row


def _notify(row: KycVerification) -> None:
    """Phone notification to the person who sent the email. Optional: push may not be set up."""
    if row.sent_by is None:
        return
    try:
        from django.urls import reverse
        from push_notifications.services import send_push_to_users

        send_push_to_users(
            [row.sent_by], title=f"KYC answer from {row.customer.name}",
            body=f"{row.get_response_display()}.", url=reverse("kyc:detail", args=[row.customer_id]))
    except Exception as exc:
        logger.info("KYC push to %s skipped: %s", row.sent_by_id, exc)
