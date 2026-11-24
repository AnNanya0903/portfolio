"""The send endpoint behind the shared compose box (kyc/_email_compose.html). Used by Marketing today."""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from . import mailer
from .models import Customer


@login_required
@require_POST
def send(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "The email could not be read. Try again.", "problems": []}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "The email could not be read. Try again.", "problems": []}, status=400)

    customer = None
    customer_id = payload.get("customer_id")
    if customer_id not in (None, ""):
        try:
            customer = Customer.objects.get(pk=int(customer_id))
        except (TypeError, ValueError, Customer.DoesNotExist):
            return JsonResponse({"ok": False, "error": "This customer no longer exists.", "problems": []}, status=400)
    enquiry_id = str(payload.get("enquiry_id") or "").strip()[:128]

    try:
        log = mailer.send_email(
            user=request.user, to=payload.get("to") or [], cc=payload.get("cc") or [], bcc=payload.get("bcc") or [],
            subject=str(payload.get("subject") or ""), body=str(payload.get("body") or ""),
            customer=customer, enquiry_id=enquiry_id)
    except mailer.MailError as exc:
        status = 502 if exc.gmail else 400
        return JsonResponse({"ok": False, "error": str(exc), "problems": exc.problems}, status=status)
    return JsonResponse({"ok": True, "id": log.pk, "message": f"Email sent to {', '.join(log.to.split(', '))}."})
