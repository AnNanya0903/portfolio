"""Quotation and feasibility portal: list, new quotation, detail with versions and tracker, decisions, files."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from kyc import links as kyc_links

from . import enquiry_link, mailer, roles, sending, threads, workflow
from .forms import ChangeEnquiryForm, DecisionForm, QuotationForm, VersionForm
from .models import EnquiryLinkSuggestion, Quotation, QuotationEvent, QuotationVersion

PAGE_SIZE = 25


def _denied(request: HttpRequest, what: str, quotation=None) -> HttpResponse:
    messages.error(request, f"You do not have access to {what}.")
    return redirect("quotations:detail", pk=quotation.pk) if quotation else redirect("quotations:list")


def _forbidden(what: str) -> HttpResponse:
    return HttpResponseForbidden(f"You do not have access to {what}.")


def _submit_now(request: HttpRequest) -> bool:
    """"Save as draft" or "Submit for approval" (the default, as before drafts existed)."""
    return (request.POST.get("action") or "submit") != "draft"


def timeline_for(quotation: Quotation):
    """The quotation's events, with the events of its enquiry's email thread (email linked)."""
    rows = QuotationEvent.objects.filter(Q(quotation=quotation) | Q(quotation__isnull=True,
                                                                     enquiry_id=quotation.enquiry_id or "-"))
    return list(rows.select_related("user", "version", "quotation"))


@login_required
def quotation_list(request: HttpRequest) -> HttpResponse:
    q = " ".join((request.GET.get("q") or "").split())
    status = request.GET.get("status") or ""
    customer_code = kyc_links.normalize_code(request.GET.get("customer"))
    rows = Quotation.objects.select_related("customer")
    if q:
        rows = rows.filter(Q(number__icontains=q) | Q(customer_name__icontains=q) | Q(title__icontains=q)
                           | Q(customer__code__icontains=q) | Q(enquiry_id__icontains=q)
                           | Q(enquiry_reference__icontains=q))
    if customer_code:
        rows = rows.filter(customer__code=customer_code)
    counts = {key: rows.filter(status=key).count() for key, _ in Quotation.STATUS_CHOICES}
    total = rows.count()
    if status in counts:
        rows = rows.filter(status=status)
    ready = status == Quotation.STATUS_APPROVED
    # The ready-to-send list: the oldest approval first, so nothing waits too long.
    ordered = rows.order_by("updated_at", "id") if ready else rows.order_by("-updated_at", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))
    if ready:
        for row in page:
            row.ready_version = row.active_version
    return render(request, "quotations/list.html", {
        "page": page, "q": q, "status": status, "customer_code": customer_code, "total": total, "ready": ready,
        "tabs": [{"key": key, "label": label, "count": counts[key]} for key, label in Quotation.STATUS_CHOICES
                 if key not in (Quotation.STATUS_ACCEPTED, Quotation.STATUS_DECLINED)],
        "can_cost": roles.can_cost(request.user),
        "can_approve": roles.can_approve(request.user),
        "can_send": roles.can_send(request.user),
    })


@login_required
def quotation_new(request: HttpRequest) -> HttpResponse:
    if not roles.can_cost(request.user):
        return _denied(request, "uploading quotations")
    if request.method == "POST":
        form = QuotationForm(request.POST, request.FILES, request=request)
        if form.is_valid():
            data = form.cleaned_data
            quotation = workflow.create_quotation(
                customer=getattr(form, "customer", None), customer_name=data["customer_name"], title=data["title"],
                enquiry_id=data.get("enquiry_id") or "", enquiry_reference=data.get("enquiry_reference") or "",
                pdf=data["pdf"], excel=data.get("excel"),
                price_note=data.get("price_note") or "", note=data.get("note") or "", user=request.user,
                submit=_submit_now(request))
            if quotation.status == Quotation.STATUS_DRAFT:
                messages.success(request, f"{quotation.number} saved as a draft. Submit it for approval when ready.")
            else:
                messages.success(request, f"{quotation.number} uploaded. It is now waiting for management approval.")
            return redirect("quotations:detail", pk=quotation.pk)
    else:
        form = QuotationForm(initial={
            "customer_code": kyc_links.normalize_code(request.GET.get("customer")),
            "customer_name": (request.GET.get("customer_name") or "")[:255],
            "enquiry_id": enquiry_link.normalize(request.GET.get("enquiry")),
            "enquiry_reference": (request.GET.get("reference") or "")[:120],
        })
    return render(request, "quotations/new.html", {"form": form})


@login_required
def enquiry_search(request: HttpRequest) -> JsonResponse:
    """The "Enquiry number" picker: enquiries matching what was typed (number, customer, subject)."""
    try:
        rows = enquiry_link.search(request.GET.get("q") or "", request=request)
    except enquiry_link.EnquiryUnavailable as exc:
        return JsonResponse({"enquiries": [], "error": str(exc)}, status=503)
    return JsonResponse({"enquiries": [{**row, "label": enquiry_link.label(row)} for row in rows]})


@login_required
@require_POST
def change_enquiry(request: HttpRequest, pk: int) -> HttpResponse:
    """Link the quotation to its enquiry, or to another one. CRO / Customer Service and administrators."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_link_enquiry(request.user):
        return _forbidden("linking quotations to enquiries")
    form = ChangeEnquiryForm(request.POST, request=request)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect("quotations:detail", pk=pk)
    new = form.cleaned_data["enquiry_id"]
    if not new and request.POST.get("action") != "unlink":
        messages.error(request, "Pick the enquiry this quotation answers.")
        return redirect("quotations:detail", pk=pk)
    old = quotation.enquiry_id
    if enquiry_link.change_enquiry(quotation, new, user=request.user):
        messages.success(request, f"{quotation.number} is now linked to {new}." if new
                         else f"{quotation.number} is no longer linked to {old}.")
    else:
        messages.info(request, f"{quotation.number} was already linked to {new or 'no enquiry'}.")
    nxt = request.POST.get("next") or ""
    if nxt == "send" and new:
        return redirect(reverse("quotations:detail", args=[pk]) + "?send=1")
    return redirect("quotations:detail", pk=pk)


