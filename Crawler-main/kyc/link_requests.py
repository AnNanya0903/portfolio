"""KYC by shareable link: the rules.

Lifecycle of a KycRequest
-------------------------
    Sent ─open─▶ Opened ─save─▶ In progress ─submit─▶ Submitted ─approve─▶ Approved
      │                                                   └─reject─▶ Rejected ─(customer fixes, submits)─▶ Submitted
      ├─ the link passes its expiry (scheduled job, or on open) ─▶ Expired ─resend─▶ Sent / Rejected
      └─ staff revoke ─▶ Revoked ─resend─▶ Sent / Rejected

* Each link is a fresh `secrets.token_urlsafe(32)`; only its SHA-256 is stored. Resend, reject and the staff
  confirmation request each issue a new token, which ends the old link.
* The customer must enter a 6-digit OTP (sent to `recipient_email`, 10 minutes, 5 tries) before the form shows any
  saved data.
* Staff can fill the form on the customer's behalf. Starting to do so ends the customer's link; `filled_by` is set
  and that person may not approve the request.
* Approve copies the details into the customer's empty fields only (GSTIN, PAN, address) and marks the customer
  verified. Reject needs a reason and sends a new link where only the rejected items can be changed.

The views (kyc/link_views.py, kyc/link_public_views.py) only translate HTTP to these calls.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from magicwand.access import is_super_admin

from . import link_fields, link_mail, link_pdf, logistics
from .departments import departments_of, log_audit
from .link_security import (
    UploadError, check_upload, decrypt, encrypt, hash_otp, hash_token, mask, new_otp, new_token, private_storage,
    same_hash, storage_key_for,
)
from .models import (
    Customer, CustomerContact, KycAuditLog, KycDetails, KycOtp, KycRequest, KycRequestAuditLog, KycRequestDocument,
)

logger = logging.getLogger(__name__)

# "Sales and above": every department that deals with customers. Technical/Design only reads.
SENDER_DEPARTMENTS = frozenset({"Sales", "Marketing", "Accounts", "Top Management"})
REVIEWER_DEPARTMENTS = frozenset({"Accounts", "Top Management"})

OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 5

# Statuses from which staff may resend a link or start filling in the form themselves.
REISSUABLE = KycRequest.OPEN_STATUSES + (KycRequest.STATUS_EXPIRED, KycRequest.STATUS_REVOKED)
# A customer has at most one request in these at a time.
ACTIVE = KycRequest.OPEN_STATUSES + (KycRequest.STATUS_SUBMITTED,)


class KycLinkError(Exception):
    """An action that is not allowed now. The message is shown to the person as it is."""


@dataclass
class Actor:
    """Who did something: a staff user, the customer (through the link) or the scheduler."""

    kind: str
    user: object = None
    ip: Optional[str] = None

    @classmethod
    def staff(cls, user, ip=None) -> "Actor":
        return cls(KycRequestAuditLog.ACTOR_STAFF, user, ip)

    @classmethod
    def customer(cls, ip=None) -> "Actor":
        return cls(KycRequestAuditLog.ACTOR_CUSTOMER, None, ip)

    @classmethod
    def system(cls) -> "Actor":
        return cls(KycRequestAuditLog.ACTOR_SYSTEM)


@dataclass
class Issued:
    """A request with the link that was just made for it. `token` is only ever held in memory."""

    request: KycRequest
    token: str = ""
    link: str = ""
    send: Optional[link_mail.SendResult] = None


@dataclass
class OtpResult:
    ok: bool
    message: str = ""
    test_code: str = ""  # test mode only: the code, shown on the page because no email was sent
    extra: Dict = field(default_factory=dict)


# ----- Permissions -----------------------------------------------------------------------------------------------


def _in_departments(user, names) -> bool:
    if is_super_admin(user):
        return True
    return any(d.is_active and d.name in names for d in departments_of(user))


def can_send(user) -> bool:
    return _in_departments(user, SENDER_DEPARTMENTS)


def can_review(user) -> bool:
    return _in_departments(user, REVIEWER_DEPARTMENTS)


def can_reveal(user) -> bool:
    return can_review(user)


def can_approve(user, req: KycRequest) -> bool:
    """Reviewers may approve, except the staff member who filled the form in."""
    return can_review(user) and not (req.filled_by_id and req.filled_by_id == getattr(user, "pk", None))


# ----- Audit -----------------------------------------------------------------------------------------------------


def audit(req: KycRequest, action: str, actor: Actor, **metadata) -> None:
    """Never raises: a missing audit row must not stop the customer or staff."""
    try:
        user = actor.user if getattr(actor.user, "pk", None) else None
        KycRequestAuditLog.objects.create(request=req, action=action, actor_type=actor.kind, actor=user,
                                          ip=actor.ip or None, metadata=metadata)
    except Exception:
        logger.exception("KYC request audit row could not be written (%s, %s)", req.pk, action)


# ----- Links -----------------------------------------------------------------------------------------------------


def expiry_hours() -> int:
    return max(1, int(getattr(settings, "KYC_LINK_EXPIRY_HOURS", 72) or 72))


def reminder_hours() -> int:
    return max(1, int(getattr(settings, "KYC_LINK_REMINDER_HOURS", 48) or 48))


def build_link(token: str, http_request=None) -> str:
    path = reverse("kyc:link_form", args=[token])
    base = str(getattr(settings, "KYC_LINK_BASE_URL", "") or "").rstrip("/")
    if base:
        return f"{base}{path}"
    if http_request is not None:
        return http_request.build_absolute_uri(path)
    return path


def _issue(req: KycRequest, purpose: str = KycRequest.PURPOSE_FILL) -> str:
    """Give the request a new link (the old one stops working) and return its token. Caller saves `req`."""
    token = new_token()
    now = timezone.now()
    req.token_hash = hash_token(token)
    req.link_purpose = purpose
    req.expires_at = now + timedelta(hours=expiry_hours())
    req.sent_at = now
    req.reminder_sent_at = None
    req.otps.filter(used_at__isnull=True).delete()
    return token


def find_by_token(token: str) -> Optional[KycRequest]:
    token = str(token or "")
    if not token or len(token) > 200:
        return None
    wanted = hash_token(token)
    req = KycRequest.objects.select_related("customer").filter(token_hash=wanted).first()
    if req is None or not same_hash(req.token_hash, wanted):
        return None
    return req


def link_state(req: Optional[KycRequest]) -> str:
    """What the public page should show for a token that was found (or None).

    invalid (unknown, replaced by a newer link, revoked, or staff took over) · expired · submitted · approved
    · confirm / confirmed (staff-entered details for the customer to confirm) · open (the form can be filled in)

    The token stays on the request after submit, approval and expiry so the customer sees why the link no longer
    opens the form; revoke, resend and staff filling replace or clear it.
    """
    if req is None or not req.token_hash:
        return "invalid"
    expired = req.expires_at is None or req.expires_at <= timezone.now()
    if req.link_purpose == KycRequest.PURPOSE_CONFIRM:
        if req.confirmed_by_customer_at:
            return "confirmed"
        return "expired" if expired else "confirm"
    if req.status == KycRequest.STATUS_APPROVED:
        return "approved"
    if req.status == KycRequest.STATUS_SUBMITTED:
        return "submitted"
    if req.status == KycRequest.STATUS_REVOKED or req.source == KycRequest.SOURCE_STAFF:
        return "invalid"
    if expired or req.status == KycRequest.STATUS_EXPIRED:
        return "expired"
    return "open"


def link_active(req: KycRequest) -> bool:
    """Whether the request has a link the customer can still use (for the staff pages and Revoke)."""
    return link_state(req) in ("open", "confirm")


def expire_if_due(req: KycRequest) -> bool:
    """Mark a request whose link has passed its time as Expired (the scheduler does this too)."""
    if req.status in KycRequest.OPEN_STATUSES and req.expires_at and req.expires_at <= timezone.now():
        changed = KycRequest.objects.filter(pk=req.pk, status=req.status, expires_at=req.expires_at).update(
            status=KycRequest.STATUS_EXPIRED, updated_at=timezone.now())
        if changed:
            audit(req, "expired", Actor.system())
            req.refresh_from_db()
        return bool(changed)
    return False


# ----- Creating, resending, revoking ------------------------------------------------------------------------------


def _purchaser(customer: Customer) -> Optional[CustomerContact]:
    contacts = list(customer.contacts.all())
    for contact in contacts:
        if contact.is_purchaser:
            return contact
    return contacts[0] if contacts else None


def _prefill(req: KycRequest) -> KycDetails:
    """What AVIGIRI already knows about the customer. The customer has one address, so it fills all three
    addresses and the two "Same as" ticks start ticked."""
    c = req.customer
    contact = _purchaser(c)
    place = dict(city=c.city or "", state=c.state or "", pincode=c.pincode or "")
    details = KycDetails(
        request=req, company_name=c.name, website=c.website or "", gstin=c.gstin or "",
        company_address=c.address or "", billing_address=c.address or "", delivery_address=c.address or "",
        same_billing_as_company=bool(c.address), same_delivery_as_billing=bool(c.address),
        transportation=c.transportation or "",
        preferred_transporter=c.preferred_transporter or logistics.DEFAULT_TRANSPORTER,
        other_transporter=c.other_transporter or "", delivery_type=c.delivery_type or "",
        # MSME is a plain Yes/No on the customer that defaults to No, so only a Yes is known for sure.
        msme_applicable="YES" if c.msme_applicable else "", udyam_aadhar_no=c.udyam_aadhar_no or "")
    for prefix in ("company", "billing", "delivery"):
        for part, value in place.items():
            setattr(details, f"{prefix}_{part}", value)
    if c.pan:
        details.pan_encrypted, details.pan_last4 = encrypt(c.pan), c.pan[-4:]
    if contact is not None:
        details.contact_name = contact.name
        details.contact_designation = contact.designation
        details.contact_email = ", ".join(link_fields.split_emails(contact.email))[:500]
        details.contact_phone = contact.phone
    details.save()
    return details


def active_request_for(customer: Customer) -> Optional[KycRequest]:
    return customer.kyc_requests.filter(status__in=ACTIVE).first()


def recipient_choices(customer: Customer) -> List[Tuple[str, str]]:
    seen: Dict[str, str] = {}
    for contact in customer.contacts.all():
        for address in (contact.email or "").split(","):
            address = address.strip().lower()
            if address and "@" in address and address not in seen:
                role = " (purchaser)" if contact.is_purchaser else ""
                seen[address] = f"{contact.name}{role} · {contact.get_category_display()} · {address}"
    return list(seen.items())


def create(customer: Customer, recipient_email: str, actor: Actor, http_request=None,
           payment_terms: str = "") -> Issued:
    if not can_send(actor.user):
        raise KycLinkError("Only Sales, Marketing, Accounts and Top Management can send a KYC link.")
    recipient_email = str(recipient_email or "").strip().lower()
    if not re.match(link_fields.EMAIL_RE, recipient_email):
        raise KycLinkError("Choose a valid email address to send the link to.")
    with transaction.atomic():
        Customer.objects.select_for_update().get(pk=customer.pk)  # one create at a time per customer
        existing = active_request_for(customer)
        if existing is not None:
            raise KycLinkError(f"{customer.name} already has a KYC request that is {existing.get_status_display()}. "
                               "Resend or revoke that one instead.")
        req = KycRequest(customer=customer, recipient_email=recipient_email, created_by=actor.user,
                         status=KycRequest.STATUS_SENT, payment_terms=_clean_terms(payment_terms))
        req.save()
        token = _issue(req)
        req.save()
        _prefill(req)
    audit(req, "created", actor, recipient=recipient_email)
    return _send_link(req, token, actor, http_request, kind="link", action="sent")


def create_manual(customer: Customer, actor: Actor, payment_terms: str = "") -> KycRequest:
    """Staff fill the whole form themselves (customer on the phone, a paper form in hand). No link and no email is
    sent and no OTP is needed; the request is the staff member's from the start (`source` staff, `filled_by`).
    A link can still be sent afterwards, only for the customer to confirm what was entered."""
    if not can_send(actor.user):
        raise KycLinkError("Only Sales, Marketing, Accounts and Top Management can fill in a KYC form.")
    with transaction.atomic():
        Customer.objects.select_for_update().get(pk=customer.pk)  # one create at a time per customer
        existing = active_request_for(customer)
        if existing is not None:
            raise KycLinkError(f"{customer.name} already has a KYC request that is {existing.get_status_display()}.")
        req = KycRequest.objects.create(
            customer=customer, created_by=actor.user, status=KycRequest.STATUS_IN_PROGRESS,
            source=KycRequest.SOURCE_STAFF, filled_by=actor.user, payment_terms=_clean_terms(payment_terms))
        _prefill(req)
    audit(req, "created", actor, manual=True)
    audit(req, "staff_fill_started", actor, manual=True)
    return req


def _clean_terms(value: str) -> str:
    return " ".join(str(value or "").split())[:200]


def set_payment_terms(req: KycRequest, terms: str, actor: Actor) -> KycRequest:
    """Payment terms are AVIGIRI's to set; the customer only sees them."""
    if not can_send(actor.user):
        raise KycLinkError("You cannot change payment terms.")
    req = KycRequest.objects.get(pk=req.pk)
    if req.status == KycRequest.STATUS_APPROVED:
        raise KycLinkError("This KYC is approved; payment terms can no longer change here.")
    terms = _clean_terms(terms)
    if terms != req.payment_terms:
        req.payment_terms = terms
        req.save(update_fields=["payment_terms", "updated_at"])
        audit(req, "payment_terms_set", actor)
    return req


