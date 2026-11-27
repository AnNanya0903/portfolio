"""IoT batches on the mixing mill: the batch summary, validity rules and automatic batch detection.

Everything here is deterministic arithmetic on stored samples (compounding.IoTSample). The AI summary only
explains these numbers; it never estimates them.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Sequence

from django.db import transaction
from django.utils import timezone

from . import iot_config, iot_samples
from .models import CompoundingIoTRun

logger = logging.getLogger(__name__)


def ms_to_datetime(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=dt_timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


# Batch summary --------------------------------------------------------------------------------------------
def find_load_start(samples: Sequence[Dict[str, Any]], threshold: Optional[float] = None) -> Optional[int]:
    """ts_ms of the first sample above the loading threshold (default IOT_BATCH_LOAD_THRESHOLD_A)."""
    threshold = iot_config.load_threshold_a() if threshold is None else threshold
    for sample in samples:
        current = sample.get("avg_current")
        if current is not None and current > threshold:
            return sample["ts_ms"]
    return None


def _spike_end(samples: Sequence[Dict[str, Any]], load_start_ts: int, threshold: float, settle_ms: int) -> int:
    """The loading spike ends at the first sample at or below the threshold that stays there for settle_ms."""
    candidate: Optional[int] = None
    last_ts = load_start_ts
    for sample in samples:
        ts = sample["ts_ms"]
        if ts < load_start_ts:
            continue
        last_ts = ts
        current = sample.get("avg_current")
        if current is None:
            continue
        if current <= threshold:
            if candidate is None:
                candidate = ts
            if ts - candidate >= settle_ms:
                return candidate
        else:
            candidate = None
    return candidate if candidate is not None else last_ts


def compute_summary(samples: Sequence[Dict[str, Any]], start_ts: int, end_ts: int) -> Dict[str, Any]:
    """Batch numbers from its samples (dicts with ts_ms, avg_current, t_kwh_import; oldest first).

    energy_kwh = last cumulative kWh reading - first one inside the batch. Durations are whole seconds.
    """
    window = [sample for sample in samples if start_ts <= sample["ts_ms"] <= end_ts]
    currents = [(sample["ts_ms"], sample["avg_current"]) for sample in window if sample.get("avg_current") is not None]
    kwh = [sample["t_kwh_import"] for sample in window if sample.get("t_kwh_import") is not None]

    summary: Dict[str, Any] = {
        "duration_s": max(int(round((end_ts - start_ts) / 1000)), 0),
        "energy_kwh": None,
        "avg_current": None,
        "peak_current": None,
        "time_to_peak_s": None,
        "load_start_ts": None,
        "loading_spike_duration_s": None,
        "sample_count": len(window),
    }
    if len(kwh) >= 2:
        energy = kwh[-1] - kwh[0]
        # A negative difference means the meter was reset mid-batch; report nothing rather than nonsense.
        summary["energy_kwh"] = round(energy, 3) if energy >= 0 else None
    if currents:
        values = [value for _ts, value in currents]
        summary["avg_current"] = round(statistics.fmean(values), 2)
        peak_ts, peak = max(currents, key=lambda item: item[1])
        summary["peak_current"] = round(peak, 2)
        summary["time_to_peak_s"] = max(int(round((peak_ts - start_ts) / 1000)), 0)
    threshold = iot_config.load_threshold_a()
    load_start = find_load_start(window, threshold)
    if load_start is not None:
        summary["load_start_ts"] = load_start
        spike_end = _spike_end(window, load_start, threshold, iot_config.spike_settle_s() * 1000)
        summary["loading_spike_duration_s"] = max(int(round((spike_end - load_start) / 1000)), 0)
    return summary


def apply_validity(run: CompoundingIoTRun) -> None:
    """Completed, or invalid when shorter than MIN_BATCH_SECONDS. Invalid batches stay out of statistics."""
    if run.ended_at is None:
        run.status = CompoundingIoTRun.STATUS_RUNNING
        run.invalid_reason = ""
        return
    duration = max(int((run.ended_at - run.started_at).total_seconds()), 0)
    run.duration_s = duration
    minimum = iot_config.min_batch_seconds()
    if duration < minimum:
        run.status = CompoundingIoTRun.STATUS_INVALID
        run.invalid_reason = f"Shorter than {minimum // 60} min ({duration} s)"
    else:
        run.status = CompoundingIoTRun.STATUS_COMPLETED
        run.invalid_reason = ""


SUMMARY_FIELDS = [
    "duration_s",
    "energy_kwh",
    "avg_current",
    "peak_current",
    "time_to_peak_s",
    "load_start_ts",
    "loading_spike_duration_s",
    "status",
    "invalid_reason",
]


def finalize_batch(run: CompoundingIoTRun, *, save: bool = True) -> CompoundingIoTRun:
    """Fill a finished batch's summary from its samples, apply the validity rule, compare with the standard."""
    if run.ended_at is None:
        apply_validity(run)
        if save:
            run.save(update_fields=["status", "invalid_reason", "updated_at"])
        return run
    start_ms, end_ms = datetime_to_ms(run.started_at), datetime_to_ms(run.ended_at)
    samples = iot_samples.samples_between(run.machine_id, start_ms, end_ms) if run.machine_id else []
    summary = compute_summary(samples, start_ms, end_ms)
    run.energy_kwh = summary["energy_kwh"]
    run.avg_current = summary["avg_current"]
    run.peak_current = summary["peak_current"]
    run.time_to_peak_s = summary["time_to_peak_s"]
    run.load_start_ts = ms_to_datetime(summary["load_start_ts"]) if summary["load_start_ts"] else None
    run.loading_spike_duration_s = summary["loading_spike_duration_s"]
    apply_validity(run)
    update_fields = SUMMARY_FIELDS + ["updated_at"]
    if save:
        run.save(update_fields=sorted(set(update_fields)))
    return run
