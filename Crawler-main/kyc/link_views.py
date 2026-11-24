"""Staff pages for KYC by shareable link: the dashboard, sending, the review screen and filling in on behalf.

Every view here is behind the KYC module access check (kyc/urls.py); the finer rules (who may send, review or
reveal) are in kyc/link_requests.py.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import link_fields, link_mail, link_pdf
from . import link_requests as svc
from .link_requests import Actor, KycLinkError
from .link_security import client_ip, mask
from .departments import log_audit
from .models import Customer, KycAuditLog, KycRequest, KycRequestDocument


def _actor(request: HttpRequest) -> Actor:
    return Actor.staff(request.user, client_ip(request))


def _json_body(request: HttpRequest) -> Dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _flash_issued(request: HttpRequest, issued: svc.Issued, what: str) -> None:
    result = issued.send
    if result is None:
        return
    if result.status == link_mail.SENT:
        messages.success(request, f"{what} sent to {issued.request.recipient_email}.")
    elif result.status == link_mail.DRY_RUN:
        # Test mode only: nothing was emailed, so the link is shown once so it can be tried.
        messages.info(request, f"Test mode: the email was not sent. The customer link is {issued.link}")
    else:
        messages.error(request, f"The email was not sent ({result.note}), so the link was closed. "
                                "Fix the problem and use Resend.")


# ----- Dashboard ---------------------------------------------------------------------------------------------------


@require_GET
def request_list(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status", "")
    q = " ".join(request.GET.get("q", "").split())
    rows = KycRequest.objects.select_related("customer", "created_by", "filled_by")
    if status in dict(KycRequest.STATUS_CHOICES):
        rows = rows.filter(status=status)
    if q:
        rows = rows.filter(Q(customer__name__icontains=q) | Q(customer__customer_code__icontains=q)
                           | Q(customer__code__icontains=q) | Q(recipient_email__icontains=q))
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    counts = svc.status_counts()
    return render(request, "kyc/link/list.html", {
        "page": page,
        "status": status,
        "q": q,
        "statuses": [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in KycRequest.STATUS_CHOICES],
        "total": sum(counts.values()),
        "now": timezone.now(),
        "can_send": svc.can_send(request.user),
    })


# ----- Sending -----------------------------------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def request_new(request: HttpRequest, customer_pk: int) -> HttpResponse:
    """POST /kyc/requests/new/<customer>/ creates the request and sends the link."""
    customer = get_object_or_404(Customer, pk=customer_pk)
    choices = svc.recipient_choices(customer)
    existing = svc.active_request_for(customer)
    error = ""
    if request.method == "POST":
        email = (request.POST.get("other_email") or request.POST.get("recipient") or "").strip()
        try:
            issued = svc.create(customer, email, _actor(request), request,
                                payment_terms=request.POST.get("payment_terms", ""))
        except KycLinkError as exc:
            error = str(exc)
        else:
            _flash_issued(request, issued, "KYC link")
            return redirect("kyc:link_detail", pk=issued.request.pk)
    return render(request, "kyc/link/new.html", {
        "customer": customer,
        "choices": choices,
        "existing": existing,
        "error": error,
        "can_send": svc.can_send(request.user),
        "expiry_hours": svc.expiry_hours(),
        "dry_run": link_mail.dry_run(),
        "payment_suggestions": link_fields.PAYMENT_TERMS_SUGGESTIONS,
    })


@require_POST
def request_resend(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        issued = svc.resend(req, _actor(request), request)
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        _flash_issued(request, issued, "A new KYC link")
    return redirect("kyc:link_detail", pk=pk)


@require_POST
def request_revoke(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    if not svc.can_send(request.user):
        messages.error(request, "You cannot revoke KYC links.")
        return redirect("kyc:link_detail", pk=pk)
    try:
        svc.revoke(req, _actor(request))
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The link was revoked. It no longer opens the form.")
    return redirect("kyc:link_detail", pk=pk)


# ----- Review screen ------------------------------------------------------------------------------------------------


def _rows(req: KycRequest):
    details = svc.details_for(req)
    rows = []
    for f in link_fields.FIELDS:
        if f.key == "pan":
            value = mask(details.pan_last4, 10)
        elif f.key == "account_number":
            value = mask(details.account_last4, 12)
        else:
            value = link_fields.display(f, getattr(details, f.key, ""))
        rows.append({"key": f.key, "label": f.label, "step": f.step, "value": value, "sensitive": f.sensitive,
                     "reopened": f.key in (req.rejected_fields or [])})
        if f.key == "contact_phone":
            key = link_fields.EXTRA_CONTACTS
            rows.append({"key": key, "label": link_fields.EXTRA_CONTACTS_LABEL, "step": f.step,
                         "value": "\n".join(link_fields.contact_lines(details.extra_contacts)), "sensitive": False,
                         "reopened": key in (req.rejected_fields or [])})
    return rows


@require_GET
def request_detail(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest.objects.select_related(
        "customer", "created_by", "filled_by", "reviewed_by"), pk=pk)
    svc.expire_if_due(req)
    rows = _rows(req)
    documents = svc._latest_documents(req)
    audit = svc.recent_audit(req)
    for row in audit:
        row.label = row.action.replace("_", " ").capitalize()
    return render(request, "kyc/link/detail.html", {
        "req": req,
        "customer": req.customer,
        "steps": [(n, t, [r for r in rows if r["step"] == n]) for n, t in link_fields.FIELD_STEPS],
        "documents": [d for d in documents if d.doc_type in link_fields.DOCUMENT_TYPES],
        "signed_form": next((d for d in documents if d.doc_type == KycRequestDocument.TYPE_SIGNED), None),
        "generated_form": svc.generated_form(req),
        "form_versions": svc.generated_versions(req),
        "can_set_credit": svc.can_review(request.user) and req.status != KycRequest.STATUS_APPROVED,
        "can_upload_signed": svc.can_upload_signed_form(req) and svc.can_send(request.user),
        "can_set_terms": svc.can_send(request.user) and req.status != KycRequest.STATUS_APPROVED,
        "payment_suggestions": link_fields.PAYMENT_TERMS_SUGGESTIONS,
        "doc_labels": dict(link_fields.DOCUMENTS),
        "missing_docs": [label for key, label in link_fields.DOCUMENTS
                         if key in link_fields.REQUIRED_DOCUMENTS and key not in {d.doc_type for d in documents}],
        "details": svc.details_for(req),
        "audit": audit,
        "can_send": svc.can_send(request.user),
        "can_review": svc.can_review(request.user),
        "can_approve": svc.can_approve(request.user, req),
        "can_reveal": svc.can_reveal(request.user),
        "can_resend": req.status in svc.REISSUABLE,
        "can_fill": req.status in svc.REISSUABLE,
        "reopenable": [(k, link_fields.LABELS[k]) for k in link_fields.REOPENABLE],
        "now": timezone.now(),
        "consent_text": link_fields.CONSENT_TEXT,
    })


@require_POST
def request_review(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    decision = request.POST.get("decision", "")
    try:
        issued = svc.review(req, decision, _actor(request), reason=request.POST.get("reason", ""),
                            fields=request.POST.getlist("fields"), http_request=request)
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        if decision == "approve":
            messages.success(request, "KYC approved. The details are now on the customer's record.")
        else:
            _flash_issued(request, issued, "The rejection with a new link")
    return redirect("kyc:link_detail", pk=pk)


@require_POST
def request_reveal(request: HttpRequest, pk: int) -> JsonResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        value = svc.reveal(req, _json_body(request).get("field", ""), _actor(request))
    except KycLinkError as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    response = JsonResponse({"value": value})
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def document_download(request: HttpRequest, doc_id: int) -> HttpResponse:
    doc = get_object_or_404(KycRequestDocument.objects.select_related("request"), pk=doc_id)
    try:
        content = svc.read_document(doc, _actor(request))
    except FileNotFoundError as exc:
        raise Http404("The file is missing.") from exc
    response = HttpResponse(content, content_type=doc.mime_type)
    ext = doc.storage_key.rsplit(".", 1)[-1]
    response["Content-Disposition"] = f'inline; filename="{doc.doc_type}-{doc.request_id}.{ext}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return response


@require_POST
def request_manual(request: HttpRequest, customer_pk: int) -> HttpResponse:
    """"Fill KYC manually": start a request that is the staff member's own (no link, no email, no OTP)."""
    customer = get_object_or_404(Customer, pk=customer_pk)
    try:
        req = svc.create_manual(customer, _actor(request), payment_terms=request.POST.get("payment_terms", ""))
    except KycLinkError as exc:
        messages.error(request, str(exc))
        existing = svc.active_request_for(customer)
        return redirect("kyc:link_detail", pk=existing.pk) if existing else redirect("kyc:detail", pk=customer.pk)
    return redirect("kyc:link_fill", pk=req.pk)


