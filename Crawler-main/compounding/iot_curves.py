"""Standard current curves per process group, and comparing a finished batch with its group's curve.

Curves are aligned on each batch's load_start_ts (the loading spike), not on the button press, and resampled
every IOT_CURVE_STEP_S seconds. The band is mean +/- IOT_CURVE_SIGMA standard deviations.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.db import transaction

from . import iot_config, iot_samples
from .models import CompoundingIoTRun, ProcessGroup, ProcessGroupCompound, StandardCurve


class CurveBuildError(ValueError):
    pass


def group_for_compound(compound_code: str) -> Optional[ProcessGroup]:
    link = (
        ProcessGroupCompound.objects.select_related("process_group")
        .filter(compound_code=(compound_code or "").strip())
        .first()
    )
    return link.process_group if link else None


def active_curve(group: Optional[ProcessGroup]) -> Optional[StandardCurve]:
    if group is None:
        return None
    return group.curves.filter(is_active=True).order_by("-version").first()


def resample(samples: Sequence[Dict[str, Any]], origin_ms: int, end_ms: int, step_s: int) -> List[Tuple[int, float]]:
    """(t seconds after origin, avg_current) every step_s, linearly interpolated between samples."""
    points = [
        (sample["ts_ms"], sample["avg_current"])
        for sample in samples
        if sample.get("avg_current") is not None and origin_ms <= sample["ts_ms"] <= end_ms
    ]
    if len(points) < 2:
        return []
    result: List[Tuple[int, float]] = []
    index = 0
    t = 0
    last_ts = points[-1][0]
    while origin_ms + t * 1000 <= last_ts:
        target = origin_ms + t * 1000
        while index + 1 < len(points) and points[index + 1][0] < target:
            index += 1
        (ts_a, value_a) = points[index]
        (ts_b, value_b) = points[min(index + 1, len(points) - 1)]
        if target <= ts_a or ts_b == ts_a:
            value = value_a
        else:
            value = value_a + (value_b - value_a) * (target - ts_a) / (ts_b - ts_a)
        result.append((t, value))
        t += step_s
    return result


def batch_curve(run: CompoundingIoTRun, samples: Optional[Sequence[Dict[str, Any]]] = None,
                step_s: Optional[int] = None) -> List[Tuple[int, float]]:
    if run.load_start_ts is None or run.ended_at is None:
        return []
    step_s = step_s or iot_config.curve_step_s()
    origin = int(run.load_start_ts.timestamp() * 1000)
    end = int(run.ended_at.timestamp() * 1000)
    if samples is None:
        samples = iot_samples.samples_between(run.machine_id, origin, end)
    return resample(samples, origin, end, step_s)


def curve_source_batches(group: ProcessGroup):
    """Batches a curve may be built from: lab passed, completed (valid length), aligned and finished."""
    return CompoundingIoTRun.objects.filter(
        process_group=group,
        lab_result=CompoundingIoTRun.LAB_PASS,
        status=CompoundingIoTRun.STATUS_COMPLETED,
        load_start_ts__isnull=False,
        ended_at__isnull=False,
    )


def build_standard_curve(group: ProcessGroup, *, user=None, min_batches: Optional[int] = None) -> StandardCurve:
    """Build the next curve version from the group's lab-passed batches and make it the active one."""
    min_batches = iot_config.curve_min_batches() if min_batches is None else min_batches
    step_s = iot_config.curve_step_s()
    sigma = iot_config.curve_sigma()
    batches = list(curve_source_batches(group).order_by("started_at"))
    if len(batches) < min_batches:
        raise CurveBuildError(
            f"{group.name} has {len(batches)} lab-passed, completed batches; at least {min_batches} are needed."
        )

    curves = [batch_curve(run, step_s=step_s) for run in batches]
    curves = [curve for curve in curves if curve]
    if len(curves) < min_batches:
        raise CurveBuildError(
            f"Only {len(curves)} of those batches have stored samples to build from; at least {min_batches} are needed."
        )

    by_t: Dict[int, List[float]] = {}
    for curve in curves:
        for t, value in curve:
            by_t.setdefault(t, []).append(value)
    # Keep the part of the curve most batches reach, so a few long batches don't define the tail alone.
    needed = max(3, math.ceil(len(curves) / 2))
    points: List[Dict[str, Any]] = []
    for t in sorted(by_t):
        values = by_t[t]
        if len(values) < needed:
            continue
        mean = statistics.fmean(values)
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        points.append({
            "t": t,
            "mean": round(mean, 2),
            "upper": round(mean + sigma * spread, 2),
            "lower": round(max(mean - sigma * spread, 0.0), 2),
            "n": len(values),
        })

    energies = [run.energy_kwh for run in batches if run.energy_kwh is not None]
    energy_mean = round(statistics.fmean(energies), 3) if energies else None
    energy_std = round(statistics.stdev(energies), 3) if len(energies) > 1 else None

    with transaction.atomic():
        latest = group.curves.select_for_update().order_by("-version").first()
        version = (latest.version if latest else 0) + 1
        group.curves.filter(is_active=True).update(is_active=False)
        return StandardCurve.objects.create(
            process_group=group,
            version=version,
            built_from_n=len(curves),
            step_s=step_s,
            sigma=sigma,
            points=points,
            energy_mean=energy_mean,
            energy_std=energy_std,
            is_active=True,
            created_by=user,
        )


