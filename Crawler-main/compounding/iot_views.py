"""IoT pages beyond the dashboard itself: the machine-vs-log table (live partial, CSV, accepting machine times),
detected-batch detail, tagging past batches, and standards / process groups."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from magicwand.access import require_any_service_access, require_service_access, user_has_service_access

from . import iot_config, iot_reconcile, iot_segments
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


# Detected batch detail ------------------------------------------------------------------------------------
def _compound_choices(day: Optional[date] = None) -> list:
    """Compounds to tag with: MB planning / master sheet of the day, plus every compound seen in logs or tags."""
    from .models import CompoundingPlan, ProcessGroupCompound
    from .views import _iot_run_compound_options

    codes = set(CompoundingIoTRun.objects.values_list("compound_code", flat=True).distinct())
    codes |= set(DetectedBatch.objects.exclude(tagged_compound="").values_list("tagged_compound", flat=True).distinct())
    codes |= set(ProcessGroupCompound.objects.values_list("compound_code", flat=True))
    plan = CompoundingPlan.objects.filter(plan_date=day or iot_segments.today_ist()).first()
    if plan is not None:
        codes |= {option["value"] for option in _iot_run_compound_options(plan)}
    return sorted(code for code in codes if code)


@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET"])
def detected_batch_detail(request, batch_id: int):
    _require_dashboard()
    batch = get_object_or_404(
        DetectedBatch.objects.select_related("log_run", "standard", "standard__process_group", "tagged_by"),
        pk=batch_id,
    )
    standard = batch.standard
    chart = {
        "curve": batch.curve or [],
        "step": batch.curve_step_s,
        "standard": standard.points if standard else [],
        "standardLabel": f"{standard.label} v{standard.version}" if standard else "",
        "aligned": batch.load_at is not None,
    }
    stats = (standard.stats or {}) if standard else {}
    day = batch.start_at.astimezone(iot_segments.IST_TIMEZONE).date()
    return render(
        request,
        "magicwand/iot_detected_batch.html",
        {
            "batch": batch,
            "standard": standard,
            "stats": stats,
            "chart": chart,
            "features": batch.features or {},
            "can_tag": is_supervisor(request.user),
            "compound_choices": _compound_choices(day) if is_supervisor(request.user) else [],
            "dashboard_url": f"{reverse('compounding:iot_dashboard')}?date={day.isoformat()}#iot-recon",
            "iot_time_zone": "Asia/Kolkata",
        },
    )


def _apply_tag(batch: DetectedBatch, compound: str, excluded: bool, reason: str, user) -> bool:
    from django.utils import timezone

    compound = (compound or "").strip()[:64]
    reason = (reason or "").strip()[:200]
    changed = (compound != batch.tagged_compound) or (excluded != batch.excluded) or (reason != batch.excluded_reason)
    if not changed:
        return False
    batch.tagged_compound = compound
    batch.tagged_by = user if compound else None
    batch.tagged_at = timezone.now() if compound else None
    batch.excluded = excluded
    batch.excluded_reason = reason if excluded else ""
    batch.save(update_fields=["tagged_compound", "tagged_by", "tagged_at", "excluded", "excluded_reason", "updated_at"])
    from . import iot_patterns

    iot_patterns.analyze_batch(batch)
    return True


@login_required
@require_service_access(SUPERVISOR_SERVICE)
@require_http_methods(["POST"])
def tag_detected_batch(request, batch_id: int):
    _require_dashboard()
    batch = get_object_or_404(DetectedBatch, pk=batch_id)
    _apply_tag(batch, request.POST.get("compound_code") or "", request.POST.get("excluded") == "1",
               request.POST.get("excluded_reason") or "", request.user)
    messages.success(request, "Saved the batch's tag.")
    return redirect("compounding:iot_detected_batch", batch_id=batch.pk)


# Tagging past batches (one-time seeding of the reference set) ------------------------------------------------
@login_required
@require_service_access(SUPERVISOR_SERVICE)
@require_http_methods(["GET", "POST"])
def tag_batches(request):
    _require_dashboard()
    machine_id = _iot_dashboard_machine_id()
    day = recon_day(request)
    start_ts, end_ts = iot_segments.day_bounds_ms(day)
    batches = list(
        DetectedBatch.objects.filter(
            machine_id=machine_id,
            start_at__gte=iot_segments.ms_to_datetime(start_ts),
            start_at__lt=iot_segments.ms_to_datetime(end_ts),
            is_open=False,
        ).select_related("log_run").order_by("start_at")
    )
    if request.method == "POST":
        changed = 0
        for batch in batches:
            prefix = f"b{batch.pk}-"
            if f"{prefix}present" not in request.POST:
                continue
            changed += _apply_tag(batch, request.POST.get(f"{prefix}compound") or "",
                                  request.POST.get(f"{prefix}excluded") == "1",
                                  request.POST.get(f"{prefix}reason") or "", request.user)
        messages.success(request, f"Saved {changed} batch tag{'s' if changed != 1 else ''} for {day:%d %b %Y}.")
        return redirect(f"{reverse('compounding:iot_tag_batches')}?date={day.isoformat()}")

    from . import iot_standards

    counts = {}
    for key, label, count in iot_standards.candidate_keys():
        counts[label] = count
    return render(
        request,
        "magicwand/iot_tag_batches.html",
        {
            "batches": batches,
            "day": day,
            "previous_day": (day - timedelta(days=1)).isoformat(),
            "next_day": (day + timedelta(days=1)).isoformat(),
            "compound_choices": _compound_choices(day),
            "label_counts": sorted(counts.items()),
            "min_batches": iot_config.standard_min_batches(),
            "standards_url": reverse("compounding:iot_standards"),
            "iot_time_zone": "Asia/Kolkata",
        },
    )


# Standards and process groups -------------------------------------------------------------------------------
@login_required
@require_any_service_access(IOT_SERVICES)
@require_http_methods(["GET", "POST"])
def standards(request):
    _require_dashboard()
    from django.db import IntegrityError, transaction

    from . import iot_patterns, iot_standards
    from .models import ProcessGroup, ProcessGroupCompound, StandardCurve

    supervisor = is_supervisor(request.user)
    if request.method == "POST":
        if not supervisor:
            messages.error(request, "Only Compounding planning can change standards and process groups.")
            return redirect("compounding:iot_standards")
        action = request.POST.get("action") or ""
        if action == "rebuild":
            key = request.POST.get("key") or ""
            try:
                standard = iot_standards.build_standard(key, user=request.user, force=True)
            except iot_standards.StandardBuildError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Built {standard.label} v{standard.version} from {standard.built_from_n} batches.")
        elif action == "create_group":
            name = (request.POST.get("name") or "").strip()[:80]
            try:
                with transaction.atomic():
                    ProcessGroup.objects.create(name=name, description=(request.POST.get("description") or "").strip())
            except IntegrityError:
                messages.error(request, f"A process group called {name} already exists.")
            else:
                if name:
                    messages.success(request, f"Created process group {name}.")
        elif action in {"add_compound", "remove_compound"}:
            group = ProcessGroup.objects.filter(pk=request.POST.get("group_id") or 0).first()
            code = (request.POST.get("compound_code") or "").strip()
            if group is None or not code:
                messages.error(request, "Choose a group and a compound.")
            elif action == "add_compound":
                existing = ProcessGroupCompound.objects.filter(compound_code=code).select_related("process_group").first()
                if existing and existing.process_group_id != group.pk:
                    messages.error(request, f"{code} is already in {existing.process_group.name}.")
                elif not existing:
                    ProcessGroupCompound.objects.create(process_group=group, compound_code=code)
                    messages.success(request, f"Added {code} to {group.name}. Rebuild the group's standard to use it.")
            else:
                ProcessGroupCompound.objects.filter(process_group=group, compound_code=code).delete()
                messages.success(request, f"Removed {code} from {group.name}.")
        return redirect("compounding:iot_standards")

    active = iot_standards.active_standards()
    candidates = iot_standards.candidate_keys()
    rows = []
    active_by_key = {standard.key: standard for standard in active}
    for key, label, count in candidates:
        rows.append({"key": key, "label": label, "count": count, "standard": active_by_key.pop(key, None)})
    for key, standard in active_by_key.items():
        rows.append({"key": key, "label": standard.label, "count": 0, "standard": standard})
    # Standards whose median curves are nearly the same: candidates for one process group.
    similar = []
    for index, first in enumerate(active):
        for second in active[index + 1:]:
            distance = iot_patterns.dtw_distance([p["median"] for p in first.points], [p["median"] for p in second.points])
            if distance <= iot_config.similar_standards_distance():
                similar.append({"a": first.label, "b": second.label, "distance": round(distance, 1)})
    return render(
        request,
        "magicwand/iot_standards.html",
        {
            "rows": rows,
            "similar": similar,
            "groups": ProcessGroup.objects.prefetch_related("compounds"),
            "history": StandardCurve.objects.select_related("process_group").order_by("-created_at")[:20],
            "min_batches": iot_config.standard_min_batches(),
            "supervisor": supervisor,
            "compound_choices": _compound_choices() if supervisor else [],
            "tag_url": reverse("compounding:iot_tag_batches"),
            "dashboard_url": reverse("compounding:iot_dashboard"),
        },
    )
