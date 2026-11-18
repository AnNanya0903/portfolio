"""The URL App Engine's cron calls every hour. It starts the scheduled automations (enquiries/scheduler.py)."""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import scheduler


@csrf_exempt
@require_http_methods(["GET", "POST"])
def automations(request: HttpRequest) -> JsonResponse:
    """Only App Engine cron may call this: it sets X-Appengine-Cron on its requests and removes that header from
    every request that comes from outside, so nobody else can start a run. There is no token to keep."""
    if request.headers.get("X-Appengine-Cron") != "true":
        return JsonResponse({"error": "only the scheduler may call this"}, status=403)
    # A problem is reported in the body and the log, not as an error status, so cron does not retry and send twice.
    return JsonResponse(scheduler.run_all(trigger="cron"))
