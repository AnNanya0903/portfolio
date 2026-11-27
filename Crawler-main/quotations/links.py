"""How the other portals find the quotations of an enquiry or a customer (and link to them)."""
from __future__ import annotations

from typing import Dict, Iterable, List

from django.db.models import Count, Q

from .models import Quotation, QuotationEvent


def counts_for_enquiries(enquiry_ids: Iterable[str]) -> Dict[str, int]:
    """{enquiry id: number of quotations entered for it}. One query, for a whole page of the board."""
    ids = [str(i) for i in enquiry_ids if str(i)]
    if not ids:
        return {}
    rows = Quotation.objects.filter(enquiry_id__in=ids).values("enquiry_id").annotate(n=Count("id"))
    return {row["enquiry_id"]: row["n"] for row in rows}


def for_enquiry(enquiry_id: str) -> List[Quotation]:
    return list(Quotation.objects.filter(enquiry_id=str(enquiry_id)).order_by("-updated_at", "-id")[:20])


def for_customer(customer) -> List[Quotation]:
    return list(Quotation.objects.filter(customer=customer).order_by("-updated_at", "-id")[:20])


def events_for_enquiry(enquiry_id: str, limit: int = 50) -> List[QuotationEvent]:
    """The audit trail of every quotation of an enquiry, and of its email thread, newest first."""
    enquiry_id = str(enquiry_id or "")
    if not enquiry_id:
        return []
    rows = QuotationEvent.objects.filter(Q(enquiry_id=enquiry_id) | Q(quotation__enquiry_id=enquiry_id))
    return list(rows.select_related("user", "version", "quotation")[:limit])


# ----- what the Enquiry Board and the enquiry page show about an enquiry's quotations ------------------------------

def _label(quotation: Quotation, latest) -> str:
    """One line for the enquiry: where its quotation stands, with the version."""
    n = latest.number if latest is not None else 0
    status = quotation.status
    if status == Quotation.STATUS_APPROVED:
        return f"Revised quotation ready to send (v{n})" if n > 1 else f"Quotation ready to send (v{n})"
    if status == Quotation.STATUS_SENT and quotation.sent_at:
        from django.utils import timezone

        return f"Quotation sent {timezone.localtime(quotation.sent_at):%d %b %Y} (v{n})"
    if status == Quotation.STATUS_PENDING:
        return f"Waiting for management approval (v{n})"
    if status == Quotation.STATUS_REVISION:
        return f"Quotation needs revision (v{n})"
    return f"{quotation.get_status_display()} (v{n})" if n else quotation.get_status_display()


def summaries_for_enquiries(enquiry_ids: Iterable[str]) -> Dict[str, List[Dict[str, object]]]:
    """{enquiry id: [{pk, number, title, status, status_display, version, label}, ...]} newest first.
    Two queries for a whole page of the board."""
    ids = [str(i) for i in enquiry_ids if str(i)]
    if not ids:
        return {}
    rows = (Quotation.objects.filter(enquiry_id__in=ids).prefetch_related("versions")
            .order_by("-updated_at", "-id"))
    out: Dict[str, List[Dict[str, object]]] = {}
    for quotation in rows:
        versions = sorted(quotation.versions.all(), key=lambda v: v.number)
        latest = versions[-1] if versions else None
        out.setdefault(quotation.enquiry_id, []).append({
            "pk": quotation.pk, "number": quotation.number, "title": quotation.title,
            "status": quotation.status, "status_display": quotation.get_status_display(),
            "version": latest.number if latest is not None else 0,
            "label": _label(quotation, latest),
        })
    return out


def version_history(enquiry_id: str) -> List[Dict[str, object]]:
    """Every version of every quotation of the enquiry: approved by / on, sent on, the revision reason."""
    enquiry_id = str(enquiry_id or "")
    if not enquiry_id:
        return []
    quotations = list(Quotation.objects.filter(enquiry_id=enquiry_id).order_by("-updated_at", "-id"))
    sent_on: Dict[int, object] = {}
    approvers: Dict[int, object] = {}
    for event in (QuotationEvent.objects.filter(quotation__in=quotations, version__isnull=False,
                                                action__in=(QuotationEvent.ACTION_SENT, QuotationEvent.ACTION_RESENT,
                                                            QuotationEvent.ACTION_APPROVED))
                  .select_related("user").order_by("created_at", "id")):
        if event.action == QuotationEvent.ACTION_APPROVED:
            approvers[event.version_id] = event
        else:
            sent_on[event.version_id] = event.created_at  # the latest send of that version
    history = []
    for quotation in quotations:
        for version in quotation.versions.select_related("decided_by").order_by("-number"):
            approval = approvers.get(version.pk)
            history.append({
                "quotation": quotation, "version": version,
                "approved_by": approval.user if approval else None,
                "approved_at": approval.created_at if approval else None,
                "sent_at": sent_on.get(version.pk),
                "revision_reason": version.decision_comment if version.status == version.STATUS_REVISION
                or (version.decided_at and approval is None) else "",
            })
    return history


def ready_to_send(enquiry_id: str = "") -> List[Quotation]:
    """Approved quotations waiting for the CRO (of one enquiry, or all)."""
    rows = Quotation.objects.filter(status=Quotation.STATUS_APPROVED)
    if enquiry_id:
        rows = rows.filter(enquiry_id=str(enquiry_id))
    return list(rows.order_by("updated_at", "id"))
