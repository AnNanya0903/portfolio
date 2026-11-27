"""Pages and APIs for IoT batches: batch detail, lab results, process groups and their standard curves."""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from magicwand.access import require_any_service_access

from . import iot_batches, iot_config, iot_curves, iot_samples
from .models import CompoundingIoTRun, ProcessGroup, ProcessGroupCompound
from .views import _available_master_compounds, _iot_dashboard_available, _iot_run_compound_options

_IOT_SERVICES = ["compounding_planning", "compounding_board"]
_MAX_LAB_VALUES = 50


def _require_dashboard() -> None:
    if not _iot_dashboard_available():
        raise Http404("Not found")


def _ms(value) -> Optional[int]:
    return int(value.timestamp() * 1000) if value else None


def batch_row(run: CompoundingIoTRun) -> Dict[str, Any]:
    """One batch as the dashboard table and APIs show it."""
    return {
        "id": run.pk,
        "batchId": run.batch_id,
        "compoundCode": run.compound_code,
        "unassigned": run.compound_code == CompoundingIoTRun.UNASSIGNED_COMPOUND,
        "batchType": run.get_batch_type_display(),
        "startedAt": run.started_at,
        "endedAt": run.ended_at,
        "durationS": run.duration_s,
        "energyKwh": run.energy_kwh,
        "avgCurrent": run.avg_current,
        "peakCurrent": run.peak_current,
        "status": run.status,
        "statusLabel": run.get_status_display(),
        "invalidReason": run.invalid_reason,
        "curveStatus": run.curve_status or CompoundingIoTRun.CURVE_NO_STANDARD,
        "curveStatusLabel": run.get_curve_status_display() if run.curve_status else "No standard",
        "outOfBandSeconds": run.out_of_band_seconds,
        "energyDeviationPct": run.energy_deviation_pct,
        "energyOutOfBand": run.energy_out_of_band,
        "labResult": run.lab_result,
        "labResultLabel": run.get_lab_result_display(),
        "source": run.detection_source,
        "sourceLabel": run.get_detection_source_display(),
        "visualCheck": run.get_stock_quality_display() if run.stock_quality else "",
        "rejectionReason": run.get_rejection_reason_display(),
    }


