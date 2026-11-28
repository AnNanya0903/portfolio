"""Machine-state segmentation of a day's current trace, and the DetectedBatch records it produces.

Rules, on a centred 30 s rolling mean of avg_current (all thresholds in iot_config):

    START  the loading spike: the mean rises above 90 A after having been below 60 A. The batch starts at the
           last point below 60 A before that rise. This starts a new batch even if the mill never went idle.
    END    just before the next loading spike (or where the mill began idling right before it), or when the
           mean stays below 45 A for 120 s (idle / off). Shorter dips stay inside the batch.
    A spike less than MIN_BATCH_SECONDS (8 min) after a start belongs to the same loading. Shorter stretches
    are not batches. Work already running when the data begins is "partial - start unknown".

Telemetry gaps become "no_data" segments. Consecutive segments cover the data without gaps.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import iot_config, iot_samples
from .models import IST_TIMEZONE, DetectedBatch, MachineSegment

logger = logging.getLogger(__name__)

OFF, IDLE, BATCH, NO_DATA = (
    MachineSegment.STATE_OFF,
    MachineSegment.STATE_IDLE,
    MachineSegment.STATE_BATCH,
    MachineSegment.STATE_NO_DATA,
)


def ms_to_datetime(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=dt_timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def day_bounds_ms(day: date) -> Tuple[int, int]:
    """[start, end) of an Asia/Kolkata calendar day in epoch ms."""
    start = datetime.combine(day, dt_time.min, tzinfo=IST_TIMEZONE)
    return datetime_to_ms(start), datetime_to_ms(start + timedelta(days=1))


@dataclass
class Segment:
    state: str
    start_ts: int
    end_ts: int
    is_open: bool = False
    is_partial: bool = False

    @property
    def duration_s(self) -> int:
        return max(int(round((self.end_ts - self.start_ts) / 1000)), 0)


# Segmentation ---------------------------------------------------------------------------------------------
def rolling_mean(points: Sequence[Tuple[int, float]], window_s: int) -> List[float]:
    """Centred rolling mean over +/- window_s/2 seconds (two pointers, O(n))."""
    half = window_s * 500
    means: List[float] = []
    lo = hi = 0
    total = 0.0
    for ts, _value in points:
        while hi < len(points) and points[hi][0] <= ts + half:
            total += points[hi][1]
            hi += 1
        while points[lo][0] < ts - half:
            total -= points[lo][1]
            lo += 1
        means.append(total / (hi - lo))
    return means


def _batch_spans(points: Sequence[Tuple[int, float]], means: Sequence[float],
                 machine_id: str) -> List[Tuple[int, int, bool, bool]]:
    """(start_index, end_index_exclusive, reached_block_end, partial) for each batch in one gap-free block."""
    idle = iot_config.batch_above_a()
    low = iot_config.spike_arm_below_a()
    spike = iot_config.load_threshold_a(machine_id)
    idle_ms = iot_config.batch_end_sustain_s() * 1000
    min_ms = iot_config.min_batch_seconds() * 1000

    spans: List[Tuple[int, int, bool, bool]] = []
    start: Optional[int] = 0 if means[0] >= idle else None  # already working: the start was not seen
    partial = start is not None
    armed = means[0] < low
    last_low: Optional[int] = None
    below_since: Optional[int] = None
    idle_from = idle_to = None  # the latest stretch below the idle level
    for i, mean in enumerate(means):
        if mean < idle:
            if idle_to != i - 1:
                idle_from = i
            idle_to = i
        if mean < low:
            armed, last_low = True, i
        if armed and mean > spike:
            armed = False
            rise = last_low if last_low is not None else i
            if start is None:
                start, partial, below_since = rise, False, None
            elif partial or points[rise][0] - points[start][0] >= min_ms:
                end = rise
                if idle_to is not None and idle_from > start and points[rise][0] - points[idle_to][0] <= 30_000:
                    end = idle_from  # the mill idled just before reloading
                spans.append((start, end, False, partial))
                start, partial, below_since = rise, False, None
        if start is None:
            continue
        if mean >= idle:
            below_since = None
            continue
        below_since = i if below_since is None else below_since
        if points[i][0] - points[below_since][0] >= idle_ms:
            spans.append((start, below_since, False, partial))
            start, partial, below_since = None, False, None
    if start is not None:
        spans.append((start, len(points), True, partial))
    return spans


def _absorb_short_runs(runs: List[List[Any]], min_ms: int) -> List[List[Any]]:
    """runs: [state, start_ts, end_ts]. Runs shorter than min_ms join the run before (or after) them."""
    merged: List[List[Any]] = []
    for run in runs:
        if merged and merged[-1][0] == run[0]:
            merged[-1][2] = run[2]
        elif merged and run[2] - run[1] < min_ms:
            merged[-1][2] = run[2]
        else:
            merged.append(list(run))
    if len(merged) > 1 and merged[0][2] - merged[0][1] < min_ms:
        merged[1][1] = merged[0][1]
        merged.pop(0)
    # Absorbing can leave equal neighbours; join them.
    result: List[List[Any]] = []
    for run in merged:
        if result and result[-1][0] == run[0]:
            result[-1][2] = run[2]
        else:
            result.append(run)
    return result


def _idle_off_runs(points, means, start: int, end: int, end_ts: int) -> List[List[Any]]:
    off_below = iot_config.off_below_a()
    runs: List[List[Any]] = []
    for i in range(start, end):
        state = OFF if means[i] < off_below else IDLE
        next_ts = points[i + 1][0] if i + 1 < end else end_ts
        if runs and runs[-1][0] == state:
            runs[-1][2] = next_ts
        else:
            runs.append([state, points[i][0], next_ts])
    return _absorb_short_runs(runs, iot_config.min_state_s() * 1000)


def segment_samples(samples: Sequence[Dict[str, Any]], *, machine_id: str = "",
                    open_end: bool = False) -> List[Segment]:
    """Segments for samples (dicts with ts_ms and avg_current, oldest first).

    open_end marks the last segment as still in progress (the caller knows the data is live).
    """
    points = [(s["ts_ms"], float(s["avg_current"])) for s in samples if s.get("avg_current") is not None]
    if not points:
        return []
    gap_ms = iot_config.max_gap_s() * 1000
    min_batch_ms = iot_config.min_batch_seconds() * 1000

    blocks: List[Tuple[int, int]] = []
    block_start = 0
    for i in range(1, len(points)):
        if points[i][0] - points[i - 1][0] > gap_ms:
            blocks.append((block_start, i))
            block_start = i
    blocks.append((block_start, len(points)))

    segments: List[Segment] = []
    for block_index, (b_start, b_end) in enumerate(blocks):
        block = points[b_start:b_end]
        means = rolling_mean(block, iot_config.rolling_window_s())
        block_end_ts = block[-1][0]
        is_last_block = block_index == len(blocks) - 1
        if segments and segments[-1].end_ts < block[0][0]:
            segments.append(Segment(NO_DATA, segments[-1].end_ts, block[0][0]))

        cursor = 0
        for span_start, span_end, reached_end, partial in _batch_spans(block, means, machine_id):
            batch_start_ts = block[span_start][0]
            batch_end_ts = block[span_end][0] if span_end < len(block) else block_end_ts
            still_running = reached_end and is_last_block and open_end
            shortest = iot_config.batch_end_sustain_s() * 1000 if partial else min_batch_ms
            if batch_end_ts - batch_start_ts < shortest and not still_running:
                continue  # too short to be a batch: left to the idle/off pass below
            if span_start > cursor:
                for state, s_ts, e_ts in _idle_off_runs(block, means, cursor, span_start, batch_start_ts):
                    segments.append(Segment(state, s_ts, e_ts))
            segments.append(Segment(BATCH, batch_start_ts, batch_end_ts, is_open=still_running, is_partial=partial))
            cursor = span_end
        if cursor < len(block):
            runs = _idle_off_runs(block, means, cursor, len(block), block_end_ts)
            for index, (state, s_ts, e_ts) in enumerate(runs):
                segments.append(Segment(state, s_ts, e_ts, is_open=open_end and is_last_block and index == len(runs) - 1))

    # Short batches skipped above leave idle/off runs next to each other; join equal neighbours.
    joined: List[Segment] = []
    for segment in segments:
        if joined and joined[-1].state == segment.state and segment.state != BATCH and joined[-1].end_ts >= segment.start_ts:
            joined[-1].end_ts = segment.end_ts
            joined[-1].is_open = segment.is_open
        else:
            joined.append(segment)
    return [segment for segment in joined if segment.end_ts > segment.start_ts or segment.is_open]


# Batch numbers --------------------------------------------------------------------------------------------
def find_load_ts(samples: Sequence[Dict[str, Any]], start_ts: int, end_ts: int,
                 machine_id: str = "") -> Tuple[Optional[int], Optional[float]]:
    """First moment the 30 s mean passes the machine's load threshold (default 90 A), else the fallback (70 A)."""
    points = [(s["ts_ms"], float(s["avg_current"])) for s in samples
              if s.get("avg_current") is not None and start_ts <= s["ts_ms"] < end_ts]
    if not points:
        return None, None
    means = rolling_mean(points, iot_config.rolling_window_s())
    for threshold in (iot_config.load_threshold_a(machine_id), iot_config.load_fallback_a()):
        for (ts, _value), mean in zip(points, means):
            if mean > threshold:
                return ts, threshold
    return None, None


