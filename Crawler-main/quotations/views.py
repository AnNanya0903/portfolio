"""Quotation and feasibility portal: list, new quotation, detail with versions and tracker, decisions, files."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from kyc import links as kyc_links

from . import roles, workflow
from .forms import DecisionForm, QuotationForm, VersionForm
from .models import Quotation, QuotationEvent, QuotationVersion

PAGE_SIZE = 25


def _denied(request: HttpRequest, what: str, quotation=None) -> HttpResponse:
    messages.error(request, f"You do not have access to {what}.")
    return redirect("quotations:detail", pk=quotation.pk) if quotation else redirect("quotations:list")


@login_required
def quotation_list(request: HttpRequest) -> HttpResponse:
    q = " ".join((request.GET.get("q") or "").split())
    status = request.GET.get("status") or ""
    customer_code = kyc_links.normalize_code(request.GET.get("customer"))
    rows = Quotation.objects.select_related("customer")
    if q:
        rows = rows.filter(Q(number__icontains=q) | Q(customer_name__icontains=q) | Q(title__icontains=q)
                           | Q(customer__code__icontains=q) | Q(enquiry_id__icontains=q))
    if customer_code:
        rows = rows.filter(customer__code=customer_code)
    counts = {key: rows.filter(status=key).count() for key, _ in Quotation.STATUS_CHOICES}
    total = rows.count()
    if status in counts:
        rows = rows.filter(status=status)
    page = Paginator(rows.order_by("-updated_at", "-id"), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "quotations/list.html", {
        "page": page, "q": q, "status": status, "customer_code": customer_code, "total": total,
        "tabs": [{"key": key, "label": label, "count": counts[key]} for key, label in Quotation.STATUS_CHOICES],
        "can_cost": roles.can_cost(request.user),
        "can_approve": roles.can_approve(request.user),
    })


@login_required
def quotation_new(request: HttpRequest) -> HttpResponse:
    if not roles.can_cost(request.user):
        return _denied(request, "uploading quotations")
    if request.method == "POST":
        form = QuotationForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            quotation = workflow.create_quotation(
                customer=getattr(form, "customer", None), customer_name=data["customer_name"], title=data["title"],
                enquiry_id=data.get("enquiry_id") or "", pdf=data["pdf"], excel=data.get("excel"),
                price_note=data.get("price_note") or "", note=data.get("note") or "", user=request.user)
            messages.success(request, f"{quotation.number} uploaded. It is now waiting for management approval.")
            return redirect("quotations:detail", pk=quotation.pk)
    else:
        form = QuotationForm(initial={
            "customer_code": kyc_links.normalize_code(request.GET.get("customer")),
            "customer_name": (request.GET.get("customer_name") or "")[:255],
            "enquiry_id": (request.GET.get("enquiry") or "")[:64],
        })
    return render(request, "quotations/new.html", {"form": form})


@login_required
def quotation_detail(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation.objects.select_related("customer", "created_by"), pk=pk)
    sees_workings = roles.sees_workings(request.user)
    latest = quotation.latest_version
    context = {
        "quotation": quotation,
        "active": quotation.active_version,
        "latest": latest,
        "sees_workings": sees_workings,
        "can_cost": roles.can_cost(request.user),
        "can_approve": roles.can_approve(request.user),
        "awaiting_decision": latest is not None and latest.status == QuotationVersion.STATUS_PENDING,
    }
    if sees_workings:
        context.update({
            "versions": list(quotation.versions.select_related("uploaded_by", "decided_by")),
            "events": list(quotation.events.select_related("user", "version")),
            "version_form": VersionForm(),
            "decision_form": DecisionForm(),
        })
    return render(request, "quotations/detail.html", context)


@login_required
@require_POST
def version_new(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_cost(request.user):
        return _denied(request, "uploading quotations", quotation)
    form = VersionForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect("quotations:detail", pk=pk)
    data = form.cleaned_data
    version = workflow.add_version(quotation, pdf=data["pdf"], excel=data.get("excel"),
                                   price_note=data.get("price_note") or "", note=data.get("note") or "",
                                   user=request.user)
    messages.success(request, f"Version {version.number} uploaded. It is now waiting for management approval.")
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def decide(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_approve(request.user):
        return _denied(request, "approving quotations", quotation)
    form = DecisionForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect("quotations:detail", pk=pk)
    data = form.cleaned_data
    version_id = int(request.POST["version"]) if (request.POST.get("version") or "").isdigit() else None
    try:
        if data["decision"] == DecisionForm.APPROVE:
            version = workflow.approve(quotation, user=request.user, comment=data.get("comment") or "",
                                       version_id=version_id)
            messages.success(request, f"Version {version.number} approved. It is now the PDF to send to the customer.")
        else:
            version = workflow.request_revision(quotation, user=request.user, comment=data.get("comment") or "",
                                                corrected_excel=data.get("corrected_excel"), version_id=version_id)
            messages.success(request, f"Version {version.number} sent back for revision.")
    except workflow.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def mark_sent(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation, pk=pk)
    try:
        version = workflow.mark_sent(quotation, user=request.user)
        messages.success(request, f"Recorded: version {version.number} was sent to the customer.")
    except workflow.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("quotations:detail", pk=pk)


logger = logging.getLogger(__name__)


@require_POST
def quotation_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a quotation for good: its versions, approval history and every stored file go with it.

    Only users who can upload quotations may delete one. The quotation number must be typed to confirm.
    The KYC customer and the enquiry it belongs to are not touched.
    """
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_cost(request.user):
        return _denied(request, "delete a quotation", quotation)
    typed = "".join((request.POST.get("confirm_number") or "").split()).upper()
    if typed != (quotation.number or "").upper():
        messages.error(request, f"Not deleted. Type the quotation number {quotation.number} exactly to confirm.")
        return redirect("quotations:detail", pk=pk)
    label = f"{quotation.number} - {quotation.title}"
    stored = [v.pdf for v in quotation.versions.all() if v.pdf] + [v.excel for v in quotation.versions.all() if v.excel]
    stored += [e.file for e in quotation.events.all() if e.file]
    with transaction.atomic():
        quotation.delete()
    for field in stored:  # after the records are gone; a storage problem must not bring them back
        try:
            field.delete(save=False)
        except Exception:
            logger.warning("Quotations: could not remove the stored file %s of deleted quotation %s", field.name, label)
    logger.info("Quotation %s deleted by %s", label, request.user)
    messages.success(request, f"Quotation {label} was deleted.")
    return redirect("quotations:list")


