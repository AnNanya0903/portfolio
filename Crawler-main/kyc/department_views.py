"""Departments: the Contact Departments cards for everyone, and the admin page that manages them."""
from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from magicwand.access import is_super_admin

from .models import Department, DepartmentContact


def admin_only(view):
    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not is_super_admin(request.user):
            return HttpResponseForbidden("Only an administrator can change departments.")
        return view(request, *args, **kwargs)
    return login_required(wrapper)


@login_required
def departments(request: HttpRequest) -> HttpResponse:
    """Every active department as a card with all its emails, the primary one first."""
    cards = Department.objects.filter(is_active=True).prefetch_related("contacts")
    return render(request, "kyc/departments.html", {"departments": cards, "is_admin": is_super_admin(request.user)})


@admin_only
def departments_manage(request: HttpRequest) -> HttpResponse:
    return render(request, "kyc/departments_manage.html",
                  {"departments": Department.objects.prefetch_related("contacts").order_by("-is_active", "sort_order", "name")})


@admin_only
@require_POST
def department_save(request: HttpRequest, pk: int = 0) -> HttpResponse:
    """Create a department (pk 0) or change its name and description."""
    name = " ".join((request.POST.get("name") or "").split())
    description = " ".join((request.POST.get("description") or "").split())[:300]
    if not name:
        messages.error(request, "A department needs a name.")
        return redirect("kyc:departments_manage")
    clash = Department.objects.filter(name__iexact=name).exclude(pk=pk)
    if clash.exists():
        messages.error(request, f"There is already a department called {name}.")
        return redirect("kyc:departments_manage")
    if pk:
        department = get_object_or_404(Department, pk=pk)
        department.name, department.description = name, description
        department.save(update_fields=["name", "description"])
        messages.success(request, f"{name} updated.")
    else:
        last = Department.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        Department.objects.create(name=name, description=description, sort_order=last + 1)
        messages.success(request, f"{name} added. Add its email addresses next.")
    return redirect("kyc:departments_manage")


@admin_only
@require_POST
def department_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    """Switch a department off or on. It is never deleted, so customers keep their 'Handled by' label."""
    department = get_object_or_404(Department, pk=pk)
    department.is_active = not department.is_active
    department.save(update_fields=["is_active"])
    messages.success(request, f"{department.name} is now {'active' if department.is_active else 'deactivated'}.")
    return redirect("kyc:departments_manage")


@admin_only
@require_POST
def department_email_add(request: HttpRequest, pk: int) -> HttpResponse:
    department = get_object_or_404(Department, pk=pk)
    email = (request.POST.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, f"“{email}” is not a valid email address.")
        return redirect("kyc:departments_manage")
    with transaction.atomic():
        if department.contacts.filter(email__iexact=email).exists():
            messages.error(request, f"{email} is already in {department.name}.")
            return redirect("kyc:departments_manage")
        first = not department.contacts.exists()
        DepartmentContact.objects.create(department=department, email=email, is_primary=first)
    messages.success(request, f"{email} added to {department.name}.")
    return redirect("kyc:departments_manage")


@admin_only
@require_POST
def department_email_edit(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(DepartmentContact, pk=contact_id, department_id=pk)
    email = (request.POST.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, f"“{email}” is not a valid email address.")
        return redirect("kyc:departments_manage")
    if contact.department.contacts.filter(email__iexact=email).exclude(pk=contact.pk).exists():
        messages.error(request, f"{email} is already in {contact.department.name}.")
        return redirect("kyc:departments_manage")
    contact.email = email
    contact.save(update_fields=["email"])
    messages.success(request, "Email updated.")
    return redirect("kyc:departments_manage")


@admin_only
@require_POST
def department_email_primary(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(DepartmentContact, pk=contact_id, department_id=pk)
    with transaction.atomic():
        contact.department.contacts.update(is_primary=False)
        contact.is_primary = True
        contact.save(update_fields=["is_primary"])
    messages.success(request, f"{contact.email} is now the primary contact of {contact.department.name}.")
    return redirect("kyc:departments_manage")


@admin_only
@require_POST
def department_email_remove(request: HttpRequest, pk: int, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(DepartmentContact, pk=contact_id, department_id=pk)
    department, email, was_primary = contact.department, contact.email, contact.is_primary
    with transaction.atomic():
        contact.delete()
        if was_primary:  # keep one primary: the next address takes over
            nxt = department.contacts.first()
            if nxt:
                nxt.is_primary = True
                nxt.save(update_fields=["is_primary"])
    messages.success(request, f"{email} removed from {department.name}.")
    return redirect("kyc:departments_manage")