# Lab results ----------------------------------------------------------------------------------------------
def _parse_lab_values(raw: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Lab values as a flat {name: value} object, from JSON or from "name: value" lines."""
    if raw in (None, ""):
        return {}, ""
    if isinstance(raw, dict):
        values = raw
    else:
        text = str(raw).strip()
        if text.startswith("{"):
            try:
                values = json.loads(text)
            except json.JSONDecodeError:
                return None, "Lab values must be a JSON object or one 'name: value' per line."
        else:
            values = {}
            for line in text.splitlines():
                if not line.strip():
                    continue
                name, sep, value = line.partition(":") if ":" in line else line.partition("=")
                if not sep or not name.strip():
                    return None, f"Could not read {line.strip()!r}; write one 'name: value' per line."
                values[name.strip()] = value.strip()
    if not isinstance(values, dict):
        return None, "Lab values must be an object of name/value pairs."
    if len(values) > _MAX_LAB_VALUES:
        return None, f"At most {_MAX_LAB_VALUES} lab values per batch."
    cleaned: Dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, (dict, list)):
            return None, f"Lab value {name!r} must be a number or text."
        if isinstance(value, str):
            text = value.strip()
            try:
                number = float(text)
            except ValueError:
                value = text[:200]
            else:
                value = number if math.isfinite(number) else text
        cleaned[str(name)[:60]] = value
    return cleaned, ""


def _save_lab_result(run: CompoundingIoTRun, result: str, values: Dict[str, Any], remarks: str, user) -> None:
    run.lab_result = result
    run.lab_values = values
    run.lab_remarks = remarks[:2000]
    run.lab_result_at = timezone.now() if result != CompoundingIoTRun.LAB_PENDING else None
    run.lab_result_by = user if result != CompoundingIoTRun.LAB_PENDING else None
    run.save(update_fields=["lab_result", "lab_values", "lab_remarks", "lab_result_at", "lab_result_by", "updated_at"])


@login_required
@require_any_service_access(_IOT_SERVICES, deny="json")
@require_http_methods(["POST"])
def lab_result_api(request, batch_id: int):
    """POST /api/batches/<id>/lab-result/ with {"result": "pass"|"fail"|"pending", "values": {...}, "remarks": ""}.

    Accepts a JSON body or a form post; a form post (the batch page) is redirected back to the batch.
    """
    _require_dashboard()
    run = CompoundingIoTRun.objects.filter(pk=batch_id).first()
    is_json = (request.content_type or "").startswith("application/json")
    if is_json:
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return JsonResponse({"ok": False, "error": "Send a JSON object"}, status=400)
    else:
        body = {key: request.POST.get(key, "") for key in ("result", "values", "remarks")}

    def fail(message: str, status: int = 400):
        if is_json:
            return JsonResponse({"ok": False, "error": message}, status=status)
        messages.error(request, message)
        return redirect(reverse("compounding:iot_batch_detail", args=[batch_id]) if run else reverse("compounding:iot_dashboard"))

    if run is None:
        return fail("Batch not found", status=404)
    result = str(body.get("result") or "").strip().lower()
    if result not in {choice[0] for choice in CompoundingIoTRun.LAB_RESULT_CHOICES}:
        return fail("result must be pass, fail or pending")
    values, error = _parse_lab_values(body.get("values"))
    if values is None:
        return fail(error)
    remarks = str(body.get("remarks") or "").strip()

    _save_lab_result(run, result, values, remarks, request.user)
    if is_json:
        return JsonResponse({"ok": True, "batch": {**batch_row(run), "labValues": run.lab_values,
                                                    "labRemarks": run.lab_remarks}},
                            json_dumps_params={"default": str})
    messages.success(request, f"Saved the lab result for batch {run.batch_id}: {run.get_lab_result_display()}.")
    return redirect("compounding:iot_batch_detail", batch_id=run.pk)


# Batch detail ---------------------------------------------------------------------------------------------
@login_required
@require_any_service_access(_IOT_SERVICES)
@require_http_methods(["GET"])
def batch_detail(request, batch_id: int):
    _require_dashboard()
    run = get_object_or_404(CompoundingIoTRun.objects.select_related("plan", "crin", "process_group", "standard_curve"),
                            pk=batch_id)
    standard = run.standard_curve or iot_curves.active_curve(run.process_group)
    curve_points: List[Dict[str, Any]] = []
    if run.load_start_ts and run.ended_at:
        curve_points = [{"t": t, "value": round(value, 2)} for t, value in iot_curves.batch_curve(run)]
    trace: List[Dict[str, Any]] = []
    if run.ended_at:
        trace = iot_samples.downsample_points(
            iot_samples.compact_history(run.machine_id, start_ts=_ms(run.started_at), end_ts=_ms(run.ended_at)),
            1500,
        )
    chart = {
        "curve": curve_points,
        "standard": standard.points if standard else [],
        "standardLabel": f"{standard.process_group.name} v{standard.version}" if standard else "",
        "trace": trace,
        "loadStartTs": _ms(run.load_start_ts),
    }
    compound_options = [option["value"] for option in _iot_run_compound_options(run.plan)]
    return render(
        request,
        "magicwand/iot_batch_detail.html",
        {
            "run": run,
            "row": batch_row(run),
            "standard": standard,
            "chart": chart,
            "lab_choices": CompoundingIoTRun.LAB_RESULT_CHOICES,
            "lab_values_text": "\n".join(f"{name}: {value}" for name, value in (run.lab_values or {}).items()),
            "compound_options": compound_options,
            "min_batch_minutes": iot_config.min_batch_seconds() // 60,
            "iot_time_zone": "Asia/Kolkata",
            "dashboard_url": f"{reverse('compounding:iot_dashboard')}?date={run.plan.plan_date.isoformat()}",
        },
    )


@login_required
@require_any_service_access(_IOT_SERVICES)
@require_http_methods(["POST"])
def assign_batch_compound(request, batch_id: int):
    """Name the compound of a batch, typically one the machine detected as UNASSIGNED."""
    _require_dashboard()
    run = get_object_or_404(CompoundingIoTRun, pk=batch_id)
    compound_code = (request.POST.get("compound_code") or "").strip()
    allowed = {option["value"] for option in _iot_run_compound_options(run.plan)} | set(
        ProcessGroupCompound.objects.values_list("compound_code", flat=True)
    )
    if not compound_code or (allowed and compound_code not in allowed):
        messages.error(request, "Select a compound from MB planning or a process group.")
        return redirect("compounding:iot_batch_detail", batch_id=run.pk)
    run.compound_code = compound_code
    run.operator = (request.POST.get("operator") or run.operator or "").strip()[:120]
    run.save(update_fields=["compound_code", "operator", "updated_at"])
    if run.ended_at:
        iot_batches.finalize_batch(run)  # picks up the compound's process group and curve
    messages.success(request, f"Batch {run.batch_id} is now {compound_code}.")
    return redirect("compounding:iot_batch_detail", batch_id=run.pk)


# Process groups -------------------------------------------------------------------------------------------
def _group_summary(group: ProcessGroup) -> Dict[str, Any]:
    curve = iot_curves.active_curve(group)
    return {
        "group": group,
        "compounds": [link.compound_code for link in group.compounds.all()],
        "curve": curve,
        "batch_count": group.batches.filter(status=CompoundingIoTRun.STATUS_COMPLETED).count(),
        "eligible_count": iot_curves.curve_source_batches(group).count(),
    }


@login_required
@require_any_service_access(_IOT_SERVICES)
@require_http_methods(["GET", "POST"])
def process_groups(request):
    _require_dashboard()
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:80]
        if not name:
            messages.error(request, "Give the process group a name.")
            return redirect("compounding:iot_process_groups")
        try:
            with transaction.atomic():
                group = ProcessGroup.objects.create(
                    name=name, description=(request.POST.get("description") or "").strip()
                )
        except IntegrityError:
            messages.error(request, f"A process group called {name} already exists.")
            return redirect("compounding:iot_process_groups")
        messages.success(request, f"Created process group {group.name}. Add its compounds below.")
        return redirect("compounding:iot_process_group_detail", group_id=group.pk)
    groups = [_group_summary(group) for group in ProcessGroup.objects.prefetch_related("compounds")]
    return render(
        request,
        "magicwand/iot_process_groups.html",
        {"groups": groups, "dashboard_url": reverse("compounding:iot_dashboard"),
         "min_batches": iot_config.curve_min_batches()},
    )


def _scatter_points(group: ProcessGroup) -> List[Dict[str, Any]]:
    batches = (
        CompoundingIoTRun.objects.filter(
            process_group=group, status=CompoundingIoTRun.STATUS_COMPLETED,
            energy_kwh__isnull=False, peak_current__isnull=False,
        )
        .order_by("-started_at")[:1000]
    )
    return [
        {"x": run.peak_current, "y": run.energy_kwh, "compound": run.compound_code, "id": run.pk,
         "lab": run.lab_result, "label": run.batch_id}
        for run in batches
    ]


@login_required
@require_any_service_access(_IOT_SERVICES)
@require_http_methods(["GET", "POST"])
def process_group_detail(request, group_id: int):
    _require_dashboard()
    group = get_object_or_404(ProcessGroup, pk=group_id)
    if request.method == "POST":
        action = request.POST.get("action") or ""
        code = (request.POST.get("compound_code") or "").strip()
        if action == "add" and code:
            existing = ProcessGroupCompound.objects.filter(compound_code=code).select_related("process_group").first()
            if existing and existing.process_group_id != group.pk:
                messages.error(request, f"{code} is already in {existing.process_group.name}. Remove it there first.")
            elif not existing:
                ProcessGroupCompound.objects.create(process_group=group, compound_code=code)
                # Earlier batches of this compound now belong to the group.
                CompoundingIoTRun.objects.filter(compound_code=code, process_group__isnull=True).update(process_group=group)
                messages.success(request, f"Added {code} to {group.name}.")
        elif action == "remove" and code:
            ProcessGroupCompound.objects.filter(process_group=group, compound_code=code).delete()
            CompoundingIoTRun.objects.filter(compound_code=code, process_group=group).update(process_group=None)
            messages.success(request, f"Removed {code} from {group.name}.")
        else:
            messages.error(request, "Choose a compound.")
        return redirect("compounding:iot_process_group_detail", group_id=group.pk)

    summary = _group_summary(group)
    taken = set(ProcessGroupCompound.objects.values_list("compound_code", flat=True))
    from .models import CompoundingPlan, compounding_business_date

    today_plan = CompoundingPlan.objects.filter(plan_date=compounding_business_date()).first()
    candidates = sorted(
        set(_available_master_compounds(today_plan) if today_plan else [])
        | set(CompoundingIoTRun.objects.exclude(compound_code=CompoundingIoTRun.UNASSIGNED_COMPOUND)
              .values_list("compound_code", flat=True).distinct())
    )
    return render(
        request,
        "magicwand/iot_process_group_detail.html",
        {
            **summary,
            "curves": group.curves.order_by("-version")[:10],
            "addable_compounds": [code for code in candidates if code and code not in taken],
            "scatter": _scatter_points(group),
            "min_batches": iot_config.curve_min_batches(),
            "groups_url": reverse("compounding:iot_process_groups"),
        },
    )


@login_required
@require_any_service_access(_IOT_SERVICES)
@require_http_methods(["POST"])
def rebuild_curve(request, group_id: int):
    _require_dashboard()
    group = get_object_or_404(ProcessGroup, pk=group_id)
    try:
        curve = iot_curves.build_standard_curve(group, user=request.user)
    except iot_curves.CurveBuildError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Built {group.name} curve v{curve.version} from {curve.built_from_n} lab-passed batches. "
            "Batches ending from now on are compared with it.",
        )
    return redirect("compounding:iot_process_group_detail", group_id=group.pk)