def _serve(field, name: str, content_type: str, *, inline: bool = False) -> HttpResponse:
    if not field:
        raise Http404()
    try:
        handle = field.open("rb")
    except Exception as exc:
        raise Http404() from exc
    response = FileResponse(handle, content_type=content_type, filename=name, as_attachment=not inline)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def version_pdf(request: HttpRequest, version_id: int) -> HttpResponse:
    """The PDF. Without costing or approval access only the active (latest, approved) PDF opens."""
    version = get_object_or_404(QuotationVersion.objects.select_related("quotation"), pk=version_id)
    if not roles.sees_workings(request.user):
        active = version.quotation.active_version
        if active is None or active.pk != version.pk:
            raise Http404()
    name = version.pdf_name or f"{version.quotation.number}-v{version.number}.pdf"
    return _serve(version.pdf, name, "application/pdf", inline=True)


@login_required
def version_excel(request: HttpRequest, version_id: int) -> HttpResponse:
    if not roles.sees_workings(request.user):
        raise Http404()
    version = get_object_or_404(QuotationVersion, pk=version_id)
    return _serve(version.excel, version.excel_name or "working.xlsx", "application/octet-stream")


@login_required
def event_file(request: HttpRequest, event_id: int) -> HttpResponse:
    if not roles.sees_workings(request.user):
        raise Http404()
    event = get_object_or_404(QuotationEvent, pk=event_id)
    return _serve(event.file, event.file_name or "corrected.xlsx", "application/octet-stream")