def _detail_context(request: HttpRequest, quotation: Quotation) -> dict:
    sees_workings = roles.sees_workings(request.user)
    latest = quotation.latest_version
    can_send = roles.can_send(request.user)
    context = {
        "quotation": quotation,
        "active": quotation.active_version,
        "latest": latest,
        "sees_workings": sees_workings,
        "can_cost": roles.can_cost(request.user),
        "can_approve": roles.can_approve(request.user),
        "can_send": can_send,
        "awaiting_decision": latest is not None and latest.status == QuotationVersion.STATUS_PENDING,
        "is_draft": latest is not None and latest.status == QuotationVersion.STATUS_DRAFT,
        # The audit trail is open to every department.
        "events": timeline_for(quotation),
        "thread": threads.contact_for(quotation.enquiry_id),
    }
    can_link = roles.can_link_enquiry(request.user)
    context["can_link_enquiry"] = can_link
    if can_link:
        context["change_enquiry_form"] = ChangeEnquiryForm(initial={"enquiry_id": quotation.enquiry_id})
    if can_send and quotation.status in (Quotation.STATUS_APPROVED, Quotation.STATUS_SENT):
        if quotation.enquiry_id:
            context["send"] = sending.preview(quotation)
            context["send_from"] = mailer.sender_header()
            context["send_resend"] = quotation.status == Quotation.STATUS_SENT
            context["open_send"] = request.method == "GET" and request.GET.get("send") == "1"
        else:
            # "Send to customer" first asks for the enquiry, then opens the send box.
            context["needs_enquiry_first"] = True
            context["open_enquiry"] = request.method == "GET" and request.GET.get("send") == "1"
        context["password_failed"] = sending.password_failed(quotation)
    if sees_workings:
        context.update({
            "versions": list(quotation.versions.select_related("uploaded_by", "decided_by")),
            "version_form": VersionForm(),
            "decision_form": DecisionForm(),
        })
    return context


