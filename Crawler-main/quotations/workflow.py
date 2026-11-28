"""The steps a quotation goes through. Views call these; each step writes one line in the tracker.

    draft -> pending -> approved -> sent -> accepted (proforma invoice unlocked) / declined
    pending -> revision (rejected, with the reason) -> pending (a new version)
A new version uploaded after approval always goes back to pending (it is an edit of an approved quotation).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.dispatch import Signal
from django.utils import timezone

from .models import Quotation, QuotationEvent, QuotationVersion

logger = logging.getLogger(__name__)

# The hook for the proforma invoice stage: sent with quotation= and user= when the customer accepts.
quotation_accepted = Signal()

# A new version of a quotation in one of these states is an edit after approval: it must be approved again.
_APPROVED_OR_LATER = (Quotation.STATUS_APPROVED, Quotation.STATUS_SENT, Quotation.STATUS_ACCEPTED,
                      Quotation.STATUS_DECLINED)


class WorkflowError(Exception):
    """The step is not allowed in the quotation's current state."""


class IntegrityError(WorkflowError):
    """The approved PDF on file is not the one management approved."""


def _name(upload) -> str:
    return (getattr(upload, "name", "") or "")[:255]


@transaction.atomic
def add_version(quotation: Quotation, *, pdf, excel=None, price_note: str = "", note: str = "",
                user=None, submit: bool = True) -> QuotationVersion:
    """Upload a new version. Every earlier version stops being current, even an approved one.

    `submit=False` keeps it as a draft, except after approval: an edit of an approved quotation always goes
    back to management."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    if quotation.status in _APPROVED_OR_LATER:
        submit = True
    last = quotation.versions.aggregate(n=Max("number"))["n"] or 0
    quotation.versions.exclude(status=QuotationVersion.STATUS_SUPERSEDED).update(
        status=QuotationVersion.STATUS_SUPERSEDED)
    version = QuotationVersion(quotation=quotation, number=last + 1, pdf=pdf, pdf_name=_name(pdf),
                               price_note=price_note.strip(), note=note.strip(), uploaded_by=user,
                               status=QuotationVersion.STATUS_PENDING if submit else QuotationVersion.STATUS_DRAFT)
    if excel:
        version.excel, version.excel_name = excel, _name(excel)
    version.save()
    quotation.status = Quotation.STATUS_PENDING if submit else Quotation.STATUS_DRAFT
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_UPLOADED,
                                  comment=version.note, user=user)
    return version


@transaction.atomic
def create_quotation(*, customer, customer_name: str, title: str, enquiry_id: str = "", pdf, excel=None,
                     price_note: str = "", note: str = "", user=None, submit: bool = True,
                     enquiry_reference: str = "") -> Quotation:
    """`enquiry_id` is a checked ENQ number (or ''); the customer's AG-QR text goes in `enquiry_reference`."""
    quotation = Quotation.objects.create(customer=customer, customer_name=customer_name, title=title.strip(),
                                         enquiry_id=enquiry_id.strip().upper(),
                                         enquiry_reference=" ".join(enquiry_reference.split())[:120], created_by=user,
                                         status=Quotation.STATUS_PENDING if submit else Quotation.STATUS_DRAFT)
    add_version(quotation, pdf=pdf, excel=excel, price_note=price_note, note=note, user=user, submit=submit)
    quotation.refresh_from_db()
    return quotation


