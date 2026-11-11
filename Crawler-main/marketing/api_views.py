"""Endpoint n8n calls on a schedule to look for customer replies to campaign emails.

Uses the same X-N8N-Token header as the enquiry follow-up endpoints (settings.N8N_API_TOKEN).
"""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from enquiries.api_views import _n8n_token_required

from . import campaigns


@csrf_exempt
@require_POST
@_n8n_token_required
def check_replies(request: HttpRequest) -> JsonResponse:
    results = campaigns.check_all_replies()
    return JsonResponse({
        "campaigns": results,
        "replied": sum(r.get("replied", 0) for r in results),
        "failed": sum(1 for r in results if r["result"] == "failed"),
    })
