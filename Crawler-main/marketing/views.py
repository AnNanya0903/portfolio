"""Marketing portal: the list of lost enquiries and one page per lost enquiry."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from enquiries import services
from enquiries.models import EnquiryContact, FollowUpSent

from . import lost, notify
from .models import CampaignRecipient, MarketingActivity, MarketingLead

logger = logging.getLogger(__name__)


def _api_root() -> str:
    return getattr(settings, "ENQUIRY_API_ROOT", "http://localhost:8405").rstrip("/")


def _load_leads(request: HttpRequest) -> tuple[List[Dict[str, Any]], str]:
    try:
        enquiries = services.list_enquiries(_api_root(), request=request)
    except services.EnquiryServiceError as exc:
        logger.warning("Marketing: could not load enquiries: %s", exc)
        return [], f"Could not load enquiries from the enquiry service: {exc}"
    return lost.build_lost_leads(enquiries), ""


@login_required
def board(request: HttpRequest) -> HttpResponse:
    leads, error = _load_leads(request)
    rows, chosen = lost.filter_leads(leads, request.GET)
    context = {
        "rows": rows,
        "summary": lost.summarise(leads),
        "filtered_count": len(rows),
        "chosen": chosen,
        "has_filters": any(chosen.values()),
        "kind_choices": list(lost.KIND_LABELS.items()),
        "stage_choices": list(lost.STAGE_LABELS.items()),
        "reason_choices": list(MarketingLead.REASON_CHOICES),
        "fin_years": sorted({row["fin_year"] for row in leads if row["fin_year"]}, reverse=True),
        "wait_days": lost.wait_days(),
        "load_error": error,
        # Same filters for a new campaign (its audience is always "Ready to contact").
        "campaign_query": urlencode({k: v for k, v in chosen.items() if v and k != "stage"}),
    }
    return render(request, "marketing/board.html", context)


def _find_lead(request: HttpRequest, enquiry_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        enquiry = services.fetch_enquiry(_api_root(), enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Could not load enquiry {enquiry_id}: {exc}")
        return None, None
    rows = lost.build_lost_leads([enquiry])
    return enquiry, (rows[0] if rows else None)


def _log(lead: MarketingLead, user, kind: str, text: str) -> None:
    MarketingActivity.objects.create(lead=lead, kind=kind, text=text, created_by=user)


def _revive(request: HttpRequest, enquiry: Dict[str, Any], row: Dict[str, Any], lead: MarketingLead) -> Optional[str]:
    """Create a new enquiry linked to the lost one. Returns the new enquiry id, or None."""
    if lead.revived_enquiry_id:
        messages.info(request, f"Already revived as enquiry {lead.revived_enquiry_id}.")
        return None
    if lead.do_not_contact:
        messages.error(request, "This customer is marked Do not contact, so it cannot be revived.")
        return None
    note = (request.POST.get("revive_note") or "").strip()
    old_id = row["enquiry_id"]
    original_salesperson = (enquiry.get("assigned_to") or "").strip()
    salesperson = None
    chosen = (request.POST.get("assign_to") or "").strip()
    if chosen:
        salesperson = get_user_model().objects.filter(pk=chosen, is_active=True).first() if chosen.isdigit() else None
        if salesperson is None:
            messages.error(request, "Choose a salesperson from the list.")
            return None
    # Keep the original wording when the same person is chosen; otherwise use the chosen person's name.
    if salesperson is not None and notify.find_user(original_salesperson) != salesperson:
        assigned_to = notify.display_name(salesperson)
    else:
        assigned_to = original_salesperson
    description = f"Revived by marketing from lost enquiry {old_id} ({row['kind_label']})."
    if note:
        description += f"\n\n{note}"
    values = {
        # A name marketing corrected wins over a name that is really an email subject.
        "customer_name": (lead.customer_name or enquiry.get("customer_name") or row["customer_name"]
                          or "Unknown customer")[:255],
        "subject": enquiry.get("subject") or row["subject"] or f"Revived enquiry {old_id}",
        "channel": "other",
        "priority": "medium",
        "description": description,
        "customer_reference": enquiry.get("customer_reference") or "",
        "project_name": enquiry.get("project_name") or None,
        "assigned_to": assigned_to or None,
        "remarks": f"Revived from enquiry {old_id}.",
    }
    try:
        created = services.create_enquiry(_api_root(), values, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Could not create the new enquiry: {exc}")
        return None
    new_id = str(created.get("enquiry_id") or "").strip()
    if not new_id:
        messages.error(request, "The enquiry service did not return the new enquiry number.")
        return None
    with transaction.atomic():
        lead.revived_enquiry_id = new_id
        lead.revived_at = timezone.now()
        lead.revived_by = request.user
        lead.updated_by = request.user
        lead.save()
        _log(lead, request.user, MarketingActivity.KIND_SYSTEM,
             f"Revived as new enquiry {new_id}" + (f", assigned to {assigned_to}" if assigned_to else "") + "." +
             (f" {note}" if note else ""))
        if row["customer_email"]:
            EnquiryContact.objects.get_or_create(enquiry_id=new_id,
                                                 defaults={"customer_email": row["customer_email"],
                                                           "other_customer_emails": ", ".join(row["other_emails"]),
                                                           "updated_by": request.user})

    # Tell the salesperson. A problem here never undoes the revive; it is shown and logged.
    done, problems = notify.notify_revived(
        sender=request.user, salesperson=salesperson, customer=row["customer_name"], old_id=old_id,
        new_id=new_id, note=note, enquiry_url=request.build_absolute_uri(reverse("enquiries:detail", args=[new_id])))
    if salesperson is not None:
        name = notify.display_name(salesperson)
        if done:
            _log(lead, request.user, MarketingActivity.KIND_SYSTEM, f"{name} was notified by {' and '.join(done)}.")
        elif not problems:
            problems = [f"{name} was not notified (phone notifications are not set up and email notices are off), "
                        "so please tell them yourself"]
    for problem in problems:
        _log(lead, request.user, MarketingActivity.KIND_SYSTEM, f"Notice: {problem}.")
        messages.warning(request, f"Revived, but {problem}.")
    return new_id


def _handle_post(request: HttpRequest, enquiry: Dict[str, Any], row: Dict[str, Any]) -> HttpResponse:
    enquiry_id = row["enquiry_id"]
    action = request.POST.get("action") or ""
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    detail_url = reverse("marketing:detail", args=[enquiry_id])

    if action == "add_note":
        kind = request.POST.get("kind") or MarketingActivity.KIND_NOTE
        if kind not in {MarketingActivity.KIND_NOTE, MarketingActivity.KIND_CALL, MarketingActivity.KIND_EMAIL}:
            kind = MarketingActivity.KIND_NOTE
        text = (request.POST.get("text") or "").strip()
        if not text:
            messages.error(request, "Write something before saving the note.")
        else:
            _log(lead, request.user, kind, text[:5000])
            messages.success(request, "Note saved.")
    elif action == "set_reason":
        reason = (request.POST.get("lost_reason") or "").strip()
        if reason and reason not in lost.REASON_LABELS:
            messages.error(request, "Choose a reason from the list.")
        elif reason != lead.lost_reason:
            lead.lost_reason = reason
            lead.updated_by = request.user
            lead.save(update_fields=["lost_reason", "updated_by", "updated_at"])
            _log(lead, request.user, MarketingActivity.KIND_SYSTEM,
                 f"Reason set to: {lost.REASON_LABELS.get(reason, 'not recorded')}.")
            messages.success(request, "Reason saved.")
    elif action == "set_customer_name":
        name = " ".join((request.POST.get("customer_name") or "").split())[:255]
        if name != lead.customer_name:
            lead.customer_name = name
            lead.updated_by = request.user
            lead.save(update_fields=["customer_name", "updated_by", "updated_at"])
            _log(lead, request.user, MarketingActivity.KIND_SYSTEM,
                 f"Customer name set to: {name}." if name else "Customer name cleared.")
            messages.success(request, "Customer name saved.")
    elif action == "toggle_do_not_contact":
        lead.do_not_contact = not lead.do_not_contact
        lead.updated_by = request.user
        lead.save(update_fields=["do_not_contact", "updated_by", "updated_at"])
        text = "Marked Do not contact." if lead.do_not_contact else "Do not contact removed."
        _log(lead, request.user, MarketingActivity.KIND_SYSTEM, text)
        messages.success(request, text)
    elif action == "revive":
        new_id = _revive(request, enquiry, row, lead)
        if new_id:
            messages.success(request, f"Revived. New enquiry {new_id} was created for sales.")
    else:
        messages.error(request, "Unknown action.")
    return redirect(detail_url)


@login_required
def detail(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    enquiry, row = _find_lead(request, enquiry_id)
    if enquiry is None:
        return redirect("marketing:board")
    if row is None:
        messages.info(request, f"Enquiry {enquiry_id} is not Rejected or No response, so it is not in Marketing.")
        return redirect("marketing:board")
    if request.method == "POST":
        return _handle_post(request, enquiry, row)

    lead = row["lead"]
    context = {
        "row": row,
        "enquiry": enquiry,
        "activities": list(lead.activities.select_related("created_by")) if lead else [],
        "follow_ups": list(FollowUpSent.objects.filter(enquiry_id=enquiry_id).order_by("-sent_at")),
        "campaign_emails": list(CampaignRecipient.objects.filter(enquiry_id=enquiry_id)
                                .select_related("campaign").order_by("-created_at")),
        "reason_choices": list(MarketingLead.REASON_CHOICES),
        "note_kinds": [(k, v) for k, v in MarketingActivity.KIND_CHOICES if k != MarketingActivity.KIND_SYSTEM],
        "can_revive": row["stage"] not in (lost.STAGE_REVIVED, lost.STAGE_DO_NOT_CONTACT),
        "team": notify.team_members(),
        "matched_salesperson": notify.find_user(enquiry.get("assigned_to") or ""),
        "wait_days": lost.wait_days(),
    }
    return render(request, "marketing/detail.html", context)