def set_credit_limit(req: KycRequest, value, actor: Actor) -> KycRequest:
    """The credit limit (Rs.) is decided by Accounts / Top Management; it is printed in the office-use panel and
    copied to the customer on approval. An empty value clears it."""
    if not can_review(actor.user):
        raise KycLinkError("Only Accounts and Top Management can set the credit limit.")
    req = KycRequest.objects.get(pk=req.pk)
    if req.status == KycRequest.STATUS_APPROVED:
        raise KycLinkError("This KYC is approved; the credit limit can no longer change here.")
    text = str(value or "").replace(",", "").replace(" ", "")
    try:
        amount = Decimal(text) if text else None
    except InvalidOperation as exc:
        raise KycLinkError("Enter the credit limit as a number, e.g. 500000.") from exc
    if amount is not None and (amount < 0 or amount >= Decimal("1e12") or amount != amount.quantize(Decimal("0.01"))):
        raise KycLinkError("Enter the credit limit as a positive amount with at most 2 decimals.")
    if amount != req.credit_limit:
        req.credit_limit = amount
        req.save(update_fields=["credit_limit", "updated_at"])
        audit(req, "credit_limit_set", actor)
    return req


def _send_link(req: KycRequest, token: str, actor: Actor, http_request, *, kind: str, action: str) -> Issued:
    link = build_link(token, http_request)
    result = link_mail.send_link(req, link, token, user=actor.user, kind=kind)
    audit(req, action, actor, delivery=result.status, note=result.note, recipient=req.recipient_email)
    if result.status == link_mail.FAILED:
        # A link nobody received should not stay open.
        KycRequest.objects.filter(pk=req.pk, token_hash=req.token_hash).update(expires_at=timezone.now())
        req.refresh_from_db()
    return Issued(req, token, link, result)