@transaction.atomic
def submit(quotation: Quotation, *, user) -> QuotationVersion:
    """A draft goes to management for approval."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    version = quotation.latest_version
    if version is None or version.status != QuotationVersion.STATUS_DRAFT:
        raise WorkflowError("There is no draft to submit.")
    version.status = QuotationVersion.STATUS_PENDING
    version.save(update_fields=["status"])
    quotation.status = Quotation.STATUS_PENDING
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_SUBMITTED,
                                  user=user)
    return version


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(field) -> bytes:
    with field.open("rb") as handle:
        return handle.read()


def freeze(version: QuotationVersion) -> None:
    """Keep an immutable copy of the approved PDF, quotations/approved/{number}_v{n}.pdf, and its SHA-256."""
    data = _read(version.pdf)
    version.sha256 = sha256_of(data)
    version.frozen_pdf.save(f"{version.quotation.number}_v{version.number}.pdf", ContentFile(data), save=False)


def approved_pdf(quotation: Quotation) -> tuple:
    """(version, PDF bytes) of the approved PDF, checked against the hash taken on approval.

    Raises WorkflowError when nothing is approved, IntegrityError when the file is not the approved one.
    A version approved before freezing existed is frozen now, once."""
    version = quotation.active_version
    if version is None:
        raise WorkflowError("There is no approved PDF to send yet.")
    if not version.frozen_pdf or not version.sha256:
        freeze(version)
        version.save(update_fields=["frozen_pdf", "sha256"])
        logger.info("Quotations: %s v%s approved before freezing; frozen at send", quotation.number, version.number)
    try:
        data = _read(version.frozen_pdf)
    except Exception as exc:
        raise IntegrityError("The approved PDF file is missing. Ask management to approve it again.") from exc
    if sha256_of(data) != version.sha256:
        raise IntegrityError("The approved PDF has changed since it was approved, so it was not sent. "
                             "Ask management to approve it again.")
    return version, data


def _pending_version(quotation: Quotation, version_id: Optional[int]) -> QuotationVersion:
    latest = quotation.latest_version
    if latest is None or latest.status != QuotationVersion.STATUS_PENDING:
        raise WorkflowError("There is no version waiting for approval.")
    if version_id and latest.pk != version_id:
        raise WorkflowError("A newer version was uploaded. Please look at it before deciding.")
    return latest


@transaction.atomic
def approve(quotation: Quotation, *, user, comment: str = "", version_id: Optional[int] = None) -> QuotationVersion:
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    version = _pending_version(quotation, version_id)
    version.status = QuotationVersion.STATUS_APPROVED
    version.decided_by, version.decided_at, version.decision_comment = user, timezone.now(), comment.strip()
    freeze(version)
    version.save(update_fields=["status", "decided_by", "decided_at", "decision_comment", "frozen_pdf", "sha256"])
    quotation.status = Quotation.STATUS_APPROVED
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_APPROVED,
                                  comment=version.decision_comment, user=user)
    # Only this version may reach the customer now: links to older versions say it was replaced.
    from . import share_links

    share_links.revoke_older(quotation, version, user=user)
    transaction.on_commit(lambda: _notify_ready(quotation, version))
    return version


def _notify_ready(quotation: Quotation, version: QuotationVersion) -> None:
    try:
        from . import notify

        notify.ready_to_send(quotation, version)
    except Exception as exc:  # never fails the approval
        logger.warning("Quotations: could not notify the CRO about %s: %s", quotation.number, exc)


@transaction.atomic
def request_revision(quotation: Quotation, *, user, comment: str = "", corrected_excel=None,
                     version_id: Optional[int] = None) -> QuotationVersion:
    """Send the version back to costing. With a corrected Excel the tracker says the price was modified."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    version = _pending_version(quotation, version_id)
    version.status = QuotationVersion.STATUS_REVISION
    version.decided_by, version.decided_at, version.decision_comment = user, timezone.now(), comment.strip()
    version.save(update_fields=["status", "decided_by", "decided_at", "decision_comment"])
    quotation.status = Quotation.STATUS_REVISION
    quotation.save(update_fields=["status", "updated_at"])
    event = QuotationEvent(
        quotation=quotation, version=version, comment=version.decision_comment, user=user,
        action=QuotationEvent.ACTION_PRICE_MODIFIED if corrected_excel else QuotationEvent.ACTION_REVISION)
    if corrected_excel:
        event.file, event.file_name = corrected_excel, _name(corrected_excel)
    event.save()
    transaction.on_commit(lambda: _notify_rejected(quotation, version, event.comment))
    return version


def _notify_rejected(quotation: Quotation, version: QuotationVersion, reason: str) -> None:
    """Tell the quotation team (who uploaded it, who created it) in the app. Never fails the rejection."""
    people = {u for u in (version.uploaded_by, quotation.created_by) if u is not None}
    if not people:
        return
    try:
        from django.urls import reverse
        from push_notifications.services import send_push_to_users

        send_push_to_users(people, title=f"{quotation.number} needs revision",
                           body=(reason or "Management asked for a revision.")[:180],
                           url=reverse("quotations:detail", args=[quotation.pk]))
    except Exception as exc:
        logger.warning("Quotations: could not notify the team about %s: %s", quotation.number, exc)


