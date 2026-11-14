"""The steps a quotation goes through. Views call these; each step writes one line in the tracker."""
from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import Quotation, QuotationEvent, QuotationVersion


class WorkflowError(Exception):
    """The step is not allowed in the quotation's current state."""


def _name(upload) -> str:
    return (getattr(upload, "name", "") or "")[:255]


@transaction.atomic
def add_version(quotation: Quotation, *, pdf, excel=None, price_note: str = "", note: str = "",
                user=None) -> QuotationVersion:
    """Upload a new version. Every earlier version stops being current, even an approved one."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    last = quotation.versions.aggregate(n=Max("number"))["n"] or 0
    quotation.versions.exclude(status=QuotationVersion.STATUS_SUPERSEDED).update(
        status=QuotationVersion.STATUS_SUPERSEDED)
    version = QuotationVersion(quotation=quotation, number=last + 1, pdf=pdf, pdf_name=_name(pdf),
                               price_note=price_note.strip(), note=note.strip(), uploaded_by=user)
    if excel:
        version.excel, version.excel_name = excel, _name(excel)
    version.save()
    quotation.status = Quotation.STATUS_PENDING
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_UPLOADED,
                                  comment=version.note, user=user)
    return version


@transaction.atomic
def create_quotation(*, customer, customer_name: str, title: str, enquiry_id: str = "", pdf, excel=None,
                     price_note: str = "", note: str = "", user=None) -> Quotation:
    quotation = Quotation.objects.create(customer=customer, customer_name=customer_name, title=title.strip(),
                                         enquiry_id=enquiry_id.strip(), created_by=user)
    add_version(quotation, pdf=pdf, excel=excel, price_note=price_note, note=note, user=user)
    quotation.refresh_from_db()
    return quotation


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
    version.save(update_fields=["status", "decided_by", "decided_at", "decision_comment"])
    quotation.status = Quotation.STATUS_APPROVED
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_APPROVED,
                                  comment=version.decision_comment, user=user)
    return version


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
    return version


@transaction.atomic
def mark_sent(quotation: Quotation, *, user) -> QuotationVersion:
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    version = quotation.active_version
    if version is None:
        raise WorkflowError("There is no approved PDF to send yet.")
    quotation.status = Quotation.STATUS_SENT
    quotation.save(update_fields=["status", "updated_at"])
    QuotationEvent.objects.create(quotation=quotation, version=version, action=QuotationEvent.ACTION_SENT, user=user)
    return version