def resend(req: KycRequest, actor: Actor, http_request=None, recipient_email: str = "") -> Issued:
    if not can_send(actor.user):
        raise KycLinkError("You cannot send KYC links.")
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().select_related("customer").get(pk=req.pk)
        if req.status not in REISSUABLE:
            raise KycLinkError(f"A {req.get_status_display().lower()} request cannot be sent again.")
        if recipient_email:
            req.recipient_email = recipient_email.strip().lower()
        if req.status in (KycRequest.STATUS_EXPIRED, KycRequest.STATUS_REVOKED):
            req.status = KycRequest.STATUS_REJECTED if req.reject_reason else KycRequest.STATUS_SENT
        if req.filled_at is None:
            req.source = KycRequest.SOURCE_CUSTOMER
        token = _issue(req)
        req.save()
    return _send_link(req, token, actor, http_request, kind="rejected" if req.reject_reason else "link",
                      action="resent")


def revoke(req: KycRequest, actor: Actor, reason: str = "staff") -> KycRequest:
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().get(pk=req.pk)
        if not link_active(req):
            raise KycLinkError("This request has no open link.")
        req.token_hash = ""
        if req.status in KycRequest.OPEN_STATUSES:
            req.status = KycRequest.STATUS_REVOKED
        req.save()
    audit(req, "revoked", actor, reason=reason)
    return req


# ----- The customer opening the link, and the OTP -----------------------------------------------------------------


