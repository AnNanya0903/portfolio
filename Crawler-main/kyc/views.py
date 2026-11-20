"""KYC portal pages: customer list, customer page, and forms for the customer and its contacts."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, IntegerField, Q
from django.db.models.functions import Cast, Substr
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import links, mailer, verification
from .forms import CustomerContactForm, CustomerForm
from .departments import audit_field_name, can_see_full_pan, log_audit, pan_for
from .models import Customer, CustomerContact, Department, KycAuditLog

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
# Sort orders for the customer list. Customer code sorts by year, then running number (AVG-26-1002 before
# AVG-26-10000), so it is ordered by the year and the number, not as text.
LIST_SORTS = {
    "customer_code": ("customer_code_year", "customer_code_number"),
    "customer_code_desc": ("-customer_code_year", "-customer_code_number"),
    "name": ("name", "code"),
    "code": ("code",),
    "updated": ("-updated_at",),
}


@login_required
def customer_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    customers = Customer.objects.annotate(
        contact_count=Count("contacts", distinct=True),
        purchaser_count=Count("contacts", filter=Q(contacts__is_purchaser=True), distinct=True),
    ).prefetch_related("departments").order_by("name", "code")
    if q:
        customers = customers.filter(
            Q(customer_code__icontains=q) | Q(code__icontains=q) | Q(name__icontains=q) | Q(gstin__icontains=q)
            | Q(city__icontains=q) | Q(contacts__name__icontains=q) | Q(contacts__email__icontains=q)
        ).distinct()
    sort = request.GET.get("sort") or ""
    if sort not in LIST_SORTS:
        sort = "name"  # the list has always been in name order by default
    # AVG-26-10000 comes after AVG-26-1002: sort on the year and the running number as numbers, not as text.
    customers = customers.annotate(
        customer_code_year=Cast(Substr("customer_code", 5, 2), IntegerField()),
        customer_code_number=Cast(Substr("customer_code", 8), IntegerField()),
    ).order_by(*LIST_SORTS[sort], "pk")
    page = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "kyc/list.html", {"page": page, "q": q, "sort": sort, "total": page.paginator.count})


def _customer_form_page(request: HttpRequest, form: CustomerForm, customer=None) -> HttpResponse:
    return render(request, "kyc/customer_form.html", {
        "form": form,
        "customer": customer,
        "main_fields": [form[name] for name in ("code", "name")],
        "address_fields": [form[name] for name in ("address", "city", "state", "pincode", "country")],
        "tax_fields": [form[name] for name in ("gstin", "pan", "phone", "website", "notes") if name in form.fields],
        "departments_field": form["departments"],
        "all_departments_field": form["all_departments"],
        "transport_fields": [form[name] for name in ("transportation", "preferred_transporter", "other_transporter", "delivery_type", "msme_applicable", "udyam_aadhar_no")],
    })


@login_required
def customer_new(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = CustomerForm(request.POST, check_same_name=True)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.created_by = customer.updated_by = request.user
            customer.save()
            form.save_m2m()
            log_audit(request.user, customer, KycAuditLog.ACTION_CREATE)
            messages.success(request, f"Customer {customer.code} saved. Now add the contacts.")
            return redirect("kyc:detail", pk=customer.pk)
        return _customer_form_page(request, form)
    return _customer_form_page(request, CustomerForm())


@login_required
def customer_edit(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(Customer, pk=pk)
    hide_pan = not can_see_full_pan(request.user)
    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer, check_same_name=True, hide_pan=hide_pan)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.updated_by = request.user
            customer.save()
            form.save_m2m()
            log_audit(request.user, customer, KycAuditLog.ACTION_EDIT,
                      [audit_field_name(f) for f in form.changed_data if f != "all_departments"])
            messages.success(request, "Customer details updated.")
            return redirect("kyc:detail", pk=customer.pk)
        # form.instance now holds the unsaved values; show the page for the saved customer.
        return _customer_form_page(request, form, get_object_or_404(Customer, pk=pk))
    return _customer_form_page(request, CustomerForm(instance=customer, hide_pan=hide_pan), customer)


def _marketing_activity(customer: Customer) -> dict:
    """Lost enquiries in the Marketing portal and campaign emails, for this customer's linked enquiries."""
    from marketing.models import CampaignRecipient, MarketingLead

    enquiry_ids = list(customer.enquiry_links.values_list("enquiry_id", flat=True))
    if not enquiry_ids:
        return {"leads": [], "emails": []}
    leads = list(MarketingLead.objects.filter(enquiry_id__in=enquiry_ids).order_by("-updated_at")[:20])
    emails = list(CampaignRecipient.objects.filter(enquiry_id__in=enquiry_ids).select_related("campaign")
                  .order_by("-created_at")[:20])
    return {"leads": leads, "emails": emails}


