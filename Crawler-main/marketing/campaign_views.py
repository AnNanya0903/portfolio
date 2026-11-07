"""Campaign pages: list, create / edit a draft, preview who gets it, send, and check replies."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import campaigns, lost
from .forms import CampaignForm
from .models import Campaign, CampaignRecipient
from .views import _load_leads


@login_required
def campaign_list(request: HttpRequest) -> HttpResponse:
    rows = []
    for campaign in Campaign.objects.select_related("created_by", "sent_by"):
        recipients = list(campaign.recipients.all())
        rows.append({
            "campaign": campaign,
            "total": len(recipients),
            "sent": sum(1 for r in recipients if r.status == CampaignRecipient.STATUS_SENT),
            "test": sum(1 for r in recipients if r.status == CampaignRecipient.STATUS_DRY_RUN),
            "failed": sum(1 for r in recipients if r.status == CampaignRecipient.STATUS_FAILED),
            "replied": sum(1 for r in recipients if r.replied_at),
        })
    return render(request, "marketing/campaign_list.html", {"rows": rows, "dry_run": campaigns.dry_run()})


def _form_page(request: HttpRequest, form: CampaignForm, campaign=None) -> HttpResponse:
    return render(request, "marketing/campaign_form.html", {
        "form": form,
        "audience_fields": [form[name] for name in ("kind", "reason", "fin_year", "q")],
        "campaign": campaign,
        "templates": campaigns.STARTER_TEMPLATES,
        "placeholders": campaigns.PLACEHOLDERS,
        "opt_out_line": campaigns.OPT_OUT_LINE,
    })


def _save(form: CampaignForm, campaign: Campaign) -> Campaign:
    data = form.cleaned_data
    campaign.name = data["name"].strip()
    campaign.subject = data["subject"].strip()
    campaign.body = data["body"]
    campaign.filters = campaigns.clean_filters(data)
    campaign.save()
    return campaign


@login_required
def campaign_new(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = CampaignForm(request.POST)
        if form.is_valid():
            campaign = _save(form, Campaign(created_by=request.user))
            messages.success(request, "Draft saved. Check who will get it before sending.")
            return redirect("marketing:campaign_detail", pk=campaign.pk)
        return _form_page(request, form)
    initial = campaigns.clean_filters(request.GET)
    starter = campaigns.STARTER_TEMPLATES["checking_in"]
    initial.update({"subject": starter["subject"], "body": starter["body"]})
    return _form_page(request, CampaignForm(initial=initial))


@login_required
def campaign_edit(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = get_object_or_404(Campaign, pk=pk)
    if campaign.status != Campaign.STATUS_DRAFT:
        messages.info(request, "A sent campaign cannot be changed.")
        return redirect("marketing:campaign_detail", pk=pk)
    if request.method == "POST":
        form = CampaignForm(request.POST)
        if form.is_valid():
            _save(form, campaign)
            messages.success(request, "Draft updated.")
            return redirect("marketing:campaign_detail", pk=pk)
        return _form_page(request, form, campaign)
    initial = {"name": campaign.name, "subject": campaign.subject, "body": campaign.body, **campaign.filters}
    return _form_page(request, CampaignForm(initial=initial), campaign)


@login_required
def campaign_detail(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = get_object_or_404(Campaign.objects.select_related("created_by", "sent_by"), pk=pk)
    context = {
        "campaign": campaign,
        "dry_run": campaigns.dry_run(),
        "max_recipients": campaigns.max_recipients(),
        "gap_days": campaigns.gap_days(),
        "wait_days": lost.wait_days(),
        "filter_labels": _filter_labels(campaign.filters),
    }
    if campaign.status == Campaign.STATUS_DRAFT:
        leads, error = _load_leads(request)
        audience = campaigns.build_audience(leads, campaign.filters)
        preview = None
        if audience["recipients"]:
            subject, body = campaigns.render_email(campaign.subject, campaign.body, audience["recipients"][0]["row"])
            preview = {"to": audience["recipients"][0]["email"], "subject": subject, "body": body}
        context.update({
            "audience": audience,
            "preview": preview,
            "load_error": error,
            "too_many": len(audience["recipients"]) > campaigns.max_recipients(),
        })
    else:
        recipients = list(campaign.recipients.all())
        context.update({
            "recipients": recipients,
            "can_retry": (not campaigns.dry_run()) and any(
                r.status in (CampaignRecipient.STATUS_FAILED, CampaignRecipient.STATUS_DRY_RUN) for r in recipients),
            "sent_count": sum(1 for r in recipients if r.status == CampaignRecipient.STATUS_SENT),
            "replied_count": sum(1 for r in recipients if r.replied_at),
        })
    return render(request, "marketing/campaign_detail.html", context)


def _filter_labels(filters):
    labels = []
    if filters.get("kind"):
        labels.append(lost.KIND_LABELS.get(filters["kind"], filters["kind"]))
    if filters.get("reason"):
        labels.append("Reason not recorded" if filters["reason"] == "none"
                      else lost.REASON_LABELS.get(filters["reason"], filters["reason"]))
    if filters.get("fin_year"):
        labels.append(f"FY {filters['fin_year']}")
    if filters.get("q"):
        labels.append(f"Search: {filters['q']}")
    return labels or ["All lost enquiries"]


@login_required
@require_POST
def campaign_send(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = get_object_or_404(Campaign, pk=pk)
    expected = None
    if campaign.status == Campaign.STATUS_DRAFT:
        try:
            expected = int(request.POST.get("expected_count") or "")
        except ValueError:
            messages.error(request, "Please check the list of customers before sending.")
            return redirect("marketing:campaign_detail", pk=pk)
    leads, error = ([], "") if campaign.status != Campaign.STATUS_DRAFT else _load_leads(request)
    if error:
        messages.error(request, error)
        return redirect("marketing:campaign_detail", pk=pk)
    try:
        counts = campaigns.send_campaign(campaign, request.user, leads, expected_count=expected)
    except campaigns.CampaignError as exc:
        messages.error(request, str(exc))
        return redirect("marketing:campaign_detail", pk=pk)
    parts = []
    if counts["sent"]:
        parts.append(f"{counts['sent']} sent")
    if counts["dry_run"]:
        parts.append(f"{counts['dry_run']} prepared in test mode (not sent)")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} stopped by a last check")
    level = messages.warning if counts["failed"] or counts["skipped"] else messages.success
    level(request, "Campaign: " + (", ".join(parts) or "nothing to send") + ".")
    return redirect("marketing:campaign_detail", pk=pk)


@login_required
@require_POST
def campaign_check_replies(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = get_object_or_404(Campaign, pk=pk)
    try:
        counts = campaigns.check_replies(campaign, request.user)
    except campaigns.CampaignError as exc:
        messages.error(request, str(exc))
    else:
        text = f"Checked {counts['checked']} emails: {counts['replied']} new replies."
        if counts["unsubscribed"]:
            text += f" {counts['unsubscribed']} asked to unsubscribe and are now Do not contact."
        messages.success(request, text)
    return redirect("marketing:campaign_detail", pk=pk)


@login_required
@require_POST
def campaign_delete(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = get_object_or_404(Campaign, pk=pk)
    if campaign.status != Campaign.STATUS_DRAFT:
        messages.error(request, "Only drafts can be deleted. Sent campaigns are kept as a record.")
        return redirect("marketing:campaign_detail", pk=pk)
    campaign.delete()
    messages.success(request, "Draft deleted.")
    return redirect("marketing:campaign_list")