def resample_curve(samples: Sequence[Dict[str, Any]], origin_ts: int, end_ts: int, step_s: int) -> List[float]:
    """avg_current every step_s seconds from origin_ts up to end_ts, linearly interpolated between samples."""
    points = [(s["ts_ms"], s["avg_current"]) for s in samples
              if s.get("avg_current") is not None and origin_ts <= s["ts_ms"] <= end_ts]
    if len(points) < 2:
        return [round(points[0][1], 1)] if points else []
    curve: List[float] = []
    index = 0
    target = origin_ts
    while target <= points[-1][0]:
        while index + 1 < len(points) and points[index + 1][0] < target:
            index += 1
        ts_a, value_a = points[index]
        ts_b, value_b = points[min(index + 1, len(points) - 1)]
        if target <= ts_a or ts_b == ts_a:
            value = value_a
        else:
            value = value_a + (value_b - value_a) * (target - ts_a) / (ts_b - ts_a)
        curve.append(round(value, 1))
        target += step_s * 1000
    return curve


def batch_numbers(samples: Sequence[Dict[str, Any]], start_ts: int, end_ts: int, machine_id: str = "") -> Dict[str, Any]:
    """Deterministic batch summary: energy_kwh = last t_kwh_import - first one inside [start, end)."""
    window = [s for s in samples if start_ts <= s["ts_ms"] < end_ts]
    currents = [(s["ts_ms"], s["avg_current"]) for s in window if s.get("avg_current") is not None]
    kwh = [s["t_kwh_import"] for s in window if s.get("t_kwh_import") is not None]
    numbers: Dict[str, Any] = {
        "duration_s": max(int(round((end_ts - start_ts) / 1000)), 0),
        "energy_kwh": None,
        "avg_current": None,
        "peak_current": None,
        "time_to_peak_s": None,
    }
    if len(kwh) >= 2 and kwh[-1] >= kwh[0]:  # a drop means the meter was reset; report nothing
        numbers["energy_kwh"] = round(kwh[-1] - kwh[0], 3)
    numbers["steady_current"] = None
    numbers["dips"] = 0
    if currents:
        numbers["avg_current"] = round(statistics.fmean(value for _ts, value in currents), 2)
        # The loading peak is read on the 30 s mean, so one noisy sample doesn't set it.
        means = rolling_mean(currents, iot_config.rolling_window_s())
        peak_index = max(range(len(means)), key=lambda index: means[index])
        numbers["peak_current"] = round(means[peak_index], 1)
        numbers["time_to_peak_s"] = max(int(round((currents[peak_index][0] - start_ts) / 1000)), 0)
        low = start_ts + (end_ts - start_ts) * iot_config.steady_from_fraction()
        high = start_ts + (end_ts - start_ts) * iot_config.steady_to_fraction()
        steady = [value for ts, value in currents if low <= ts <= high]
        if steady:
            numbers["steady_current"] = round(statistics.fmean(steady), 1)
    load_ts, threshold = find_load_ts(window, start_ts, end_ts, machine_id)
    step = iot_config.curve_step_s()
    numbers["load_ts"] = load_ts
    numbers["load_threshold_a"] = threshold
    numbers["curve"] = resample_curve(window, load_ts if load_ts is not None else start_ts, end_ts, step)
    numbers["curve_step_s"] = step
    numbers["dips"] = count_dips(numbers["curve"], iot_config.dip_below_a())
    return numbers