@login_required
def customer_detail(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(
        Customer.objects.select_related("created_by", "updated_by").prefetch_related("departments__contacts"), pk=pk)
    log_audit(request.user, customer, KycAuditLog.ACTION_VIEW)
    from quotations import links as quotation_links

    contacts = list(customer.contacts.all())
    groups = [
        {"key": key, "label": label, "contacts": [c for c in contacts if c.category == key]}
        for key, label in CustomerContact.CATEGORY_CHOICES
    ]
    # Each department shows its own contacts. Sales and Marketing both show the Sales and marketing contacts.
    department_category = {
        "Sales": CustomerContact.CATEGORY_SALES,
        "Marketing": CustomerContact.CATEGORY_SALES,
        "Accounts": CustomerContact.CATEGORY_ACCOUNTS,
        "Technical/Design": CustomerContact.CATEGORY_TECHNICAL,
        "Top Management": CustomerContact.CATEGORY_MANAGEMENT,
    }
    assigned_ids = {d.pk for d in customer.departments.all()}
    department_groups = []
    for department in Department.objects.filter(is_active=True).prefetch_related("contacts").order_by("sort_order", "name"):
        category = department_category.get(department.name, "")
        department_groups.append({
            "department": department,
            "category": category,
            "contacts": [c for c in contacts if category and c.category == category],
            "emails": department.emails(),
            "assigned": department.pk in assigned_ids,
        })
    return render(request, "kyc/detail.html", {
        "customer": customer,
        "pan_display": pan_for(request.user, customer.pan),
        "team_departments": [{"department": d, "emails": d.emails()} for d in customer.departments.all()],
        "groups": groups,
        "department_groups": department_groups,
        "documents": list(customer.documents.select_related("uploaded_by")),
        "email_history": mailer.history_for_customer(customer),
        "verifications": [{"row": v, "state": verification.link_state(v)}
                          for v in customer.verifications.select_related("sent_by")[:5]],
        "has_email": any(c.email for c in contacts),
        "enquiry_links": list(customer.enquiry_links.all()[:50]),
        "enquiry_link_count": customer.enquiry_links.count(),
        "quotations": quotation_links.for_customer(customer),
        "marketing": _marketing_activity(customer),
        "delete_summary": {
            "contacts": len(contacts),
            "documents": customer.documents.count(),
            "verifications": customer.verifications.count(),
            "enquiries": customer.enquiry_links.count(),
        },
        "has_purchaser": any(c.is_purchaser for c in contacts),
        "missing_teams": [g["label"] for g in groups if not any(c.email for c in g["contacts"])],
    })


def _contact_form_page(request: HttpRequest, form: CustomerContactForm, customer: Customer,
                       contact=None) -> HttpResponse:
    return render(request, "kyc/contact_form.html", {
        "form": form,
        "customer": customer,
        "contact": contact,
        "text_fields": [form[name] for name in ("category", "name", "designation", "phone", "alternate_phone", "email", "notes")],
        "other_designation_field": form["designation_other"],
    })


@login_required
def contact_new(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == "POST":
        form = CustomerContactForm(request.POST)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.customer = customer
            contact.created_by = contact.updated_by = request.user
            contact.save()
            log_audit(request.user, customer, KycAuditLog.ACTION_EDIT, [f"contact added: {contact.name}"])
            messages.success(request, f"{contact.name} added to {contact.get_category_display()}.")
            return redirect("kyc:detail", pk=customer.pk)
        return _contact_form_page(request, form, customer)
    category = request.GET.get("category") or ""
    valid = {key for key, _ in CustomerContact.CATEGORY_CHOICES}
    initial = {"category": category} if category in valid else {}
    return _contact_form_page(request, CustomerContactForm(initial=initial), customer)


@login_required
def contact_edit(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    customer = get_object_or_404(Customer, pk=pk)
    contact = get_object_or_404(CustomerContact, pk=contact_id, customer=customer)
    if request.method == "POST":
        form = CustomerContactForm(request.POST, instance=contact)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.updated_by = request.user
            contact.save()
            log_audit(request.user, customer, KycAuditLog.ACTION_EDIT,
                      [audit_field_name(f"contact {contact.name}: {f}") for f in form.changed_data])
            messages.success(request, "Contact updated.")
            return redirect("kyc:detail", pk=customer.pk)
        return _contact_form_page(request, form, customer, contact)
    return _contact_form_page(request, CustomerContactForm(instance=contact), customer, contact)


@login_required
@require_POST
def contact_delete(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(CustomerContact, pk=contact_id, customer_id=pk)
    name = contact.name
    log_audit(request.user, contact.customer, KycAuditLog.ACTION_EDIT, [audit_field_name(f"contact removed: {name}")])
    contact.delete()
    messages.success(request, f"{name} removed.")
    return redirect("kyc:detail", pk=pk)


@login_required
@require_POST
def customer_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a customer for good: contacts, uploaded KYC documents (and their files), verification
    requests and enquiry links go with it. The enquiries themselves are not touched.

    The customer code must be typed to confirm, so a stray click can never delete a record.
    """
    customer = get_object_or_404(Customer, pk=pk)
    typed = "".join((request.POST.get("confirm_code") or "").split()).upper()
    if typed != customer.code.upper():
        messages.error(request, f"Not deleted. Type the customer code {customer.code} exactly to confirm.")
        return redirect("kyc:detail", pk=pk)
    label = f"{customer.code} - {customer.name}"
    stored_files = [doc.file for doc in customer.documents.all() if doc.file]
    with transaction.atomic():
        customer.delete()
    for stored in stored_files:  # after the records are gone; a storage problem must not bring them back
        try:
            stored.delete(save=False)
        except Exception:
            logger.warning("KYC: could not remove the stored file %s of deleted customer %s", stored.name, label)
    log_audit(request.user, customer, KycAuditLog.ACTION_DELETE)
    logger.info("KYC customer %s deleted by %s", label, request.user)
    messages.success(request, f"Customer {label} was deleted.")
    return redirect("kyc:list")


@login_required
def customer_search(request: HttpRequest) -> JsonResponse:
    """Customers for the customer-code picker on the enquiry forms."""
    return JsonResponse({"customers": links.search(request.GET.get("q") or "")})