def mark_opened(req: KycRequest, actor: Actor) -> None:
    """The first open of each link is recorded; Sent becomes Opened."""
    now = timezone.now()
    updates = {}
    if req.opened_at is None or (req.sent_at and req.opened_at < req.sent_at):
        updates["opened_at"] = now
    if req.status == KycRequest.STATUS_SENT:
        updates["status"] = KycRequest.STATUS_OPENED
    if updates:
        KycRequest.objects.filter(pk=req.pk).update(updated_at=now, **updates)
        audit(req, "opened", actor)
        req.refresh_from_db()


def send_otp(req: KycRequest, actor: Actor) -> OtpResult:
    since = timezone.now() - timedelta(hours=1)
    if req.otps.filter(created_at__gte=since).count() >= OTP_MAX_SENDS_PER_HOUR:
        return OtpResult(False, "Too many codes were requested. Please try again in an hour.")
    code = new_otp()
    with transaction.atomic():
        # Older codes stop working but stay, so the hourly limit above can count them.
        req.otps.filter(used_at__isnull=True, expires_at__gt=timezone.now()).update(expires_at=timezone.now())
        KycOtp.objects.create(request=req, otp_hash=hash_otp(req.pk, code),
                              expires_at=timezone.now() + timedelta(minutes=OTP_MINUTES))
    result = link_mail.send_otp(req, code, OTP_MINUTES)
    audit(req, "otp_sent", actor, delivery=result.status)
    if not result.ok:
        return OtpResult(False, "We could not send the code. Please try again later, or reply to our email.")
    return OtpResult(True, f"We sent a 6-digit code to {mask_email(req.recipient_email)}.",
                     test_code=code if result.status == link_mail.DRY_RUN else "")


def verify_otp(req: KycRequest, code: str, actor: Actor) -> OtpResult:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    with transaction.atomic():
        otp = req.otps.select_for_update().filter(used_at__isnull=True).first()
        if otp is None or otp.expires_at <= timezone.now():
            return OtpResult(False, "The code has expired. Please ask for a new one.")
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            return OtpResult(False, "Too many wrong codes. Please ask for a new one.")
        otp.attempts += 1
        if len(code) != 6 or not same_hash(otp.otp_hash, hash_otp(req.pk, code)):
            otp.save(update_fields=["attempts"])
            left = OTP_MAX_ATTEMPTS - otp.attempts
            audit(req, "otp_failed", actor, attempts=otp.attempts)
            return OtpResult(False, f"That code is not right. {left} attempt{'s' if left != 1 else ''} left."
                             if left else "Too many wrong codes. Please ask for a new one.")
        otp.used_at = timezone.now()
        otp.save(update_fields=["attempts", "used_at"])
        KycRequest.objects.filter(pk=req.pk).update(otp_verified_at=otp.used_at, updated_at=otp.used_at)
    audit(req, "otp_verified", actor)
    return OtpResult(True)


def mask_email(address: str) -> str:
    name, _, domain = str(address or "").partition("@")
    if not domain:
        return ""
    return f"{name[:2]}{'*' * max(len(name) - 2, 1)}@{domain}"


# ----- Form data ---------------------------------------------------------------------------------------------------


def details_for(req: KycRequest) -> KycDetails:
    details = getattr(req, "_details_cache", None)
    if details is None:
        details, _ = KycDetails.objects.get_or_create(request=req)
        req._details_cache = details
    return details


def editable_keys(req: KycRequest, actor: Actor) -> List[str]:
    """Field keys and document types this actor may change now."""
    if actor.kind == KycRequestAuditLog.ACTOR_CUSTOMER and req.status == KycRequest.STATUS_REJECTED \
            and req.rejected_fields:
        return [k for k in req.rejected_fields if k in link_fields.REOPENABLE]
    return list(link_fields.REOPENABLE)


def _present(details: KycDetails) -> Dict[str, bool]:
    present = {}
    for f in link_fields.FIELDS:
        if f.key == "pan":
            present[f.key] = bool(details.pan_encrypted)
        elif f.key == "account_number":
            present[f.key] = bool(details.account_number_encrypted)
        else:
            present[f.key] = bool(getattr(details, f.key, ""))
    return present


def form_data(req: KycRequest, actor: Actor) -> Dict:
    """What the form page shows: values (sensitive ones masked), uploaded documents and what may change."""
    details = details_for(req)
    values, masked = {}, {}
    for f in link_fields.FIELDS:
        if f.key == "pan":
            masked["pan"] = mask(details.pan_last4, 10)
        elif f.key == "account_number":
            masked["account_number"] = mask(details.account_last4, 12)
        else:
            values[f.key] = getattr(details, f.key, "")
    values[link_fields.EXTRA_CONTACTS] = list(details.extra_contacts or [])
    docs = {d.doc_type: {"name": d.original_name, "size": d.size, "uploadedAt": d.uploaded_at.isoformat()}
            for d in _latest_documents(req) if d.doc_type in link_fields.DOCUMENT_TYPES}
    values_display = {f.key: link_fields.display(f, values[f.key]) for f in link_fields.FIELDS if f.key in values}
    values_display[link_fields.EXTRA_CONTACTS] = "\n".join(link_fields.contact_lines(details.extra_contacts))
    return {
        "values": values,
        "display": values_display,
        "masked": masked,
        "paymentTerms": req.payment_terms,
        "uploaded": docs,  # not "documents": that is client_config's list of the documents to upload
        "editable": editable_keys(req, actor),
        "rejectReason": req.reject_reason if req.status == KycRequest.STATUS_REJECTED else "",
        "status": req.status,
        "consent": bool(details.consent_given_at),
    }


def _assert_can_edit(req: KycRequest, actor: Actor) -> None:
    if actor.kind == KycRequestAuditLog.ACTOR_CUSTOMER:
        if link_state(req) != "open":
            raise KycLinkError("This form can no longer be changed.")
    else:
        if req.source != KycRequest.SOURCE_STAFF or req.status != KycRequest.STATUS_IN_PROGRESS:
            raise KycLinkError("Start “Fill on behalf of customer” first.")
        if req.filled_by_id != getattr(actor.user, "pk", None):
            raise KycLinkError("Another staff member is filling this form in.")


