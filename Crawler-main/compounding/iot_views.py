"""IoT pages beyond the dashboard itself: the machine-vs-log table (live partial, CSV, accepting machine times),
detected-batch detail, tagging past batches, and standards / process groups."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from magicwand.access import require_any_service_access, require_service_access, user_has_service_access

from . import iot_reconcile, iot_segments
from .models import CompoundingIoTRun, DetectedBatch
from .views import _iot_dashboard_available, _iot_dashboard_machine_id, _parse_date

IOT_SERVICES = ["compounding_planning", "compounding_board"]
# Supervisors (accept machine times, tag batches, rebuild standards) are the Compounding planning team.
SUPERVISOR_SERVICE = "compounding_planning"


def _require_dashboard() -> None:
    if not _iot_dashboard_available():
        raise Http404("Not found")


def is_supervisor(user) -> bool:
    return bool(user and user.is_authenticated and (user.is_superuser or user_has_service_access(user, SUPERVISOR_SERVICE)))


def recon_day(request) -> date:
    return _parse_date(request.GET.get("date") or request.POST.get("date") or "") or iot_segments.today_ist()


def recon_context(machine_id: str, day: date, user) -> Dict[str, Any]:
    is_today = day == iot_segments.today_ist()
    if is_today:
        iot_segments.detect_recent_throttled(machine_id)
    rows = iot_reconcile.build_rows(machine_id, day)
    return {
        "recon_rows": rows,
        "recon_summary": iot_reconcile.summarize(rows),
        "recon_date": day.isoformat(),
        "recon_is_today": is_today,
        "recon_can_correct": is_supervisor(user),
        "iot_time_zone": "Asia/Kolkata",
    }


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def recon_partial(request):
    """The machine-vs-log table as HTML, for the dashboard's live refresh."""
    _require_dashboard()
    context = recon_context(_iot_dashboard_machine_id(), recon_day(request), request.user)
    return render(request, "magicwand/_iot_recon.html", context)


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def recon_csv(request):
    _require_dashboard()
    day = recon_day(request)
    rows = iot_reconcile.build_rows(_iot_dashboard_machine_id(), day)
    response = HttpResponse(iot_reconcile.rows_csv(rows), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="machine-vs-log-{day.isoformat()}.csv"'
    return response


@login_required
@require_service_access(SUPERVISOR_SERVICE)
@require_http_methods(["POST"])
def accept_machine_times(request):
    """Supervisor: correct an operator log to the detected batch's times (audited)."""
    _require_dashboard()
    day = recon_day(request)
    redirect_url = f"{reverse('compounding:iot_dashboard')}?date={day.isoformat()}#iot-recon"
    machine_id = _iot_dashboard_machine_id()
    batch = DetectedBatch.objects.filter(pk=request.POST.get("batch_id") or 0, machine_id=machine_id).first()
    log = CompoundingIoTRun.objects.filter(pk=request.POST.get("log_id") or 0, machine_id=machine_id).first()
    if batch is None or log is None:
        messages.error(request, "That batch or log no longer exists. Refresh the page.")
        return redirect(redirect_url)
    rows = iot_reconcile.build_rows(machine_id, day)
    row = next((r for r in rows if r["batch"] is not None and r["batch"].pk == batch.pk and r["log"] is not None
                and r["log"].pk == log.pk), None)
    if row is None or not row["correctable"]:
        messages.error(request, "Only mis-logged or time-mismatched rows of finished batches can be corrected.")
        return redirect(redirect_url)
    try:
        iot_reconcile.accept_machine_times(batch, log, request.user, verdict=row["verdict"])
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_url)
    messages.success(
        request,
        f"{log.compound_code} log corrected to the machine's {row['start_text']}–{row['end_text']}. "
        "The old times are kept in the audit trail.",
    )
    return redirect(redirect_url)