def compare_with_curve(curve_points: Sequence[Tuple[int, float]], standard: StandardCurve) -> int:
    """Seconds of the batch's curve that fall outside the standard band (where both have data)."""
    band = {point["t"]: point for point in standard.points}
    outside = 0
    for t, value in curve_points:
        point = band.get(t)
        if point is None:
            continue
        if value > point["upper"] or value < point["lower"]:
            outside += 1
    return outside * standard.step_s


CURVE_FIELDS = [
    "process_group",
    "standard_curve",
    "curve_status",
    "out_of_band_seconds",
    "energy_deviation_pct",
    "energy_out_of_band",
]


def evaluate_batch(run: CompoundingIoTRun, samples: Optional[Sequence[Dict[str, Any]]] = None) -> List[str]:
    """Set the batch's process group and its comparison with the active standard. Returns the changed fields."""
    run.process_group = group_for_compound(run.compound_code)
    standard = active_curve(run.process_group)
    run.standard_curve = standard
    run.out_of_band_seconds = None
    run.energy_deviation_pct = None
    run.energy_out_of_band = False
    if standard is None or run.load_start_ts is None or run.status != CompoundingIoTRun.STATUS_COMPLETED:
        run.curve_status = CompoundingIoTRun.CURVE_NO_STANDARD
        return CURVE_FIELDS

    curve_points = batch_curve(run, samples, step_s=standard.step_s)
    run.out_of_band_seconds = compare_with_curve(curve_points, standard)
    run.curve_status = (
        CompoundingIoTRun.CURVE_OUT_OF_BAND
        if run.out_of_band_seconds > iot_config.curve_out_of_band_tolerance_s()
        else CompoundingIoTRun.CURVE_IN_BAND
    )
    if run.energy_kwh is not None and standard.energy_mean:
        run.energy_deviation_pct = round((run.energy_kwh - standard.energy_mean) / standard.energy_mean * 100, 2)
        if standard.energy_std:
            run.energy_out_of_band = abs(run.energy_kwh - standard.energy_mean) > standard.sigma * standard.energy_std
    return CURVE_FIELDS


def standard_band_for_chart(standard: Optional[StandardCurve], load_start_ms: Optional[int]) -> List[Dict[str, Any]]:
    """The band in absolute time for a batch that loaded at load_start_ms: [{ts, mean, upper, lower}]."""
    if standard is None or load_start_ms is None:
        return []
    return [
        {"ts": load_start_ms + point["t"] * 1000, "mean": point["mean"], "upper": point["upper"], "lower": point["lower"]}
        for point in standard.points
    ]
