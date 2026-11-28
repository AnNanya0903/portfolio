"""The "Compounds run" batch log: its table rows, editing, confirming and deleting entries, and the confirmed
counts the Compounding Board shows as Actual."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from magicwand.access import require_any_service_access, require_service_access

from . import iot_autolog, iot_compare, iot_segments
from .iot_views import ADMIN_SERVICE, IOT_SERVICES, _require_dashboard, is_admin
from .models import IST_TIMEZONE, CompoundingCRIN, CompoundingIoTRun as Run, CompoundingPlan, DetectedBatch
from .views import _iot_run_compound_options, _iot_run_duration_text, _parse_batch_weight


def _detected(run: Run):
    return DetectedBatch.objects.filter(log_run=run).first()


def run_rows(machine_id: str, day: date) -> List[Dict[str, Any]]:
    start_ts, end_ts = iot_segments.day_bounds_ms(day)
    runs = (
        Run.objects.filter(
            machine_id=machine_id,
            started_at__gte=iot_segments.ms_to_datetime(start_ts),
            started_at__lt=iot_segments.ms_to_datetime(end_ts),
        )
        .select_related("crin", "detected_batch")
        .order_by("started_at", "id")
    )
    rows = []
    for run in runs:
        batch = getattr(run, "detected_batch", None)
        unassigned = run.compound_code == Run.UNASSIGNED
        to_confirm = run.compound_needs_confirm or run.crin_needs_confirm
        pending = bool(run.ended_at) and not run.stock_quality and not run.flag
        rows.append({
            "run": run,
            "start": run.started_at.astimezone(IST_TIMEZONE).strftime("%H:%M"),
            "end": run.ended_at.astimezone(IST_TIMEZONE).strftime("%H:%M") if run.ended_at else "running",
            "time_taken": _iot_run_duration_text(run.started_at, run.ended_at),
            "quality": run.get_stock_quality_display() if run.stock_quality else "Pending",
            "unassigned": unassigned,
            "pending": pending,
            "needs_action": not run.flag and (unassigned or to_confirm or pending),
            "can_confirm": to_confirm and not unassigned,
            "looks_like": batch.looks_like if batch else "",
        })
    return rows


def board_iot_counts(plan: CompoundingPlan, lines) -> Dict[int, int]:
    """Confirmed mill batches per board line: matched on compound and CRIN, else the compound's first line."""
    counts: Dict[int, int] = {}
    on_mill = set(DetectedBatch.objects.filter(log_run__plan=plan).values_list("log_run_id", flat=True))
    for run in iot_autolog.confirmed_runs(plan):
        if run.pk not in on_mill:
            continue
        same = [line for line in lines if (line.compound_code or "").strip() == run.compound_code]
        if not same:
            continue
        line = next((item for item in same if run.crin_id and item.crin_request_id == run.crin_id), same[0])
        counts[line.id] = counts.get(line.id, 0) + 1
    return counts


def _back(run: Run) -> str:
    day = run.started_at.astimezone(IST_TIMEZONE).date().isoformat()
    return f"{reverse('compounding:iot_dashboard')}?date={day}#compounds-run"


