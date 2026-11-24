"""Customer verification: the staff page that sends the email, and the public page the customer answers on."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import verification
from .forms import VerificationResponseForm, VerificationSendForm
from .models import Customer, KycVerification


@login_required
def verification_send(request: HttpRequest, pk: int) -> HttpResponse:
    customer = get_object_or_404(Customer, pk=pk)
    contacts = list(customer.contacts.all())
    posted = request.method == "POST"
    form = VerificationSendForm(request.POST if posted else None, contacts=contacts)

    def build_link(token: str) -> str:
        return request.build_absolute_uri(reverse("kyc:verify", args=[token]))

    if posted and form.is_valid():
        row = verification.send_verification(customer, form.cleaned_data["recipients"], request.user, build_link)
        if row.status == KycVerification.STATUS_SENT:
            messages.success(request, f"Verification email sent to {row.sent_to}.")
        elif row.status == KycVerification.STATUS_DRY_RUN:
            messages.info(request, "Test mode: the email was recorded but not sent. "
                                   "You can open the customer page from the Verification box to see it.")
        else:
            messages.error(request, f"The email was not sent: {row.note}")
        return redirect("kyc:detail", pk=customer.pk)

    valid_until = timezone.now() + timedelta(days=verification.link_days())
    return render(request, "kyc/verification_send.html", {
        "customer": customer,
        "form": form,
        "has_email": bool(form.fields["recipients"].choices),
        "subject": verification.SUBJECT,
        # The real link is made when the email is sent; show where it goes without a working address.
        "body": verification.build_body(customer, build_link("0").replace("/0/", "/(private link)/"), valid_until),
        "cc": verification.default_cc(),
        "dry_run": verification.dry_run(),
        "groups": verification.contact_groups(customer),
    })


def _public(request: HttpRequest, context: dict, status: int = 200) -> HttpResponse:
    response = render(request, "kyc/verify.html", context, status=status)
    # The page shows a company's contact details: keep it out of caches, search engines and referrers.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def verify(request: HttpRequest, token: str) -> HttpResponse:
    """Public page (no sign-in): the private link in the verification email."""
    row = KycVerification.objects.select_related("customer").filter(token=token).first()
    if row is None:
        return _public(request, {"state": "missing"}, status=404)

    state = verification.link_state(row)
    form = VerificationResponseForm()
    if request.method == "POST" and state == "open":
        form = VerificationResponseForm(request.POST)
        if form.is_valid():
            saved = verification.record_response(token, form.cleaned_data["response"], form.cleaned_data["comment"])
            if saved is not None:
                return redirect("kyc:verify", token=token)
            state = verification.link_state(KycVerification.objects.get(pk=row.pk))

    return _public(request, {
        "state": state,
        "row": row,
        "customer": row.customer,
        "groups": verification.contact_groups(row.customer),
        "form": form,
    })
