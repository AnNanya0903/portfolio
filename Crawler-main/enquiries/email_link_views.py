"""“Link enquiry email”: staff pick the customer's original email in the shared mailbox (customerservice@avigiri.com),
and the enquiry keeps its thread so the quotation, the proforma invoice and later emails reply in it.

Enquiries that came by phone, WhatsApp or a visit stay unlinked; their quotation starts a new thread.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from quotations import mailer, threads

logger = logging.getLogger(__name__)


def _back(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("enquiries:email_link", enquiry_id=enquiry_id)


@login_required
def email_link(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    contact = threads.contact_for(enquiry_id)
    if request.method == "POST":
        if request.POST.get("action") == "unlink":
            threads.unlink_email(enquiry_id)
            messages.success(request, f"Enquiry {enquiry_id} is no longer linked to an email thread.")
            return _back(request, enquiry_id)
        try:
            linked = threads.link_email(enquiry_id, (request.POST.get("message") or "").strip(), user=request.user)
        except threads.ThreadError as exc:
            messages.error(request, str(exc))
            return _back(request, enquiry_id)
        messages.success(request, f"Linked: quotations for enquiry {enquiry_id} will reply in “{linked.email_subject}”.")
        return _back(request, enquiry_id)

    query = " ".join((request.GET.get("q") or "").split())
    if not query and not request.GET.get("all") and contact is not None and contact.customer_email:
        query = f"from:{contact.customer_email}"
    emails, error = [], ""
    try:
        emails = threads.recent_emails(query)
    except threads.ThreadError as exc:
        error = str(exc)
    return render(request, "enquiries/email_link.html", {
        "enquiry_id": enquiry_id, "contact": contact, "emails": emails, "q": query, "error": error,
        "mailbox": mailer.mailbox_address(), "next": request.GET.get("next") or "",
    })