def _apply_same_as(details: KycDetails, allowed) -> List[str]:
    """A ticked "Same as ..." box means the address is a copy: billing from company, then delivery from billing.
    The browser copies it as it is ticked; this makes the saved data say the same whatever the browser did."""
    changed = []
    for flag, (source, target) in link_fields.SAME_AS.items():
        if not getattr(details, flag):
            continue
        for part in link_fields.ADDRESS_PARTS:
            key = f"{target}_{part}"
            value = getattr(details, f"{source}_{part}")
            if key in allowed and getattr(details, key) != value:
                setattr(details, key, value)
                changed.append(key)
    return changed


def save_draft(req: KycRequest, data: Dict, actor: Actor) -> Dict[str, str]:
    """Save what was entered (any subset of fields). Returns field errors; valid fields are saved regardless."""
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().select_related("customer").get(pk=req.pk)
        _assert_can_edit(req, actor)
        allowed = editable_keys(req, actor)
        values, errors = link_fields.clean(data, allowed)
        details = details_for(req)
        changed = []
        for key, value in values.items():
            if key == "pan":
                details.pan_encrypted, details.pan_last4 = encrypt(value), value[-4:]
            elif key == "account_number":
                details.account_number_encrypted, details.account_last4 = encrypt(value), value[-4:]
            else:
                if getattr(details, key) == value:
                    continue
                setattr(details, key, value)
            changed.append(key)
        changed += _apply_same_as(details, set(allowed))
        if changed:
            details.save()
        if req.status in (KycRequest.STATUS_SENT, KycRequest.STATUS_OPENED):
            req.status = KycRequest.STATUS_IN_PROGRESS
            req.save(update_fields=["status", "updated_at"])
    if changed or errors:
        audit(req, "draft_saved", actor, fields=changed, invalid=sorted(errors))
    return errors


def _latest_documents(req: KycRequest) -> List[KycRequestDocument]:
    latest: Dict[str, KycRequestDocument] = {}
    for doc in req.documents.order_by("-uploaded_at", "-id"):
        latest.setdefault(doc.doc_type, doc)
    return list(latest.values())


def upload_document(req: KycRequest, doc_type: str, uploaded, actor: Actor) -> KycRequestDocument:
    if doc_type not in link_fields.DOCUMENT_TYPES:
        raise KycLinkError("Unknown document type.")
    req = KycRequest.objects.select_related("customer").get(pk=req.pk)
    _assert_can_edit(req, actor)
    if doc_type not in editable_keys(req, actor):
        raise KycLinkError("This document cannot be changed now.")
    return _store_document(req, doc_type, uploaded, actor)


def can_upload_signed_form(req: KycRequest) -> bool:
    return req.status in (KycRequest.STATUS_SUBMITTED, KycRequest.STATUS_APPROVED)


def upload_signed_form(req: KycRequest, uploaded, actor: Actor) -> KycRequestDocument:
    """Sign & Seal: the KYC form PDF, printed, signed and stamped, uploaded after the form was submitted.
    The customer uploads through their link; staff (who may send links) from the request page."""
    req = KycRequest.objects.select_related("customer").get(pk=req.pk)
    if not can_upload_signed_form(req):
        raise KycLinkError("Submit the KYC form first, then upload the signed copy.")
    if actor.kind == KycRequestAuditLog.ACTOR_STAFF and not can_send(actor.user):
        raise KycLinkError("You cannot upload documents.")
    return _store_document(req, KycRequestDocument.TYPE_SIGNED, uploaded, actor)


def store_generated_form(req: KycRequest, actor: Actor, reason: str) -> Optional[KycRequestDocument]:
    """Render the Customer Registration Form for this request and keep it as a document in private storage.

    Called on every submit and again on approval. Older versions stay (they show what was printed then); only the
    newest has `is_latest`. Never raises: a PDF problem must not stop a customer's submit or an approval, so it is
    logged and audited instead, and downloads fall back to rendering live.
    """
    req = KycRequest.objects.select_related("customer", "reviewed_by").get(pk=req.pk)
    try:
        content = link_pdf.render(req)
        key = storage_key_for("application/pdf")
        private_storage.save(key, content, "application/pdf")
    except Exception:
        logger.exception("KYC request %s: the registration form PDF could not be stored (%s)", req.pk, reason)
        audit(req, "pdf_failed", actor, reason=reason)
        return None
    when = timezone.localtime(timezone.now(), timezone.get_fixed_timezone(330))
    with transaction.atomic():
        req.documents.filter(doc_type=KycRequestDocument.TYPE_GENERATED, is_latest=True).update(is_latest=False)
        doc = KycRequestDocument.objects.create(
            request=req, doc_type=KycRequestDocument.TYPE_GENERATED, is_latest=True, storage_key=key,
            original_name=link_pdf.stored_name(req, when), mime_type="application/pdf", size=len(content),
            uploaded_by=actor.user if getattr(actor.user, "pk", None) else None)
    audit(req, "pdf_generated", actor, document=doc.pk, reason=reason, size=len(content))
    return doc


def generated_form(req: KycRequest) -> Optional[KycRequestDocument]:
    """The newest stored registration form PDF, or None."""
    return req.documents.filter(doc_type=KycRequestDocument.TYPE_GENERATED, is_latest=True).order_by("-id").first()


def generated_versions(req: KycRequest) -> List[KycRequestDocument]:
    return list(req.documents.filter(doc_type=KycRequestDocument.TYPE_GENERATED).order_by("-id"))


