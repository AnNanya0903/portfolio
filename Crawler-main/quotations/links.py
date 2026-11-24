"""How the other portals find the quotations of an enquiry or a customer (and link to them)."""
from __future__ import annotations

from typing import Dict, Iterable, List

from django.db.models import Count

from .models import Quotation


def counts_for_enquiries(enquiry_ids: Iterable[str]) -> Dict[str, int]:
    """{enquiry id: number of quotations entered for it}. One query, for a whole page of the board."""
    ids = [str(i) for i in enquiry_ids if str(i)]
    if not ids:
        return {}
    rows = Quotation.objects.filter(enquiry_id__in=ids).values("enquiry_id").annotate(n=Count("id"))
    return {row["enquiry_id"]: row["n"] for row in rows}


def for_enquiry(enquiry_id: str) -> List[Quotation]:
    return list(Quotation.objects.filter(enquiry_id=str(enquiry_id)).order_by("-updated_at", "-id")[:20])


def for_customer(customer) -> List[Quotation]:
    return list(Quotation.objects.filter(customer=customer).order_by("-updated_at", "-id")[:20])
