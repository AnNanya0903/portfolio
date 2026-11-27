"""IoT batches on the mixing mill: the batch summary, validity rules and automatic batch detection.

Everything here is deterministic arithmetic on stored samples (compounding.IoTSample). The AI summary only
explains these numbers; it never estimates them.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Sequence

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import iot_config, iot_curves, iot_samples
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
    update_fields += iot_curves.evaluate_batch(run, samples)
    if save:
        run.save(update_fields=sorted(set(update_fields)))
    return run


# Automatic batch detection --------------------------------------------------------------------------------
@dataclass
class DetectedRun:
    """A stretch of machine work found in the current trace. end_ts is None while it is still running."""

    start_ts: int
    end_ts: Optional[int] = None
    last_ts: Optional[int] = None

    @property
    def finished(self) -> bool:
        return self.end_ts is not None


def detect_runs(
    samples: Sequence[Dict[str, Any]],
    *,
    start_threshold: Optional[float] = None,
    start_sustain_s: Optional[int] = None,
    idle_threshold: Optional[float] = None,
    idle_sustain_s: Optional[int] = None,
    max_gap_s: Optional[int] = None,
) -> List[DetectedRun]:
    """Find machine runs in samples (dicts with ts_ms and avg_current, oldest first).

    A run starts at the first sample of a stretch where avg_current stays above the start threshold for
    start_sustain_s, and ends at the first sample of a stretch below the idle threshold that lasts
    idle_sustain_s. A telemetry gap longer than max_gap_s ends a run at the last sample before the gap.
    """
    start_threshold = iot_config.start_threshold_a() if start_threshold is None else start_threshold
    start_sustain_ms = (iot_config.start_sustain_s() if start_sustain_s is None else start_sustain_s) * 1000
    idle_threshold = iot_config.idle_threshold_a() if idle_threshold is None else idle_threshold
    idle_sustain_ms = (iot_config.idle_sustain_s() if idle_sustain_s is None else idle_sustain_s) * 1000
    max_gap_ms = (iot_config.max_gap_s() if max_gap_s is None else max_gap_s) * 1000

    runs: List[DetectedRun] = []
    running = False
    run_start: Optional[int] = None
    above_since: Optional[int] = None
    idle_since: Optional[int] = None
    prev_ts: Optional[int] = None

    for sample in samples:
        current = sample.get("avg_current")
        if current is None:
            continue
        ts = sample["ts_ms"]
        if prev_ts is not None and ts - prev_ts > max_gap_ms:
            if running:
                runs.append(DetectedRun(run_start, prev_ts, prev_ts))
                running = False
            above_since = idle_since = None
        if not running:
            if current > start_threshold:
                above_since = ts if above_since is None else above_since
                if ts - above_since >= start_sustain_ms:
                    running, run_start, idle_since = True, above_since, None
            else:
                above_since = None
        else:
            if current < idle_threshold:
                idle_since = ts if idle_since is None else idle_since
                if ts - idle_since >= idle_sustain_ms:
                    runs.append(DetectedRun(run_start, idle_since, ts))
                    running, above_since, idle_since = False, None, None
            else:
                idle_since = None
        prev_ts = ts

    if running:
        runs.append(DetectedRun(run_start, None, prev_ts))
    return runs


def _plan_for(moment: datetime):
    from .models import CompoundingPlan, compounding_business_date, next_compounding_cjc_number

    plan_date = compounding_business_date(moment)
    plan = CompoundingPlan.objects.filter(plan_date=plan_date).first()
    if plan is None:
        plan = CompoundingPlan.objects.create(
            plan_date=plan_date, cjc_number=next_compounding_cjc_number(year=plan_date.year)
        )
    return plan


def _manual_batch_for(machine_id: str, run: DetectedRun) -> Optional[CompoundingIoTRun]:
    """The operator's "Start batch" this run belongs to: pressed before the run (or soon after it began),
    not yet matched to a run, and still open or ended after the run started."""
    run_start = ms_to_datetime(run.start_ts)
    window = timedelta(seconds=iot_config.manual_attach_window_s())
    candidates = CompoundingIoTRun.objects.filter(
        machine_id=machine_id,
        detection_source=CompoundingIoTRun.SOURCE_MANUAL,
        run_detected=False,
        started_at__lte=run_start + window,
        started_at__gte=run_start - timedelta(hours=2),
    )
    if run.finished:
        candidates = candidates.filter(Q(ended_at__isnull=True) | Q(ended_at__gte=run_start))
    else:
        # A run still on the machine only joins a batch that is still open.
        candidates = candidates.filter(ended_at__isnull=True)
    return candidates.order_by("-started_at", "-id").first()


def _apply_run(machine_id: str, run: DetectedRun) -> Optional[CompoundingIoTRun]:
    start_dt = ms_to_datetime(run.start_ts)
    end_dt = ms_to_datetime(run.end_ts) if run.finished else None

    existing = CompoundingIoTRun.objects.filter(machine_id=machine_id, run_detected=True, started_at=start_dt).first()
    if existing is not None:
        if run.finished and (existing.ended_at != end_dt or existing.status == CompoundingIoTRun.STATUS_RUNNING):
            existing.ended_at = end_dt
            existing.save(update_fields=["ended_at", "updated_at"])
            finalize_batch(existing)
        return existing

    batch = _manual_batch_for(machine_id, run)
    if batch is not None:
        # The machine's own start/end replace the button presses (created_at keeps when Start was tapped).
        batch.started_at = start_dt
        batch.run_detected = True
        if run.finished:
            batch.ended_at = end_dt
        batch.save(update_fields=["started_at", "ended_at", "run_detected", "updated_at"])
        if run.finished:
            finalize_batch(batch)
        return batch

    batch = CompoundingIoTRun.objects.create(
        plan=_plan_for(start_dt),
        machine_id=machine_id,
        compound_code=CompoundingIoTRun.UNASSIGNED_COMPOUND,
        started_at=start_dt,
        ended_at=end_dt,
        stock_quality="",
        status=CompoundingIoTRun.STATUS_RUNNING,
        detection_source=CompoundingIoTRun.SOURCE_AUTO,
        run_detected=True,
    )
    if run.finished:
        finalize_batch(batch)
    return batch


def _default_scan_start(machine_id: str, now_ms: int) -> int:
    running = (
        CompoundingIoTRun.objects.filter(
            machine_id=machine_id, run_detected=True, status=CompoundingIoTRun.STATUS_RUNNING
        )
        .order_by("started_at")
        .first()
    )
    if running is not None:
        return datetime_to_ms(running.started_at)
    last = (
        CompoundingIoTRun.objects.filter(machine_id=machine_id, run_detected=True, ended_at__isnull=False)
        .order_by("-ended_at")
        .first()
    )
    if last is not None:
        return datetime_to_ms(last.ended_at) + 1
    return now_ms - 6 * 3600 * 1000


def process_machine(
    machine_id: str,
    *,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Run detection over stored samples and create, attach or close batches. Safe to run repeatedly.

    Without a range it continues from the batch still running, or the end of the last detected batch.
    """
    now_ms = int(timezone.now().timestamp() * 1000)
    end_ts = now_ms if end_ts is None else end_ts
    start_ts = _default_scan_start(machine_id, now_ms) if start_ts is None else start_ts
    samples = iot_samples.samples_between(machine_id, start_ts, end_ts)
    runs = detect_runs(samples)

    report: List[Dict[str, Any]] = []
    for run in runs:
        entry: Dict[str, Any] = {"start_ts": run.start_ts, "end_ts": run.end_ts}
        if run.finished:
            summary = compute_summary(samples, run.start_ts, run.end_ts)
            entry.update(energy_kwh=summary["energy_kwh"], duration_s=summary["duration_s"],
                         peak_current=summary["peak_current"], avg_current=summary["avg_current"])
        if not dry_run:
            with transaction.atomic():
                batch = _apply_run(machine_id, run)
            if batch is not None:
                entry.update(batch_id=batch.pk, compound_code=batch.compound_code,
                             source=batch.detection_source, status=batch.status)
        report.append(entry)
    return report