def customer_generated_form(customer: Customer) -> Optional[KycRequestDocument]:
    """The stored form to open from the customer page: the one of the approved request if there is one, otherwise
    the newest stored form of any request."""
    docs = KycRequestDocument.objects.filter(request__customer=customer, doc_type=KycRequestDocument.TYPE_GENERATED,
                                             is_latest=True).select_related("request").order_by("-id")
    return next((d for d in docs if d.request.status == KycRequest.STATUS_APPROVED), None) or docs.first()


def signed_form(req: KycRequest) -> Optional[KycRequestDocument]:
    return req.documents.filter(doc_type=KycRequestDocument.TYPE_SIGNED).order_by("-uploaded_at", "-id").first()


def _store_document(req: KycRequest, doc_type: str, uploaded, actor: Actor) -> KycRequestDocument:
    try:
        content, mime = check_upload(uploaded)
    except UploadError as exc:
        audit(req, "document_refused", actor, doc_type=doc_type, reason=str(exc))
        raise KycLinkError(str(exc)) from exc
    key = storage_key_for(mime)
    private_storage.save(key, content, mime)
    name = str(getattr(uploaded, "name", "") or "")[-200:]
    with transaction.atomic():
        old = list(req.documents.filter(doc_type=doc_type))
        doc = KycRequestDocument.objects.create(
            request=req, doc_type=doc_type, storage_key=key, original_name=name, mime_type=mime, size=len(content),
            uploaded_by=actor.user if getattr(actor.user, "pk", None) else None)
        for previous in old:
            previous.delete()
    for previous in old:
        private_storage.delete(previous.storage_key)
    if req.status in (KycRequest.STATUS_SENT, KycRequest.STATUS_OPENED):
        KycRequest.objects.filter(pk=req.pk).update(status=KycRequest.STATUS_IN_PROGRESS, updated_at=timezone.now())
    audit(req, "document_uploaded", actor, doc_type=doc_type, size=len(content), mime=mime)
    return doc


def problems_before_submit(req: KycRequest) -> Dict[str, str]:
    details = details_for(req)
    plain = {f.key: getattr(details, f.key, "") for f in link_fields.FIELDS if not f.sensitive}
    problems = {key: "This is required." for key in link_fields.missing(_present(details), plain)}
    # Saved values are checked again: a rule may have changed since the draft was saved.
    for f in link_fields.FIELDS:
        if f.sensitive or f.kind == "checkbox" or problems.get(f.key):
            continue
        value = getattr(details, f.key, "")
        if value:
            message = link_fields.check_value(f, value)
            if message:
                problems[f.key] = message
    _, contact_problems = link_fields.clean_contacts(details.extra_contacts or [])
    problems.update(contact_problems)
    have = {d.doc_type for d in _latest_documents(req)}
    for doc_type, label in link_fields.DOCUMENTS:
        if doc_type in link_fields.REQUIRED_DOCUMENTS and doc_type not in have:
            problems[doc_type] = f"Upload the {label.lower()}."
    return problems


def submit(req: KycRequest, consent: bool, actor: Actor, detail_url: str = "") -> Dict[str, str]:
    """Lock the form and tell the team. Returns problems (and changes nothing) when something is missing."""
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().select_related("customer").get(pk=req.pk)
        _assert_can_edit(req, actor)
        problems = problems_before_submit(req)
        if not consent:
            problems["consent"] = "Please tick the consent box to submit."
        if problems:
            return problems
        now = timezone.now()
        details = details_for(req)
        details.consent_given_at = now
        details.save(update_fields=["consent_given_at", "updated_at"])
        req.status = KycRequest.STATUS_SUBMITTED
        req.submitted_at = now  # locked: the link now only says "submitted"
        if actor.kind == KycRequestAuditLog.ACTOR_STAFF:
            req.filled_at = now
        req.save()
    audit(req, "submitted", actor, source=req.source)
    store_generated_form(req, actor, "submitted")
    _notify_team(req, detail_url)
    return {}


def _notify_team(req: KycRequest, detail_url: str) -> None:
    addresses = []
    for department in req.customer.departments.filter(is_active=True):
        emails = department.emails()
        if emails:
            addresses.append(emails[0])
    result = link_mail.notify_team(req, addresses, detail_url)
    audit(req, "team_notified", Actor.system(), delivery=result.status, note=result.note)
    try:
        from push_notifications.services import send_push_to_users

        users = [u for u in (req.created_by, req.filled_by) if u is not None]
        if users:
            send_push_to_users(users, title=f"KYC submitted: {req.customer.name}",
                               body="Ready for review by Accounts or Top Management.",
                               url=reverse("kyc:link_detail", args=[req.pk]))
    except Exception as exc:
        logger.info("KYC submitted push skipped: %s", exc)


# ----- Staff filling in the form ----------------------------------------------------------------------------------


def start_staff_fill(req: KycRequest, actor: Actor) -> KycRequest:
    if not can_send(actor.user):
        raise KycLinkError("You cannot fill in KYC forms.")
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().get(pk=req.pk)
        if req.source == KycRequest.SOURCE_STAFF and req.status == KycRequest.STATUS_IN_PROGRESS:
            if req.filled_by_id != actor.user.pk:
                raise KycLinkError("Another staff member is already filling this form in.")
            return req
        if req.status not in REISSUABLE:
            raise KycLinkError(f"A {req.get_status_display().lower()} request cannot be filled in.")
        link_was_open = bool(req.token_hash)
        req.token_hash = ""  # the customer's link stops working: no two people editing at once
        req.source = KycRequest.SOURCE_STAFF
        req.filled_by = actor.user
        req.filled_at = None
        req.status = KycRequest.STATUS_IN_PROGRESS
        req.save()
    if link_was_open:
        audit(req, "revoked", actor, reason="staff started filling the form")
    audit(req, "staff_fill_started", actor)
    return req


