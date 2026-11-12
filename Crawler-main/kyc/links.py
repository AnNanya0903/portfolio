"""The link between an enquiry (kept in the enquiry service) and a KYC customer.

The enquiries board calls these when a customer code is picked on a form.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from django.db.models import Q

from .models import Customer, EnquiryCustomer

SEARCH_LIMIT = 15


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", str(code or "")).upper()


def customer_for_code(code: str) -> Optional[Customer]:
    code = normalize_code(code)
    return Customer.objects.filter(code=code).first() if code else None


def contact_email(customer: Customer) -> str:
    """Where enquiry emails for this customer go: the purchaser, else the first sales contact with an email."""
    contacts = [c for c in customer.contacts.all() if c.email]
    for pick in (lambda c: c.is_purchaser, lambda c: c.category == "sales_marketing"):
        for contact in contacts:
            if pick(contact):
                return contact.email
    return ""


def set_link(enquiry_id: Any, customer: Optional[Customer], user=None) -> None:
    """Link the enquiry to `customer`; None removes the link."""
    enquiry_id = str(enquiry_id or "").strip()
    if not enquiry_id:
        return
    if customer is None:
        EnquiryCustomer.objects.filter(enquiry_id=enquiry_id).delete()
        return
    EnquiryCustomer.objects.update_or_create(
        enquiry_id=enquiry_id,
        defaults={"customer": customer, "linked_by": user if getattr(user, "is_authenticated", False) else None})


def customers_for(enquiry_ids: Iterable[Any]) -> Dict[str, Customer]:
    """{enquiry_id: customer} for the enquiries that are linked."""
    ids = [str(i) for i in enquiry_ids if i]
    if not ids:
        return {}
    links = EnquiryCustomer.objects.filter(enquiry_id__in=ids).select_related("customer")
    return {link.enquiry_id: link.customer for link in links}


def suggestions_for(names: Iterable[str]) -> Dict[str, Customer]:
    """{lower-case name: customer} for names that match exactly one KYC customer's company name."""
    wanted = {" ".join(str(n or "").split()).lower() for n in names}
    wanted.discard("")
    if not wanted:
        return {}
    by_name: Dict[str, List[Customer]] = {}
    query = Q()
    for name in wanted:
        query |= Q(name__iexact=name)
    for customer in Customer.objects.filter(query):
        by_name.setdefault(" ".join(customer.name.split()).lower(), []).append(customer)
    return {name: found[0] for name, found in by_name.items() if len(found) == 1}


def search(q: str) -> List[Dict[str, str]]:
    """Customers for the picker: by code, company name or contact email."""
    q = " ".join(str(q or "").split())
    customers = Customer.objects.all()
    if q:
        customers = customers.filter(
            Q(code__icontains=normalize_code(q)) | Q(name__icontains=q) | Q(contacts__email__icontains=q)
        ).distinct()
    customers = customers.order_by("code").prefetch_related("contacts")[:SEARCH_LIMIT]
    return [{"code": c.code, "name": c.name, "city": c.city, "email": contact_email(c)} for c in customers]