def _sync_batch(run: Run) -> None:
    batch = _detected(run)
    if batch is None:
        return
    batch.compound_code = "" if run.compound_code == Run.UNASSIGNED else run.compound_code
    batch.save(update_fields=["compound_code", "updated_at"])
    iot_compare.compare_batch(batch)


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET", "POST"])
def edit_run(request, run_id: int):
    _require_dashboard()
    run = get_object_or_404(Run.objects.select_related("crin", "plan"), pk=run_id)
    compounds = sorted(
        {option["value"] for option in _iot_run_compound_options(run.plan)}
        | ({run.compound_code} if run.compound_code != Run.UNASSIGNED else set())
    )
    open_crins = CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN).order_by("crin_number")
    crins = list(open_crins)
    if run.crin and run.crin not in crins:
        crins.append(run.crin)

    if request.method == "POST":
        compound = (request.POST.get("compound_code") or "").strip()
        quality = (request.POST.get("stock_quality") or "").strip()
        reason = (request.POST.get("rejection_reason") or "").strip()
        crin_id = (request.POST.get("crin_id") or "").strip()
        weight, weight_ok = _parse_batch_weight(request.POST.get("batch_weight_kg") or "")
        crin = next((item for item in crins if str(item.pk) == crin_id), None)
        error = ""
        if compound != Run.UNASSIGNED and compound not in compounds:
            error = "Choose a compound from the list."
        elif quality and quality not in dict(Run.QUALITY_CHOICES):
            error = "Choose a valid quality."
        elif quality == Run.QUALITY_REJECTED and reason not in dict(Run.REJECTION_REASON_CHOICES):
            error = "Say why the batch was rejected."
        elif not weight_ok:
            error = "Enter the weight as a number above 0, or leave it blank."
        elif crin_id and crin is None:
            error = "That CRIN is not open."
        if error:
            messages.error(request, error)
        else:
            with transaction.atomic():
                before = iot_autolog.snapshot(run)
                run.compound_code = compound
                run.crin = crin
                run.batch_weight_kg = weight
                run.stock_quality = quality
                run.rejection_reason = reason if quality == Run.QUALITY_REJECTED else ""
                run.notes = (request.POST.get("notes") or "").strip()[:2000]
                # Saving the form is the person's confirmation of what it shows.
                run.compound_needs_confirm = run.crin_needs_confirm = False
                run.save()
                iot_autolog.audit(run, "edited", before, request.user)
            _sync_batch(run)
            messages.success(request, "Saved the log entry.")
            return redirect(_back(run))

    return render(request, "magicwand/iot_run_edit.html", {
        "run": run,
        "batch": _detected(run),
        "compounds": compounds,
        "crins": crins,
        "unassigned": Run.UNASSIGNED,
        "quality_choices": Run.QUALITY_CHOICES,
        "reason_choices": Run.REJECTION_REASON_CHOICES,
        "audits": run.audits.select_related("changed_by")[:20],
        "can_delete": is_admin(request.user),
        "back_url": _back(run),
        "iot_time_zone": "Asia/Kolkata",
    })


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["POST"])
def confirm_run(request, run_id: int):
    _require_dashboard()
    run = get_object_or_404(Run, pk=run_id)
    if run.compound_code == Run.UNASSIGNED:
        messages.error(request, "Choose the compound first.")
        return redirect("compounding:iot_run_edit", run_id=run.pk)
    if run.compound_needs_confirm or run.crin_needs_confirm:
        before = iot_autolog.snapshot(run)
        run.compound_needs_confirm = run.crin_needs_confirm = False
        run.save(update_fields=["compound_needs_confirm", "crin_needs_confirm", "updated_at"])
        iot_autolog.audit(run, "confirmed", before, request.user)
        _sync_batch(run)
        messages.success(request, f"Confirmed {run.compound_code}.")
    return redirect(_back(run))


@login_required
@require_service_access(ADMIN_SERVICE)
@require_http_methods(["POST"])
def delete_run(request, run_id: int):
    _require_dashboard()
    run = get_object_or_404(Run, pk=run_id)
    if request.POST.get("confirm_delete") != "1":
        messages.error(request, "Tick the confirmation box to delete the entry.")
        return redirect("compounding:iot_run_edit", run_id=run.pk)
    back = _back(run)
    with transaction.atomic():
        batch = _detected(run)
        if batch is not None:
            # Remember the deletion so the auto-log does not create the entry again.
            batch.log_run, batch.log_suppressed, batch.compound_code = None, True, ""
            batch.save(update_fields=["log_run", "log_suppressed", "compound_code", "updated_at"])
        iot_autolog.audit(run, "deleted", iot_autolog.snapshot(run), request.user)
        run.delete()
    messages.success(request, "Deleted the log entry.")
    return redirect(back)