def count_dips(curve: Sequence[float], below: float) -> int:
    """How many times the curve drops below `below` amps (each stretch counts once)."""
    dips = 0
    under = False
    for value in curve:
        if value < below and not under:
            dips += 1
        under = value < below
    return dips


# Detection run ----------------------------------------------------------------------------------------------
def _apply_numbers(batch: DetectedBatch, numbers: Dict[str, Any]) -> None:
    batch.duration_s = numbers["duration_s"]
    batch.energy_kwh = numbers["energy_kwh"]
    batch.avg_current = numbers["avg_current"]
    batch.peak_current = numbers["peak_current"]
    batch.time_to_peak_s = numbers["time_to_peak_s"]
    batch.load_at = ms_to_datetime(numbers["load_ts"]) if numbers["load_ts"] is not None else None
    batch.load_threshold_a = numbers["load_threshold_a"]
    batch.curve = numbers["curve"]
    batch.curve_step_s = numbers["curve_step_s"]
    batch.steady_current = numbers["steady_current"]
    batch.dips = numbers["dips"]


def detect_range(machine_id: str, start_ts: int, end_ts: int, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Segment [start_ts, end_ts), replace its MachineSegment rows and create/update/remove DetectedBatches.

    Safe to repeat: a batch keeps its identity (and its exclusion) as long as it overlaps its old self.
    """
    now_ms = int(timezone.now().timestamp() * 1000) if now_ms is None else now_ms
    samples = iot_samples.samples_between(machine_id, start_ts, end_ts - 1)
    live = bool(samples) and end_ts > now_ms and now_ms - samples[-1]["ts_ms"] <= iot_config.max_gap_s() * 1000
    segments = segment_samples(samples, machine_id=machine_id, open_end=live)

    start_dt, end_dt = ms_to_datetime(start_ts), ms_to_datetime(end_ts)
    with transaction.atomic():
        existing = list(
            DetectedBatch.objects.select_for_update()
            .filter(machine_id=machine_id, start_at__gte=start_dt, start_at__lt=end_dt)
            .order_by("start_at")
        )
        used: set[int] = set()
        kept: List[Tuple[Segment, DetectedBatch]] = []
        for segment in segments:
            if segment.state != BATCH:
                continue
            best, best_overlap = None, 0
            for batch in existing:
                if batch.pk in used:
                    continue
                overlap = min(segment.end_ts, datetime_to_ms(batch.end_at)) - max(segment.start_ts, datetime_to_ms(batch.start_at))
                if overlap > best_overlap:
                    best, best_overlap = batch, overlap
            batch = best or DetectedBatch(machine_id=machine_id)
            batch.start_at = ms_to_datetime(segment.start_ts)
            batch.end_at = ms_to_datetime(segment.end_ts)
            batch.is_open = segment.is_open
            batch.is_partial = segment.is_partial
            _apply_numbers(batch, batch_numbers(samples, segment.start_ts, segment.end_ts, machine_id))
            batch.save()
            used.add(batch.pk)
            kept.append((segment, batch))
        removed = [batch.pk for batch in existing if batch.pk not in used]
        DetectedBatch.objects.filter(pk__in=removed).delete()

        MachineSegment.objects.filter(machine_id=machine_id, start_at__gte=start_dt, start_at__lt=end_dt).delete()
        by_segment = {id(segment): batch for segment, batch in kept}
        MachineSegment.objects.bulk_create([
            MachineSegment(
                machine_id=machine_id,
                state=segment.state,
                start_at=ms_to_datetime(segment.start_ts),
                end_at=ms_to_datetime(segment.end_ts),
                is_open=segment.is_open,
                detected_batch=by_segment.get(id(segment)),
            )
            for segment in segments
        ])

    from . import iot_autolog, iot_compare

    try:  # logging and analysis must never lose the detection itself
        iot_autolog.sync_logs(machine_id, start_dt, end_dt)
    except Exception:
        logger.exception("IoT auto-log failed for %s", machine_id)
    for _segment, batch in kept:
        try:
            batch.refresh_from_db()
            iot_compare.compare_batch(batch)
        except Exception:
            logger.exception("IoT batch analysis failed for %s", batch.pk)
    return {
        "machineId": machine_id,
        "segments": len(segments),
        "batches": len(kept),
        "removed": len(removed),
    }


def detect_day(machine_id: str, day: date, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    start_ts, end_ts = day_bounds_ms(day)
    return {"date": day.isoformat(), **detect_range(machine_id, start_ts, end_ts, now_ms=now_ms)}


def today_ist() -> date:
    return timezone.now().astimezone(IST_TIMEZONE).date()


def detect_recent(machine_id: str) -> List[Dict[str, Any]]:
    """Today, plus yesterday in the first hour after midnight so a late batch gets closed."""
    now = timezone.now().astimezone(IST_TIMEZONE)
    days = [now.date()]
    if now.hour < 1:
        days.insert(0, now.date() - timedelta(days=1))
    return [detect_day(machine_id, day) for day in days]


def detect_recent_throttled(machine_id: str) -> Optional[List[Dict[str, Any]]]:
    """detect_recent at most every IOT_DETECT_THROTTLE_S per machine. Never raises."""
    if not machine_id or not cache.add(f"compounding:iot:detect_lock:{machine_id}", 1,
                                       timeout=max(iot_config.detect_throttle_s(), 1)):
        return None
    try:
        return detect_recent(machine_id)
    except Exception:
        logger.exception("IoT detection failed for %s", machine_id)
        return None
