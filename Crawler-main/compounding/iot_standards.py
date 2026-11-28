"""The standard current graph of each compound, built from its normal detected batches.

Normal batches: finished, linked to the compound through the operator's entry, with a load point, and not
excluded by an admin. A standard needs IOT_STANDARD_MIN_BATCHES (15; 30 recommended). Curves are aligned at
load_ts and resampled every 10 s; the standard is the median with a 5th-95th percentile band. The standard
duration, energy, peak, time to peak and steady current are stored as median + allowed range (same
percentiles, never narrower than the minimum ranges in iot_config.min_allowed_range). Standards are versioned; rebuilding with an unchanged set of batches keeps the current version.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from django.db import transaction

from . import iot_config
from .models import DetectedBatch, StandardCurve

STAT_FIELDS = ("duration_s", "energy_kwh", "peak_current", "time_to_peak_s", "steady_current")


class StandardBuildError(ValueError):
    pass


def active_standard(compound_code: str) -> Optional[StandardCurve]:
    if not compound_code:
        return None
    return StandardCurve.objects.filter(compound_code=compound_code, is_active=True).order_by("-version").first()


def active_standards() -> List[StandardCurve]:
    return list(StandardCurve.objects.filter(is_active=True).order_by("compound_code"))


def normal_batches(compound_code: str):
    return DetectedBatch.objects.filter(
        compound_code=compound_code,
        excluded=False,
        is_open=False,
        load_at__isnull=False,
    ).exclude(curve=[])


def readiness(compound_code: str) -> Tuple[int, int]:
    """(normal batches so far, batches needed)."""
    return normal_batches(compound_code).count(), iot_config.standard_min_batches()


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    position = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def band(values: Sequence[float]) -> Tuple[float, float, float]:
    """(median, low, high) with low/high at the configured percentiles."""
    low_pct, high_pct = iot_config.standard_percentiles()
    return statistics.median(values), percentile(values, low_pct), percentile(values, high_pct)


def _stat(name: str, values: Iterable[Optional[float]]) -> Optional[Dict[str, float]]:
    """Median and allowed range (5th-95th percentile, widened to at least the configured minimum)."""
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    median, low, high = band(numbers)
    margin = iot_config.min_allowed_range(name, median)
    low, high = min(low, median - margin), max(high, median + margin)
    return {"median": round(median, 2), "low": round(low, 2), "high": round(high, 2)}


def build_standard(compound_code: str, *, user=None, force: bool = False) -> StandardCurve:
    """Build the compound's next standard version and make it active."""
    minimum = iot_config.standard_min_batches()
    batches = list(normal_batches(compound_code).order_by("start_at"))
    if len(batches) < minimum:
        raise StandardBuildError(
            f"Standard not ready for {compound_code} ({len(batches)}/{minimum} normal batches; 30 recommended)."
        )
    source_ids = sorted(batch.pk for batch in batches)
    current = active_standard(compound_code)
    if current is not None and not force and sorted(current.source_batch_ids or []) == source_ids:
        return current

    step = iot_config.curve_step_s()
    by_index: Dict[int, List[float]] = {}
    for batch in batches:
        for index, value in enumerate(batch.curve or []):
            by_index.setdefault(index, []).append(float(value))
    # Keep the stretch most batches reach, so a few long batches don't define the tail alone.
    needed = max(3, math.ceil(len(batches) / 2))
    points = []
    for index in sorted(by_index):
        values = by_index[index]
        if len(values) < needed:
            continue
        median, low, high = band(values)
        points.append({"t": index * step, "median": round(median, 1), "lower": round(low, 1), "upper": round(high, 1)})
    stats: Dict[str, Any] = {}
    for name in STAT_FIELDS:
        stat = _stat(name, (getattr(batch, name) for batch in batches))
        if stat is not None:
            stats[name] = stat

    with transaction.atomic():
        latest = (
            StandardCurve.objects.select_for_update().filter(compound_code=compound_code).order_by("-version").first()
        )
        StandardCurve.objects.filter(compound_code=compound_code, is_active=True).update(is_active=False)
        standard = StandardCurve.objects.create(
            compound_code=compound_code,
            version=(latest.version if latest else 0) + 1,
            built_from_n=len(batches),
            source_batch_ids=source_ids,
            step_s=step,
            points=points,
            stats=stats,
            is_active=True,
            created_by=user,
        )
    # Batches that were waiting for a standard get compared now.
    from . import iot_compare

    for batch in DetectedBatch.objects.filter(compound_code=compound_code, is_open=False).filter(
        result__in=["", DetectedBatch.RESULT_NO_STANDARD]
    ):
        iot_compare.compare_batch(batch)
    return standard


def compounds_seen() -> List[str]:
    return sorted(
        code for code in DetectedBatch.objects.exclude(compound_code="").values_list("compound_code", flat=True).distinct()
    )


def overview() -> List[Dict[str, Any]]:
    """Every compound with batches: normal count, readiness and its active standard."""
    minimum = iot_config.standard_min_batches()
    rows = []
    for compound in compounds_seen():
        count = normal_batches(compound).count()
        rows.append({
            "compound": compound,
            "count": count,
            "needed": minimum,
            "ready": count >= minimum,
            "standard": active_standard(compound),
        })
    return rows


def rebuild_all(*, user=None) -> List[Dict[str, Any]]:
    """Rebuild every compound with enough batches (the weekly cron). Unchanged sets keep their version."""
    report = []
    for row in overview():
        if not row["ready"]:
            report.append({"compound": row["compound"], "built": False, "batches": row["count"]})
            continue
        before = row["standard"]
        standard = build_standard(row["compound"], user=user)
        report.append({"compound": row["compound"], "built": before is None or before.pk != standard.pk,
                       "version": standard.version, "batches": row["count"]})
    return report