@transaction.atomic
def record_send(quotation: Quotation, *, user, version: QuotationVersion, to: List[str], cc: List[str],
                message_id: str, thread_id: str = "", resend: bool = False,
                details: Optional[Dict[str, Any]] = None) -> Quotation:
    """The email went out: Sent to customer, who sent it, to whom, and its Message-ID."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    quotation.status = Quotation.STATUS_SENT
    quotation.sent_at, quotation.sent_by = timezone.now(), user
    quotation.sent_to, quotation.sent_cc = ", ".join(to)[:1000], ", ".join(cc)[:1000]
    quotation.sent_message_id, quotation.sent_thread_id = (message_id or "")[:255], (thread_id or "")[:255]
    quotation.save(update_fields=["status", "sent_at", "sent_by", "sent_to", "sent_cc", "sent_message_id",
                                  "sent_thread_id", "updated_at"])
    QuotationEvent.objects.create(
        quotation=quotation, version=version, user=user,
        action=QuotationEvent.ACTION_RESENT if resend else QuotationEvent.ACTION_SENT,
        comment=f"To {', '.join(to)}" + (f", CC {', '.join(cc)}" if cc else ""),
        details={"to": to, "cc": cc, "message_id": message_id, "thread_id": thread_id, **(details or {})})
    return quotation


def record_send_failed(quotation: Quotation, *, user, version: Optional[QuotationVersion], error: str,
                       to: List[str], cc: List[str]) -> None:
    QuotationEvent.objects.create(quotation=quotation, version=version, user=user,
                                  action=QuotationEvent.ACTION_SEND_FAILED, comment=str(error)[:1000],
                                  details={"to": to, "cc": cc, "error": str(error)[:1000]})


@transaction.atomic
def accept(quotation: Quotation, *, user, reference: str = "", note: str = "") -> Quotation:
    """The customer accepted: the proforma invoice stage is unlocked."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    if quotation.status != Quotation.STATUS_SENT:
        raise WorkflowError("Only a quotation sent to the customer can be marked as accepted.")
    quotation.status = Quotation.STATUS_ACCEPTED
    quotation.customer_reference, quotation.customer_response_note = reference.strip()[:120], note.strip()
    quotation.responded_at, quotation.responded_by = timezone.now(), user
    quotation.save(update_fields=["status", "customer_reference", "customer_response_note", "responded_at",
                                  "responded_by", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=quotation.active_version, user=user,
                                  action=QuotationEvent.ACTION_ACCEPTED, comment=quotation.customer_response_note,
                                  details={"reference": quotation.customer_reference})
    transaction.on_commit(lambda: quotation_accepted.send(sender=Quotation, quotation=quotation, user=user))
    return quotation


@transaction.atomic
def decline(quotation: Quotation, *, user, reason: str) -> Quotation:
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    if quotation.status != Quotation.STATUS_SENT:
        raise WorkflowError("Only a quotation sent to the customer can be marked as declined.")
    if not (reason or "").strip():
        raise WorkflowError("Say why the customer declined.")
    quotation.status = Quotation.STATUS_DECLINED
    quotation.customer_response_note = reason.strip()
    quotation.responded_at, quotation.responded_by = timezone.now(), user
    quotation.save(update_fields=["status", "customer_response_note", "responded_at", "responded_by",
                                  "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=quotation.active_version, user=user,
                                  action=QuotationEvent.ACTION_DECLINED, comment=quotation.customer_response_note)
    return quotation


@transaction.atomic
def mark_sent(quotation: Quotation, *, user) -> QuotationVersion:
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    version = quotation.active_version
    if version is None:
        raise WorkflowError("There is no approved PDF to send yet.")
    quotation.status = Quotation.STATUS_SENT
    quotation.sent_at, quotation.sent_by = timezone.now(), user
    quotation.save(update_fields=["status", "sent_at", "sent_by", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_SENT, user=user,
                                  comment="Sent outside the portal", details={"manual": True})
    return version