def default_confirm_email(req: KycRequest) -> str:
    """Where the confirmation link goes by default: the address the request has, else the purchaser's first one."""
    if req.recipient_email:
        return req.recipient_email
    contact = _purchaser(req.customer)
    emails = link_fields.split_emails(contact.email) if contact else []
    return emails[0] if emails else ""


def request_confirmation(req: KycRequest, actor: Actor, http_request=None, recipient_email: str = "") -> Issued:
    """After staff submit: ask the customer to check and confirm what staff entered. A manual request has no
    address yet, so the one to send to is given here (and kept)."""
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().select_related("customer").get(pk=req.pk)
        recipient_email = str(recipient_email or "").strip().lower()
        if recipient_email:
            if not re.match(link_fields.EMAIL_RE, recipient_email):
                raise KycLinkError("Enter a valid email address for the customer.")
            req.recipient_email = recipient_email
        if not req.recipient_email:
            raise KycLinkError("Enter the customer's email address to send the confirmation link to.")
        if req.source != KycRequest.SOURCE_STAFF or req.status not in (KycRequest.STATUS_SUBMITTED,
                                                                      KycRequest.STATUS_APPROVED):
            raise KycLinkError("Only a form submitted by staff can be sent to the customer to confirm.")
        if req.confirmed_by_customer_at:
            raise KycLinkError("The customer has already confirmed these details.")
        token = _issue(req, KycRequest.PURPOSE_CONFIRM)
        req.confirmation_requested_at = timezone.now()
        req.save()
    return _send_link(req, token, actor, http_request, kind="confirm", action="confirmation_sent")


def customer_confirm(req: KycRequest, actor: Actor) -> None:
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().get(pk=req.pk)
        if link_state(req) != "confirm":
            raise KycLinkError("This link can no longer be used.")
        req.confirmed_by_customer_at = timezone.now()
        req.save(update_fields=["confirmed_by_customer_at", "updated_at"])
    audit(req, "customer_confirmed", actor)


def confirm_summary(req: KycRequest) -> List[Dict[str, str]]:
    """The details as the customer sees them on the confirmation page (PAN and account masked)."""
    details = details_for(req)
    rows = []
    for f in link_fields.FIELDS:
        if f.key == "pan":
            value = mask(details.pan_last4, 10)
        elif f.key == "account_number":
            value = mask(details.account_last4, 12)
        else:
            value = link_fields.display(f, getattr(details, f.key, ""))
        rows.append({"label": f.label, "value": value or "—"})
        if f.key == "contact_phone" and details.extra_contacts:
            rows.append({"label": link_fields.EXTRA_CONTACTS_LABEL,
                         "value": "\n".join(link_fields.contact_lines(details.extra_contacts))})
    if req.payment_terms:
        at = next((i for i, r in enumerate(rows) if r["label"] == "Transportation"), len(rows))
        rows.insert(at, {"label": "Payment terms", "value": req.payment_terms})
    return rows


# ----- Review ------------------------------------------------------------------------------------------------------


def review(req: KycRequest, decision: str, actor: Actor, *, reason: str = "", fields: Iterable[str] = (),
           http_request=None) -> Issued:
    reason = str(reason or "").strip()
    with transaction.atomic():
        req = KycRequest.objects.select_for_update().select_related("customer").get(pk=req.pk)
        if not can_review(actor.user):
            raise KycLinkError("Only Accounts and Top Management can approve or reject KYC.")
        if req.status != KycRequest.STATUS_SUBMITTED:
            raise KycLinkError("Only a submitted form can be reviewed.")
        if decision not in ("approve", "reject"):
            raise KycLinkError("Choose approve or reject.")
        now = timezone.now()
        req.reviewed_by = actor.user
        req.reviewed_at = now
        if decision == "approve":
            if not can_approve(actor.user, req):
                raise KycLinkError("You filled in this form, so someone else must approve it.")
            req.status = KycRequest.STATUS_APPROVED
            req.save()
            filled = apply_to_customer(req, actor.user)
            audit(req, "approved", actor, customer_fields_filled=filled)
        else:
            if not reason:
                raise KycLinkError("Write the reason for rejecting.")
            chosen = [k for k in fields if k in link_fields.REOPENABLE]
            if not chosen:
                raise KycLinkError("Tick the fields or documents the customer must correct.")
            req.status = KycRequest.STATUS_REJECTED
            req.reject_reason = reason[:2000]
            req.rejected_fields = chosen
            req.source = KycRequest.SOURCE_CUSTOMER
            token = _issue(req)
            req.save()
    if decision == "approve":
        # Printed again, now with the date of approval, payment terms, credit limit and approver filled in.
        store_generated_form(req, actor, "approved")
        return Issued(req)
    audit(req, "rejected", actor, fields=chosen)
    return _send_link(req, token, actor, http_request, kind="rejected", action="sent")