# Live state for the dashboard banner ----------------------------------------------------------------------
def open_logged_batch(machine_id: str) -> Optional[CompoundingIoTRun]:
    """The operator's batch that is still on the machine (one started with "Start batch")."""
    return (
        CompoundingIoTRun.objects.filter(
            machine_id=machine_id, ended_at__isnull=True, detection_source=CompoundingIoTRun.SOURCE_MANUAL
        )
        .select_related("crin")
        .order_by("-started_at", "-id")
        .first()
    )


def machine_state(machine_id: str, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Whether the mill is drawing working current right now and whether a batch has been logged for it."""
    now_ms = int(timezone.now().timestamp() * 1000) if now_ms is None else now_ms
    latest = iot_samples.latest_sample(machine_id)
    fresh = latest is not None and now_ms - latest.ts_ms <= max(iot_config.max_gap_s(), 60) * 1000
    current = latest.avg_current if fresh else None
    running = current is not None and current > iot_config.start_threshold_a()
    logged = open_logged_batch(machine_id)
    return {
        "machineRunning": running,
        "currentA": current,
        "latestTs": latest.ts_ms if latest else None,
        "batchLogged": logged is not None,
        "openBatchId": logged.pk if logged else None,
        "showUnloggedBanner": running and logged is None,
        "startThresholdA": iot_config.start_threshold_a(),
    }