@require_POST
def request_credit_limit(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        svc.set_credit_limit(req, request.POST.get("credit_limit", ""), _actor(request))
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Credit limit saved.")
    return redirect("kyc:link_detail", pk=pk)


@require_POST
def request_payment_terms(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        svc.set_payment_terms(req, request.POST.get("payment_terms", ""), _actor(request))
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Payment terms saved.")
    return redirect("kyc:link_detail", pk=pk)


def _pdf_file(content: bytes, name: str) -> HttpResponse:
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _stored_pdf(doc, actor) -> Optional[HttpResponse]:
    """The stored registration form (the download is logged), or None when the file is not there any more."""
    if doc is None:
        return None
    try:
        return _pdf_file(svc.read_document(doc, actor), doc.original_name)
    except FileNotFoundError:
        return None


def pdf_response(req: KycRequest, actor) -> HttpResponse:
    """The request's registration form: the stored file, or rendered now when none was stored."""
    stored = _stored_pdf(svc.generated_form(req), actor)
    if stored is not None:
        return stored
    svc.audit(req, "pdf_downloaded", actor, live=True)
    return _pdf_file(link_pdf.render(req), link_pdf.filename(req))


@require_GET
def request_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """Download the Customer Registration Form (stored on submit and on approval) to print, sign and stamp."""
    req = get_object_or_404(KycRequest.objects.select_related("customer"), pk=pk)
    return pdf_response(req, _actor(request))


@require_GET
def request_pdf_version(request: HttpRequest, pk: int, doc_id: int) -> HttpResponse:
    """One stored version of the registration form (older versions are kept)."""
    doc = get_object_or_404(KycRequestDocument, pk=doc_id, request_id=pk, doc_type=KycRequestDocument.TYPE_GENERATED)
    stored = _stored_pdf(doc, _actor(request))
    if stored is None:
        raise Http404("The file is missing.")
    return stored


@require_GET
def customer_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """The customer's Registration Form: the stored one (approved first), or built from the customer record."""
    customer = get_object_or_404(Customer.objects.prefetch_related("contacts", "kyc_requests"), pk=pk)
    log_audit(request.user, customer, KycAuditLog.ACTION_VIEW, ["kyc_form_pdf"])
    stored = _stored_pdf(svc.customer_generated_form(customer), _actor(request))
    if stored is not None:
        return stored
    return _pdf_file(link_pdf.render_customer(customer), link_pdf.customer_filename(customer))


@require_POST
def request_signed_upload(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        svc.upload_signed_form(req, request.FILES.get("file"), _actor(request))
    except KycLinkError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Signed KYC form uploaded.")
    return redirect("kyc:link_detail", pk=pk)


# ----- Fill on behalf of the customer ------------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def request_fill(request: HttpRequest, pk: int) -> HttpResponse:
    """GET shows the form (POST first starts filling, which ends the customer's link)."""
    req = get_object_or_404(KycRequest.objects.select_related("customer"), pk=pk)
    if request.method == "POST":
        try:
            svc.start_staff_fill(req, _actor(request))
        except KycLinkError as exc:
            messages.error(request, str(exc))
            return redirect("kyc:link_detail", pk=pk)
        return redirect("kyc:link_fill", pk=pk)
    if not (req.source == KycRequest.SOURCE_STAFF and req.status == KycRequest.STATUS_IN_PROGRESS
            and req.filled_by_id == request.user.pk):
        messages.info(request, "Use “Fill on behalf of customer” on the request first.")
        return redirect("kyc:link_detail", pk=pk)
    actor = _actor(request)
    return render(request, "kyc/link/fill.html", {
        "req": req,
        "customer": req.customer,
        "form_config": {
            **link_fields.client_config(),
            **svc.form_data(req, actor),
            "mode": "staff",
            "urls": {
                "save": reverse("kyc:link_fill_save", args=[pk]),
                "upload": reverse("kyc:link_fill_upload", args=[pk]),
                "submit": reverse("kyc:link_fill_submit", args=[pk]),
                "done": reverse("kyc:link_detail", args=[pk]),
            },
            "maxUploadMb": 5,
            "confirmEmail": svc.default_confirm_email(req),
        },
    })


@require_http_methods(["PUT", "POST"])
def request_fill_save(request: HttpRequest, pk: int) -> JsonResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        errors = svc.save_draft(req, _json_body(request).get("values") or {}, _actor(request))
    except KycLinkError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"ok": not errors, "errors": errors, **_masked(req)})


@require_POST
def request_fill_upload(request: HttpRequest, pk: int) -> JsonResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    try:
        doc = svc.upload_document(req, request.POST.get("doc_type", ""), request.FILES.get("file"), _actor(request))
    except KycLinkError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "document": {"name": doc.original_name, "size": doc.size}})


