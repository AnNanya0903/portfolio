"""KYC portal pages: customer list, customer page, and forms for the customer and its contacts."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CustomerContactForm, CustomerForm
from .models import Customer, CustomerContact

PAGE_SIZE = 50


@login_required
def customer_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    customers = Customer.objects.annotate(
        contact_count=Count("contacts", distinct=True),
        purchaser_count=Count("contacts", filter=Q(contacts__is_purchaser=True), distinct=True),
    ).order_by("name", "code")
    if q:
        customers = customers.filter(
            Q(code__icontains=q) | Q(name__icontains=q) | Q(gstin__icontains=q) | Q(city__icontains=q)
            | Q(contacts__name__icontains=q) | Q(contacts__email__icontains=q)
        ).distinct()
    page = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "kyc/list.html", {"page": page, "q": q, "total": page.paginator.count})


def _customer_form_page(request: HttpRequest, form: CustomerForm, customer=None) -> HttpResponse:
    return render(request, "kyc/customer_form.html", {
        "form": form,
        "customer": customer,
        "main_fields": [form[name] for name in ("code", "name")],
        "address_fields": [form[name] for name in ("address", "city", "state", "pincode", "country")],
        "tax_fields": [form[name] for name in ("gstin", "pan", "phone", "website", "notes")],
    })


@login_required
def customer_new(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = CustomerForm(request.POST)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.created_by = customer.updated_by = request.user
            customer.save()
            messages.success(request, f"Customer {customer.code} saved. Now add the contacts.")
            return redirect("kyc:detail", pk=customer.pk)
        return _customer_form_page(request, form)
    return _customer_form_page(request, CustomerForm())


@login_required
def customer_edit(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.updated_by = request.user
            customer.save()
            messages.success(request, "Customer details updated.")
            return redirect("kyc:detail", pk=customer.pk)
        # form.instance now holds the unsaved values; show the page for the saved customer.
        return _customer_form_page(request, form, get_object_or_404(Customer, pk=pk))
    return _customer_form_page(request, CustomerForm(instance=customer), customer)


@login_required
def customer_detail(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(Customer.objects.select_related("created_by", "updated_by"), pk=pk)
    contacts = list(customer.contacts.all())
    groups = [
        {"key": key, "label": label, "contacts": [c for c in contacts if c.category == key]}
        for key, label in CustomerContact.CATEGORY_CHOICES
    ]
    return render(request, "kyc/detail.html", {
        "customer": customer,
        "groups": groups,
        "has_purchaser": any(c.is_purchaser for c in contacts),
        "missing_teams": [g["label"] for g in groups if not any(c.email for c in g["contacts"])],
    })


def _contact_form_page(request: HttpRequest, form: CustomerContactForm, customer: Customer,
                       contact=None) -> HttpResponse:
    return render(request, "kyc/contact_form.html", {
        "form": form,
        "customer": customer,
        "contact": contact,
        "text_fields": [form[name] for name in ("category", "name", "designation", "email", "phone", "notes")],
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
            messages.success(request, "Contact updated.")
            return redirect("kyc:detail", pk=customer.pk)
        return _contact_form_page(request, form, customer, contact)
    return _contact_form_page(request, CustomerContactForm(instance=contact), customer, contact)


@login_required
@require_POST
def contact_delete(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(CustomerContact, pk=contact_id, customer_id=pk)
    name = contact.name
    contact.delete()
    messages.success(request, f"{name} removed.")
    return redirect("kyc:detail", pk=pk)
