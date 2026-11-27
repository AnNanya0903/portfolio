"""Merge a duplicate enquiry into the one to keep (administrators only).

Enquiries with a linked quotation cannot be deleted; a true duplicate is merged instead: its quotations, their
tracker, the customer's email thread and addresses move to the enquiry kept, then the duplicate is deleted in the
enquiry service.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from quotations import enquiry_link, roles
from quotations.models import Quotation


@login_required
def merge(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    if not roles.is_admin(request.user):
        messages.error(request, "Only an administrator can merge enquiries.")
        return redirect("enquiries:detail", enquiry_id)
    if request.method == "POST":
        keep = " ".join((request.POST.get("keep") or "").split()).split(" ", 1)[0]
        try:
            result = enquiry_link.merge_enquiries(enquiry_id, keep, user=request.user, request=request)
        except enquiry_link.LinkError as exc:
            messages.error(request, str(exc))
            return redirect("enquiries:merge", enquiry_id)
        kept = enquiry_link.normalize(keep)
        moved = f" Quotations moved: {', '.join(result.quotations)}." if result.quotations else ""
        if result.deleted:
            messages.success(request, f"{enquiry_id} was merged into {kept} and deleted.{moved}")
        else:
            messages.warning(request, result.error + moved)
        return redirect("enquiries:detail", kept)
    return render(request, "enquiries/merge.html", {
        "enquiry_id": enquiry_id,
        "quotations": Quotation.objects.filter(enquiry_id=str(enquiry_id)).order_by("id"),
    })