@login_required
def quotation_detail(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation.objects.select_related("customer", "created_by"), pk=pk)
    return render(request, "quotations/detail.html", _detail_context(request, quotation))


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
                                   user=request.user, submit=_submit_now(request))
    if version.status == QuotationVersion.STATUS_DRAFT:
        messages.success(request, f"Version {version.number} saved as a draft.")
    else:
        messages.success(request, f"Version {version.number} uploaded. It is now waiting for management approval.")
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def submit(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_cost(request.user):
        return _forbidden("submitting quotations")
    try:
        version = workflow.submit(quotation, user=request.user)
        messages.success(request, f"Version {version.number} submitted. It is now waiting for management approval.")
    except workflow.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def decide(request: HttpRequest, pk: int) -> HttpResponse:
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_approve(request.user):
        return _forbidden("approving quotations")
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


def _send(request: HttpRequest, pk: int, *, resend: bool) -> HttpResponse:
    quotation = get_object_or_404(Quotation.objects.select_related("customer", "created_by"), pk=pk)
    if not roles.can_send(request.user):
        return _forbidden("sending quotations to customers")
    post = request.POST
    try:
        sent = sending.send(quotation, user=request.user, to=post.get("to"), cc=post.get("cc"),
                            subject=post.get("subject") or "", body=post.get("body") or "", resend=resend,
                            base_url=request.build_absolute_uri("/").rstrip("/"))
    except sending.SendError as exc:
        # Show the send box again, as typed, with the reason.
        quotation.refresh_from_db()
        context = _detail_context(request, quotation)
        if "send" in context:
            draft = context["send"]
            draft.to, draft.cc = [post.get("to") or ""], [post.get("cc") or ""]
            draft.body = post.get("body") or draft.body
            if not draft.subject_locked:
                draft.subject = post.get("subject") or draft.subject
        context.update({"send_error": str(exc), "open_send": True})
        messages.error(request, str(exc))
        return render(request, "quotations/detail.html", context, status=exc.status)
    where = "in the customer's email thread" if sent.references else "as a new email"
    if sent.local_demo:
        messages.success(request, f"Test mode: {quotation.number} was prepared {where} but not sent.")
    else:
        messages.success(request, f"{quotation.number} {'sent again' if resend else 'sent'} to the customer {where}.")
    if sent.password_error:
        messages.warning(request, f"The quotation email went out, but the separate email with the link's password "
                                  f"did not: {sent.password_error} Use “Send the password again”.")
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def send_password(request: HttpRequest, pk: int) -> HttpResponse:
    """The password email failed: give the link a new password and email it, in the same thread. CRO only."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_send(request.user):
        return _forbidden("sending quotations to customers")
    try:
        sending.resend_password(quotation, user=request.user)
        messages.success(request, f"A new password for the {quotation.number} link was emailed to the customer.")
    except sending.SendError as exc:
        messages.error(request, str(exc))
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def send(request: HttpRequest, pk: int) -> HttpResponse:
    return _send(request, pk, resend=False)


@login_required
@require_POST
def resend(request: HttpRequest, pk: int) -> HttpResponse:
    return _send(request, pk, resend=True)


@login_required
@require_POST
def respond(request: HttpRequest, pk: int) -> HttpResponse:
    """The CRO records the customer's answer: accepted (PO / reference, note) or declined (reason)."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_send(request.user):
        return _forbidden("recording the customer's answer")
    answer = request.POST.get("answer")
    try:
        if answer == "accepted":
            workflow.accept(quotation, user=request.user, reference=request.POST.get("reference") or "",
                            note=request.POST.get("note") or "")
            messages.success(request, f"{quotation.number}: customer accepted. You can now create the proforma invoice.")
        elif answer == "declined":
            workflow.decline(quotation, user=request.user, reason=request.POST.get("reason") or "")
            messages.success(request, f"{quotation.number}: customer declined.")
        else:
            messages.error(request, "Choose accepted or declined.")
    except workflow.WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("quotations:detail", pk=pk)


@login_required
def proforma(request: HttpRequest, pk: int) -> HttpResponse:
    """The proforma invoice hook. The PI flow is not built here yet: it opens once the customer accepted."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if not quotation.proforma_unlocked:
        messages.error(request, "The proforma invoice opens once the customer has accepted the quotation.")
        return redirect("quotations:detail", pk=pk)
    messages.info(request, "Proforma invoices are made from the enquiry for now. It will reply in the same email "
                           "thread as the quotation.")
    if quotation.enquiry_id:
        return redirect("enquiries:detail", quotation.enquiry_id)
    return redirect("quotations:detail", pk=pk)


@login_required
@require_POST
def mark_sent(request: HttpRequest, pk: int) -> HttpResponse:
    """Record a quotation sent outside the portal (e.g. by hand from Gmail). CRO only."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if not roles.can_send(request.user):
        return _forbidden("sending quotations to customers")
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
    stored += [v.frozen_pdf for v in quotation.versions.all() if v.frozen_pdf]
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
    if version.frozen_pdf:  # the immutable approved copy: exactly what the customer receives
        return _serve(version.frozen_pdf, f"{version.quotation.number}.pdf", "application/pdf", inline=True)
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


@login_required
def enquiry_suggestions(request: HttpRequest) -> HttpResponse:
    """Administrators: confirm the enquiry suggested for each older quotation, pick another, or reject it.
    Suggestions are made by `manage.py suggest_quotation_enquiries`; nothing is linked without a click here."""
    if not roles.is_admin(request.user):
        return _denied(request, "the enquiry suggestions")
    if request.method == "POST":
        suggestion = get_object_or_404(EnquiryLinkSuggestion.objects.select_related("quotation"),
                                       pk=request.POST.get("suggestion") or 0)
        try:
            if request.POST.get("action") == "reject":
                enquiry_link.reject_suggestion(suggestion, user=request.user)
                messages.success(request, f"{suggestion.quotation.number}: suggestion rejected; it stays unlinked.")
            else:
                picked = ""
                if request.POST.get("action") == "pick":
                    picked = " ".join((request.POST.get("enquiry_id") or "").split()).split(" ", 1)[0]
                    if not picked:
                        raise enquiry_link.LinkError("Type or pick the enquiry number to link.")
                number = enquiry_link.confirm_suggestion(suggestion, user=request.user, enquiry_id=picked,
                                                         request=request)
                messages.success(request, f"{suggestion.quotation.number} is now linked to {number}.")
        except enquiry_link.LinkError as exc:
            messages.error(request, f"{suggestion.quotation.number}: {exc}")
        return redirect(reverse("quotations:enquiry_suggestions") + (f"?status={request.POST.get('status')}"
                                                                      if request.POST.get("status") else ""))
    status = request.GET.get("status") or EnquiryLinkSuggestion.STATUS_PENDING
    rows = EnquiryLinkSuggestion.objects.select_related("quotation", "decided_by")
    counts = {key: rows.filter(status=key).count() for key, _ in EnquiryLinkSuggestion.STATUS_CHOICES}
    if status in counts:
        rows = rows.filter(status=status)
    page = Paginator(rows.order_by("quotation__number"), 50).get_page(request.GET.get("page"))
    return render(request, "quotations/enquiry_suggestions.html", {
        "page": page, "status": status,
        "tabs": [{"key": k, "label": label, "count": counts[k]} for k, label in EnquiryLinkSuggestion.STATUS_CHOICES],
        "unlinked": Quotation.objects.filter(enquiry_id="").count(),
    })
