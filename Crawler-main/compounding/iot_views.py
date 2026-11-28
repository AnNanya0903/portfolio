"""IoT pages around the dashboard: the daily batch-variation view (live partial, CSV / PDF report), the
detected-batch page, excluding a batch from its standard, and the standards page."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from magicwand.access import require_any_service_access, require_service_access, user_has_service_access

from . import iot_config, iot_daily, iot_segments, iot_standards
from .models import DetectedBatch
from .views import _iot_dashboard_available, _iot_dashboard_machine_id, _parse_date

IOT_SERVICES = ["compounding_planning", "compounding_board"]
# Admins (exclude batches from a standard, rebuild standards) are the Compounding planning team.
ADMIN_SERVICE = "compounding_planning"


def _require_dashboard() -> None:
    if not _iot_dashboard_available():
        raise Http404("Not found")


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and (user.is_superuser or user_has_service_access(user, ADMIN_SERVICE)))


def requested_day(request) -> date:
    return _parse_date(request.GET.get("date") or request.POST.get("date") or "") or iot_segments.today_ist()


def daily_context(machine_id: str, day: date, user) -> Dict[str, Any]:
    is_today = day == iot_segments.today_ist()
    if is_today:
        iot_segments.detect_recent_throttled(machine_id)
    return {
        "daily": iot_daily.build_day(machine_id, day),
        "daily_date": day.isoformat(),
        "daily_is_today": is_today,
        "daily_previous": (day - timedelta(days=1)).isoformat(),
        "daily_next": (day + timedelta(days=1)).isoformat(),
        "iot_can_admin": is_admin(user),
        "iot_time_zone": "Asia/Kolkata",
    }


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def daily_partial(request):
    """The daily variation view as HTML, for the dashboard's live refresh."""
    _require_dashboard()
    return render(request, "magicwand/_iot_daily.html",
                  daily_context(_iot_dashboard_machine_id(), requested_day(request), request.user))


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def daily_report(request, fmt: str):
    _require_dashboard()
    if fmt not in {"csv", "pdf"}:
        raise Http404("Unknown report format")
    machine_id = _iot_dashboard_machine_id()
    day = requested_day(request)
    data = iot_daily.build_day(machine_id, day)
    if fmt == "pdf":
        response = HttpResponse(iot_daily.report_pdf(data, machine_id), content_type="application/pdf")
    else:
        response = HttpResponse(iot_daily.report_csv(data), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="batch-variation-{day.isoformat()}.{fmt}"'
    return response


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def detected_batch_detail(request, batch_id: int):
    _require_dashboard()
    batch = get_object_or_404(DetectedBatch.objects.select_related("log_run", "standard", "excluded_by"), pk=batch_id)
    standard = batch.standard
    day = batch.start_at.astimezone(iot_segments.IST_TIMEZONE).date()
    count, needed = iot_standards.readiness(batch.compound_code) if batch.compound_code else (0, 0)
    stats = (standard.stats or {}) if standard else {}
    kpis = [
        ("Duration", round(batch.duration_s / 60, 1), "min", stats.get("duration_s"), 60, batch.duration_dev_pct, "%"),
        ("Energy", batch.energy_kwh, "kWh", stats.get("energy_kwh"), 1, batch.energy_dev_pct, "%"),
        ("Loading peak", batch.peak_current, "A", stats.get("peak_current"), 1, batch.peak_dev_a, "A"),
        ("Time to peak", batch.time_to_peak_s, "s", stats.get("time_to_peak_s"), 1, batch.peak_time_dev_s, "s"),
        ("Steady current", batch.steady_current, "A", stats.get("steady_current"), 1, batch.steady_dev_a, "A"),
    ]
    kpi_rows = []
    for label, value, unit, stat, divisor, deviation, deviation_unit in kpis:
        row = {"label": label, "value": value, "unit": unit, "deviation": deviation, "deviation_unit": deviation_unit,
               "standard": None, "low": None, "high": None, "outside": False}
        if stat:
            row.update(standard=round(stat["median"] / divisor, 1), low=round(stat["low"] / divisor, 1),
                       high=round(stat["high"] / divisor, 1))
            raw = {"Duration": batch.duration_s}.get(label, value)
            row["outside"] = raw is not None and not (stat["low"] <= raw <= stat["high"])
        kpi_rows.append(row)
    return render(
        request,
        "magicwand/iot_detected_batch.html",
        {
            "batch": batch,
            "standard": standard,
            "kpis": kpi_rows,
            "result_label": "Running" if batch.is_open else iot_daily.RESULT_LABELS.get(batch.result, "—"),
            "result_tone": "info" if batch.is_open else iot_daily.RESULT_TONES.get(batch.result, "secondary"),
            "ready_count": min(count, needed),
            "needed": needed,
            "chart": {
                "curve": batch.curve or [],
                "step": batch.curve_step_s,
                "standard": standard.points if standard else [],
                "aligned": batch.load_at is not None,
                "sections": batch.out_of_band or [],
            },
            "can_admin": is_admin(request.user),
            "dashboard_url": f"{reverse('compounding:iot_dashboard')}?date={day.isoformat()}#iot-daily",
            "iot_time_zone": "Asia/Kolkata",
        },
    )


@login_required
@require_service_access(ADMIN_SERVICE)
@require_http_methods(["POST"])
def exclude_batch(request, batch_id: int):
    """Admin: keep a batch out of (or put it back into) its compound's standard."""
    _require_dashboard()
    batch = get_object_or_404(DetectedBatch, pk=batch_id)
    batch.excluded = request.POST.get("excluded") == "1"
    batch.excluded_reason = (request.POST.get("excluded_reason") or "").strip()[:200] if batch.excluded else ""
    batch.excluded_by = request.user if batch.excluded else None
    batch.save(update_fields=["excluded", "excluded_reason", "excluded_by", "updated_at"])
    messages.success(
        request,
        "Batch left out of the standard. Rebuild the standard to apply it." if batch.excluded
        else "Batch counts towards the standard again.",
    )
    return redirect("compounding:iot_detected_batch", batch_id=batch.pk)


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET", "POST"])
def standards(request):
    _require_dashboard()
    admin = is_admin(request.user)
    if request.method == "POST":
        if not admin:
            messages.error(request, "Only Compounding planning can rebuild standards.")
            return redirect("compounding:iot_standards")
        compound = (request.POST.get("compound") or "").strip()
        try:
            standard = iot_standards.build_standard(compound, user=request.user, force=True)
        except iot_standards.StandardBuildError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Built {compound} v{standard.version} from {standard.built_from_n} batches.")
        return redirect("compounding:iot_standards")
    return render(
        request,
        "magicwand/iot_standards.html",
        {
            "rows": iot_standards.overview(),
            "admin": admin,
            "min_batches": iot_config.standard_min_batches(),
            "dashboard_url": reverse("compounding:iot_dashboard"),
        },
    )