@require_POST
def request_fill_submit(request: HttpRequest, pk: int) -> JsonResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    body = _json_body(request)
    actor = _actor(request)
    try:
        problems = svc.submit(req, bool(body.get("consent")), actor,
                              request.build_absolute_uri(reverse("kyc:link_detail", args=[pk])))
    except KycLinkError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    if problems:
        return JsonResponse({"ok": False, "errors": problems}, status=400)
    if body.get("ask_customer_to_confirm"):
        try:
            issued = svc.request_confirmation(req, actor, request, recipient_email=body.get("confirm_email", ""))
            _flash_issued(request, issued, "The confirmation request")
        except KycLinkError as exc:
            messages.error(request, str(exc))
    messages.success(request, "Submitted. Accounts or Top Management can now review it.")
    return JsonResponse({"ok": True, "redirect": reverse("kyc:link_detail", args=[pk])})


@require_POST
def request_confirmation(request: HttpRequest, pk: int) -> HttpResponse:
    req = get_object_or_404(KycRequest, pk=pk)
    if not svc.can_send(request.user):
        messages.error(request, "You cannot send KYC links.")
    else:
        try:
            _flash_issued(request, svc.request_confirmation(req, _actor(request), request,
                                                            recipient_email=request.POST.get("recipient_email", "")),
                          "The confirmation request")
        except KycLinkError as exc:
            messages.error(request, str(exc))
    return redirect("kyc:link_detail", pk=pk)


def _masked(req: KycRequest) -> Dict[str, Any]:
    details = svc.details_for(KycRequest.objects.get(pk=req.pk))
    return {"masked": {"pan": mask(details.pan_last4, 10), "account_number": mask(details.account_last4, 12)}}


# ----- Scheduler -----------------------------------------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["GET", "POST"])
def cron_jobs(request: HttpRequest) -> JsonResponse:
    """App Engine cron (cron.yaml): expire old links and send the 48-hour reminders. App Engine strips
    X-Appengine-Cron from outside requests, so only the scheduler can start a run."""
    if request.headers.get("X-Appengine-Cron") != "true":
        return JsonResponse({"error": "only the scheduler may call this"}, status=403)
    return JsonResponse(svc.run_jobs())
