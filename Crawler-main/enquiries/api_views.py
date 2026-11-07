"""Endpoints n8n calls on a schedule: automatic follow-ups (Stage 2) and automatic customer emails.

Both need the X-N8N-Token header to match settings.N8N_API_TOKEN. They do not use a login
session, so they are CSRF-exempt; the token is the only way in.
"""
from __future__ import annotations

import functools
import hmac

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import auto_follow_up, services, status_emails


def _n8n_token_required(view):
    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        expected = (getattr(settings, "N8N_API_TOKEN", "") or "").strip()
        if not expected:
            return JsonResponse({"error": "N8N_API_TOKEN is not configured"}, status=503)
        given = (request.headers.get("X-N8N-Token") or "").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return JsonResponse({"error": "invalid token"}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _status() -> dict:
    return {
        "enabled": bool(getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False)),
        "dry_run": bool(getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True)),
    }


@csrf_exempt
@require_GET
@_n8n_token_required
def due(request: HttpRequest) -> JsonResponse:
    """List enquiries due for an automatic follow-up today."""
    if not _status()["enabled"]:
        return JsonResponse({**_status(), "due": []})
    try:
        items = auto_follow_up.due_follow_ups()
    except services.EnquiryServiceError as exc:
        return JsonResponse({**_status(), "error": f"could not load enquiries: {exc}"}, status=502)
    return JsonResponse({**_status(), "count": len(items), "due": items})


@csrf_exempt
@require_POST
@_n8n_token_required
def send(request: HttpRequest, enquiry_id: str) -> JsonResponse:
    """Send one automatic follow-up. Status 200 for sent/skipped/dry run, 500 when sending failed."""
    outcome = auto_follow_up.run_auto_follow_up(enquiry_id)
    status = 500 if outcome["result"] == auto_follow_up.FAILED else 200
    return JsonResponse({**_status(), **outcome}, status=status)


@csrf_exempt
@require_POST
@_n8n_token_required
def run_customer_emails(request: HttpRequest) -> JsonResponse:
    """Scheduled run: acknowledge new enquiries and send the closing email where the customer never replied."""
    if not _status()["enabled"]:
        return JsonResponse({**_status(), "acknowledgement": [], "closing": []})
    try:
        results = status_emails.run_scheduled(auto_follow_up._api_root())
    except services.EnquiryServiceError as exc:
        return JsonResponse({**_status(), "error": f"could not load enquiries: {exc}"}, status=502)
    return JsonResponse({**_status(), **results})
