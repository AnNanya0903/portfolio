"""Labels for detected batches, and the versioned standard graph per compound or process group.

A detected batch's label is its supervisor tag, else the compound of the operator log it overlaps by at least
IOT_LABEL_MIN_OVERLAP (70 %, intersection over union). Standards are built from labelled, completed, normal
batches (not excluded): at least IOT_STANDARD_MIN_BATCHES (15; 30 recommended), aligned at the load point,
resampled every 10 s, with the median and a 5th-95th percentile (or mean +/- 2 sigma) band.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from django.db import transaction
from django.utils import timezone

from . import iot_config
from .models import CompoundingIoTRun, DetectedBatch, ProcessGroup, ProcessGroupCompound, StandardCurve


class StandardBuildError(ValueError):
    pass


# Keys ------------------------------------------------------------------------------------------------------
def group_for(compound_code: str) -> Optional[ProcessGroup]:
    link = ProcessGroupCompound.objects.select_related("process_group").filter(compound_code=compound_code).first()
    return link.process_group if link else None


def key_for(compound_code: str) -> Tuple[str, str, Optional[ProcessGroup]]:
    """(standard key, display label, group) for a compound: its process group when it has one."""
    group = group_for(compound_code) if compound_code else None
    if group is not None:
        return f"group:{group.pk}", group.name, group
    return f"compound:{compound_code}", compound_code, None


def compounds_for_key(key: str) -> List[str]:
    kind, _, value = key.partition(":")
    if kind == "group":
        return list(ProcessGroupCompound.objects.filter(process_group_id=int(value)).values_list("compound_code", flat=True))
    return [value]


def active_standard(key: str) -> Optional[StandardCurve]:
    return StandardCurve.objects.filter(key=key, is_active=True).order_by("-version").first()


def active_standards() -> List[StandardCurve]:
    return list(StandardCurve.objects.filter(is_active=True).select_related("process_group").order_by("key"))


# Labels ----------------------------------------------------------------------------------------------------
def overlap_ratio(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    """Intersection over union of two time spans."""
    intersection = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    if intersection <= 0:
        return 0.0
    union = (max(a_end, b_end) - min(a_start, b_start)).total_seconds()
    return intersection / union if union > 0 else 0.0


def log_end(run: CompoundingIoTRun) -> datetime:
    return run.ended_at or max(timezone.now(), run.started_at)


def best_log_for(batch: DetectedBatch) -> Tuple[Optional[CompoundingIoTRun], float]:
    """The operator log (same machine) that overlaps the batch most, with the overlap ratio."""
    candidates = CompoundingIoTRun.objects.filter(
        machine_id=batch.machine_id,
        started_at__lt=batch.end_at,
    ).exclude(ended_at__lte=batch.start_at)
    best, best_ratio, best_seconds = None, 0.0, 0.0
    for run in candidates:
        end = log_end(run)
        seconds = (min(end, batch.end_at) - max(run.started_at, batch.start_at)).total_seconds()
        if seconds > best_seconds:
            best, best_seconds = run, seconds
            best_ratio = overlap_ratio(batch.start_at, batch.end_at, run.started_at, end)
    return best, best_ratio


def refresh_label(batch: DetectedBatch) -> None:
    """Set log_run, log_overlap and label_compound (tag first, else the log when it overlaps enough)."""
    run, ratio = best_log_for(batch)
    batch.log_run = run
    batch.log_overlap = round(ratio, 3) if run else None
    if batch.tagged_compound:
        batch.label_compound = batch.tagged_compound
    elif run is not None and ratio >= iot_config.label_min_overlap() and not batch.is_open:
        batch.label_compound = run.compound_code
    else:
        batch.label_compound = ""


def reference_batches(key: str):
    """Completed, normal, labelled batches for a standard key."""
    return DetectedBatch.objects.filter(
        label_compound__in=compounds_for_key(key),
        excluded=False,
        is_open=False,
        load_at__isnull=False,
    ).exclude(curve=[])


# Building --------------------------------------------------------------------------------------------------
def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    position = (len(ordered) - 1) * pct / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _band(values: Sequence[float]) -> Tuple[float, float, float]:
    median = statistics.median(values)
    if iot_config.standard_band_method() == "sigma":
        mean = statistics.fmean(values)
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        sigma = iot_config.standard_sigma()
        return median, max(mean - sigma * spread, 0.0), mean + sigma * spread
    low_pct, high_pct = iot_config.standard_percentiles()
    return median, percentile(values, low_pct), percentile(values, high_pct)


def _stat(values: Iterable[Optional[float]]) -> Optional[Dict[str, float]]:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    median, low, high = _band(numbers)
    return {"median": round(median, 2), "low": round(low, 2), "high": round(high, 2)}


def build_standard(key: str, *, user=None, min_batches: Optional[int] = None, force: bool = False) -> StandardCurve:
    """Build the next version of a standard and make it active. Unchanged reference sets are not rebuilt
    unless force=True (the weekly rebuild uses that to skip groups with no new batches)."""
    min_batches = iot_config.standard_min_batches() if min_batches is None else min_batches
    batches = list(reference_batches(key).order_by("start_at"))
    compound_code = "" if key.startswith("group:") else key.partition(":")[2]
    label = compound_code or key
    group = None
    if key.startswith("group:"):
        group = ProcessGroup.objects.filter(pk=int(key.partition(":")[2])).first()
        if group is None:
            raise StandardBuildError(f"No process group for {key}")
        label = group.name
    if len(batches) < min_batches:
        raise StandardBuildError(
            f"{label} has {len(batches)} normal labelled batches; at least {min_batches} are needed "
            f"(30 recommended)."
        )
    source_ids = [batch.pk for batch in batches]
    current = active_standard(key)
    if current is not None and not force and sorted(current.source_batch_ids or []) == sorted(source_ids):
        return current

    step = iot_config.curve_step_s()
    by_index: Dict[int, List[float]] = {}
    for batch in batches:
        for index, value in enumerate(batch.curve or []):
            by_index.setdefault(index, []).append(float(value))
    needed = max(3, math.ceil(len(batches) / 2))
    points = []
    for index in sorted(by_index):
        values = by_index[index]
        if len(values) < needed:
            continue
        median, low, high = _band(values)
        points.append({"t": index * step, "median": round(median, 1), "lower": round(low, 1), "upper": round(high, 1)})

    stats: Dict[str, Any] = {}
    for name in ("duration_s", "energy_kwh", "peak_current", "time_to_peak_s", "steady_len_s", "steady_mean"):
        stat = _stat((batch.features or {}).get(name, getattr(batch, name, None)) for batch in batches)
        if stat is not None:
            stats[name] = stat

    with transaction.atomic():
        latest = StandardCurve.objects.select_for_update().filter(key=key).order_by("-version").first()
        StandardCurve.objects.filter(key=key, is_active=True).update(is_active=False)
        return StandardCurve.objects.create(
            key=key,
            compound_code=compound_code,
            process_group=group,
            version=(latest.version if latest else 0) + 1,
            built_from_n=len(batches),
            source_batch_ids=source_ids,
            step_s=step,
            band_method=iot_config.standard_band_method(),
            points=points,
            stats=stats,
            is_active=True,
            created_by=user,
        )


def candidate_keys() -> List[Tuple[str, str, int]]:
    """(key, label, reference batch count) for every compound or group that has labelled batches."""
    counts: Dict[str, Tuple[str, int]] = {}
    labels = DetectedBatch.objects.filter(excluded=False, is_open=False).exclude(label_compound="")
    for compound in labels.values_list("label_compound", flat=True).distinct():
        key, label, _group = key_for(compound)
        if key not in counts:
            counts[key] = (label, reference_batches(key).count())
    return sorted(((key, label, count) for key, (label, count) in counts.items()), key=lambda item: item[1])


def rebuild_all(*, user=None) -> List[Dict[str, Any]]:
    """Rebuild every standard with enough batches (weekly cron). Unchanged reference sets are skipped."""
    report = []
    for key, label, count in candidate_keys():
        if count < iot_config.standard_min_batches():
            report.append({"key": key, "label": label, "built": False, "batches": count})
            continue
        before = active_standard(key)
        standard = build_standard(key, user=user)
        report.append({"key": key, "label": label, "built": before is None or standard.pk != before.pk,
                       "version": standard.version, "batches": count})
    return report
