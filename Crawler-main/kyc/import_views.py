"""Bringing existing customers into KYC: from a document (photo, PDF, pasted text) or from a sheet."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import extraction, sheet_import
from .forms import ContactFormSet, CustomerForm, DocumentUploadForm, SheetUploadForm
from .models import Customer, KycDocument

SHEET_SESSION_KEY = "kyc_sheet_import_plan"


def _existing_customer(request: HttpRequest):
    pk = request.GET.get("customer") or request.POST.get("customer") or ""
    return Customer.objects.filter(pk=pk).first() if pk.isdigit() else None


@login_required
def import_new(request: HttpRequest) -> HttpResponse:
    """Upload a document. For a new customer it is read and checked; for an existing one it is only filed."""
    customer = _existing_customer(request)
    posted = request.method == "POST"
    form = DocumentUploadForm(request.POST, request.FILES) if posted else DocumentUploadForm()
    if posted and form.is_valid():
        uploaded = form.cleaned_data.get("document")
        text = (form.cleaned_data.get("pasted_text") or "").strip()
        file_bytes = uploaded.read() if uploaded else None
        if uploaded:
            uploaded.seek(0)
        document = KycDocument(
            customer=customer, pasted_text=text, uploaded_by=request.user,
            original_name=(getattr(uploaded, "name", "") or "")[:255],
            content_type=(getattr(uploaded, "content_type", "") or "").lower()[:120],
            size_bytes=getattr(uploaded, "size", 0) or 0,
        )
        if uploaded:
            document.file = uploaded
        if customer is None:
            try:
                document.extracted_data = extraction.extract_kyc(
                    file_bytes=file_bytes, content_type=document.content_type,
                    filename=document.original_name, text=text)
            except extraction.KycExtractionError as exc:
                document.error_message = str(exc)[:500]
        document.save()

        if customer is not None:
            messages.success(request, "Document added.")
            return redirect("kyc:detail", pk=customer.pk)
        if document.error_message:
            messages.warning(request, f"{document.error_message} Type the details in from the document.")
        elif not extraction.has_details(document.extracted_data):
            messages.warning(request, "Nothing could be read from this document. Type the details in.")
        else:
            messages.success(request, "Details read. Check every field against the document before saving.")
        return redirect("kyc:import_review", doc_id=document.pk)
    return render(request, "kyc/import_new.html", {"form": form, "customer": customer})


@login_required
def import_review(request: HttpRequest, doc_id: int) -> HttpResponse:
    document = get_object_or_404(KycDocument, pk=doc_id)
    if document.customer_id:
        messages.info(request, "This document is already saved to a customer.")
        return redirect("kyc:detail", pk=document.customer_id)

    data = document.extracted_data or {}
    if request.method == "POST":
        form = CustomerForm(request.POST, check_same_name=True)
        contacts = ContactFormSet(request.POST, prefix="contacts")
        if form.is_valid() and contacts.is_valid():
            with transaction.atomic():
                customer = form.save(commit=False)
                customer.created_by = customer.updated_by = request.user
                customer.save()
                saved = 0
                for contact_form in contacts:
                    if contact_form in contacts.deleted_forms:
                        continue
                    contact = contact_form.save(commit=False)
                    contact.customer = customer
                    contact.created_by = contact.updated_by = request.user
                    contact.save()
                    saved += 1
                document.customer = customer
                document.save(update_fields=["customer"])
            messages.success(request, f"Customer {customer.code} saved with {saved} contact{'s' if saved != 1 else ''}.")
            return redirect("kyc:detail", pk=customer.pk)
    else:
        initial = dict(data.get("customer") or {})
        initial["country"] = initial.get("country") or "India"
        form = CustomerForm(initial=initial)
        contacts = ContactFormSet(initial=data.get("contacts") or [], prefix="contacts")

    return render(request, "kyc/import_review.html", {
        "document": document,
        "form": form,
        "contacts": contacts,
        "main_fields": [form[name] for name in ("code", "name", "address", "city", "state", "pincode", "country")],
        "tax_fields": [form[name] for name in ("gstin", "pan", "phone", "website", "notes")],
        "transport_fields": [form[name] for name in ("transportation", "preferred_transporter", "other_transporter", "delivery_type", "msme_applicable", "udyam_aadhar_no")],
    })


@login_required
@require_POST
def import_discard(request: HttpRequest, doc_id: int) -> HttpResponse:
    document = get_object_or_404(KycDocument, pk=doc_id, customer__isnull=True)
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, "Document discarded. Nothing was saved.")
    return redirect("kyc:list")


@login_required
def document_file(request: HttpRequest, doc_id: int) -> HttpResponse:
    """The uploaded file, only for signed-in users with KYC access (never a public link)."""
    document = get_object_or_404(KycDocument, pk=doc_id)
    if not document.file:
        raise Http404()
    try:
        handle = document.file.open("rb")
    except Exception as exc:
        raise Http404() from exc
    response = FileResponse(handle, content_type=document.content_type or "application/octet-stream",
                            filename=document.original_name or "kyc-document")
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def import_sheet(request: HttpRequest) -> HttpResponse:
    """Step 1 checks the sheet and shows what would happen; step 2 (action=confirm) does it."""
    if request.method == "POST" and request.POST.get("action") == "confirm":
        plan = request.session.pop(SHEET_SESSION_KEY, None)
        if not plan:
            messages.error(request, "The checked sheet is no longer available. Upload it again.")
            return redirect("kyc:import_sheet")
        result = sheet_import.apply_plan(plan, request.user)
        text = (f"{result['customers']} customer{'s' if result['customers'] != 1 else ''} and "
                f"{result['contacts']} contact{'s' if result['contacts'] != 1 else ''} imported.")
        if result["skipped"]:
            text += f" {result['skipped']} left out because the customer code was added in the meantime."
        messages.success(request, text)
        return redirect("kyc:list")

    plan = None
    posted = request.method == "POST"
    form = SheetUploadForm(request.POST, request.FILES) if posted else SheetUploadForm()
    if posted and form.is_valid():
        try:
            plan = sheet_import.build_plan(sheet_import.read_rows(form.cleaned_data["sheet"]))
        except sheet_import.SheetError as exc:
            form.add_error("sheet", str(exc))
        else:
            request.session[SHEET_SESSION_KEY] = plan
    else:
        request.session.pop(SHEET_SESSION_KEY, None)
    return render(request, "kyc/import_sheet.html", {
        "form": form, "plan": plan, "headers": sheet_import.TEMPLATE_HEADERS, "max_rows": sheet_import.MAX_ROWS})


@login_required
def sheet_template(request: HttpRequest) -> HttpResponse:
    response = HttpResponse(sheet_import.template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="kyc-import-template.csv"'
    return response