def apply_to_customer(req: KycRequest, user) -> List[str]:
    """Copy the approved details into the customer's empty fields only, and mark the customer verified.

    Payment terms and the credit limit set on the request are copied as they are. Transport is copied as a group (transportation, preferred and other transporter) when the customer has none
    yet, because Preferred transporter always has a value (ARC by default). MSME is set to Yes when the KYC says
    Yes and the customer still has the default No. The contact person is added as a Sales contact, and each other
    contact person in their own department, unless one of their email addresses is already on the customer.
    """
    details = details_for(req)
    customer = Customer.objects.get(pk=req.customer_id)
    filled = []

    def fill(name, value):
        if value and not getattr(customer, name):
            setattr(customer, name, value)
            filled.append(name)

    fill("gstin", details.gstin)
    fill("pan", decrypt(details.pan_encrypted) if details.pan_encrypted else "")
    fill("address", details.company_address)
    fill("city", details.company_city)
    fill("state", details.company_state)
    fill("pincode", details.company_pincode)
    fill("website", details.website)
    fill("delivery_type", details.delivery_type)
    fill("udyam_aadhar_no", details.udyam_aadhar_no)
    if not customer.transportation and not customer.other_transporter and \
            (details.transportation or details.preferred_transporter):
        customer.transportation = details.transportation or None
        customer.preferred_transporter = details.preferred_transporter or logistics.DEFAULT_TRANSPORTER
        customer.other_transporter = details.other_transporter or None
        filled += ["transportation", "preferred_transporter", "other_transporter"]
    if details.msme_applicable == "YES" and not customer.msme_applicable:
        customer.msme_applicable = True
        filled.append("msme_applicable")
    # What AVIGIRI agreed at approval is set, not only filled in: the reviewer entered it on purpose.
    if req.payment_terms and req.payment_terms != customer.payment_terms:
        customer.payment_terms = req.payment_terms
        filled.append("payment_terms")
    if req.credit_limit is not None and req.credit_limit != customer.credit_limit:
        customer.credit_limit = req.credit_limit
        filled.append("credit_limit")
    customer.kyc_status = Customer.KYC_VERIFIED
    customer.updated_by = user
    customer.save(update_fields=filled + ["kyc_status", "updated_by", "updated_at"])

    emails = link_fields.split_emails(details.contact_email)
    known = {e for c in customer.contacts.all() for e in link_fields.split_emails(c.email)}
    if details.contact_name and emails and not known.intersection(emails):
        CustomerContact.objects.create(
            customer=customer, category=CustomerContact.CATEGORY_SALES, name=details.contact_name[:200],
            designation=details.contact_designation[:200], email=", ".join(emails)[:254],
            phone=details.contact_phone[:40], is_purchaser=not customer.contacts.exists(),
            created_by=user, updated_by=user)
        filled.append("contact")
        known.update(emails)
    departments = dict(CustomerContact.CATEGORY_CHOICES)
    added = 0
    for row in details.extra_contacts or []:
        row_emails = link_fields.split_emails(row.get("email", ""))
        if not row.get("name") or row.get("department") not in departments or known.intersection(row_emails):
            continue
        CustomerContact.objects.create(
            customer=customer, category=row["department"], name=row["name"][:200],
            designation=(row.get("designation") or "")[:200], email=", ".join(row_emails)[:254],
            phone=(row.get("phone") or "")[:40], is_purchaser=not customer.contacts.exists(),
            created_by=user, updated_by=user)
        known.update(row_emails)
        added += 1
    if added:
        filled.append("extra_contacts")
    log_audit(user, customer, KycAuditLog.ACTION_EDIT, filled + ["kyc_status"])
    return filled


# ----- Sensitive fields and documents for staff --------------------------------------------------------------------


def reveal(req: KycRequest, key: str, actor: Actor) -> str:
    if not can_reveal(actor.user):
        raise KycLinkError("Only Accounts and Top Management can see the full number.")
    details = details_for(req)
    encrypted = {"pan": details.pan_encrypted, "account_number": details.account_number_encrypted}.get(key)
    if encrypted is None:
        raise KycLinkError("Unknown field.")
    audit(req, "revealed", actor, field=key)
    return decrypt(encrypted)


def read_document(doc: KycRequestDocument, actor: Actor) -> bytes:
    audit(doc.request, "document_downloaded", actor, doc_type=doc.doc_type, document=doc.pk)
    return private_storage.read(doc.storage_key)


# ----- Scheduled jobs ------------------------------------------------------------------------------------------------


def expire_links(now=None) -> int:
    now = now or timezone.now()
    return sum(expire_if_due(req) for req in KycRequest.objects.filter(
        status__in=KycRequest.OPEN_STATUSES, expires_at__lte=now))


def send_reminders(now=None) -> Dict[str, int]:
    """One reminder per link, `KYC_LINK_REMINDER_HOURS` after it was sent, if the form is still not submitted."""
    now = now or timezone.now()
    due = KycRequest.objects.select_related("customer").filter(
        status__in=KycRequest.OPEN_STATUSES, link_purpose=KycRequest.PURPOSE_FILL,
        source=KycRequest.SOURCE_CUSTOMER, reminder_sent_at__isnull=True,
        sent_at__lte=now - timedelta(hours=reminder_hours()), expires_at__gt=now).exclude(token_hash="")
    sent = failed = 0
    for req in due:
        # Claim the row first so two overlapping runs cannot both send.
        if not KycRequest.objects.filter(pk=req.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=now):
            continue
        result = link_mail.send_reminder(req)
        audit(req, "reminder_sent", Actor.system(), delivery=result.status, note=result.note)
        if result.ok:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}


def run_jobs() -> Dict[str, object]:
    expired = expire_links()
    reminders = send_reminders()
    return {"expired": expired, "reminders": reminders, "dry_run": link_mail.dry_run()}


def status_counts() -> Dict[str, int]:
    from django.db.models import Count

    rows = KycRequest.objects.values("status").annotate(n=Count("id"))
    return {row["status"]: row["n"] for row in rows}


def recent_audit(req: KycRequest, limit: int = 50):
    return list(req.audit_logs.select_related("actor")[:limit])


def customer_kyc(customer: Customer) -> Dict:
    """For the customer page: the latest request and the approved details (masked)."""
    requests = list(customer.kyc_requests.select_related("filled_by", "reviewed_by")[:5])
    approved = next((r for r in requests if r.status == KycRequest.STATUS_APPROVED), None)
    return {
        "requests": requests,
        "active": next((r for r in requests if r.status in ACTIVE), None),
        "approved": approved,
        "approved_rows": confirm_summary(approved) if approved else [],
    }
