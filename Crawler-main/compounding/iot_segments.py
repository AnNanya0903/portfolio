"""Machine-state segmentation of a day's current trace, and the DetectedBatch records it produces.

The state machine runs on a centred rolling mean (IOT_SEG_ROLLING_S, default 30 s) of avg_current:

    OFF   rolling mean < IOT_SEG_OFF_BELOW_A (5 A)
    IDLE  between OFF and IOT_SEG_BATCH_ABOVE_A (45 A) - the rolls turning empty at ~35-40 A
    BATCH rolling mean >= 45 A for IOT_SEG_BATCH_START_SUSTAIN_S (60 s); it lasts until the mean stays below
          45 A for IOT_SEG_BATCH_END_SUSTAIN_S (90 s), so shorter dips (cutting/sheeting) stay inside it.
          One exception: a dip of at least IOT_SEG_RELOAD_DIP_S (40 s) followed by a new loading spike starts
          a new batch, which separates back-to-back batches with only a minute between them.

Batch stretches shorter than MIN_BATCH_SECONDS (480 s) count as IDLE. Telemetry gaps longer than
IOT_SEG_MAX_GAP_S become "no_data" segments. Consecutive segments cover the data without gaps.
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


def _batch_spans(points: Sequence[Tuple[int, float]], means: Sequence[float], machine_id: str) -> List[Tuple[int, int, bool]]:
    """(start_index, end_index_exclusive, reached_block_end) for each batch stretch in one gap-free block."""
    above = iot_config.batch_above_a()
    start_ms = iot_config.batch_start_sustain_s() * 1000
    end_ms = iot_config.batch_end_sustain_s() * 1000
    reload_dip_ms = iot_config.reload_dip_s() * 1000
    reload_within_ms = iot_config.reload_within_s() * 1000
    min_batch_ms = iot_config.min_batch_seconds() * 1000
    load_threshold = iot_config.load_threshold_a(machine_id)

    spans: List[Tuple[int, int, bool]] = []
    in_batch = False
    batch_start = above_since = below_since = None
    n = len(points)

    def spike_follows(index: int) -> bool:
        limit = points[index][0] + reload_within_ms
        for j in range(index, n):
            if points[j][0] > limit:
                return False
            if points[j][1] > load_threshold:
                return True
        return False

    for i in range(n):
        ts = points[i][0]
        mean = means[i]
        if not in_batch:
            if mean >= above:
                above_since = i if above_since is None else above_since
                if ts - points[above_since][0] >= start_ms:
                    in_batch, batch_start, below_since = True, above_since, None
            else:
                above_since = None
            continue
        if mean < above:
            below_since = i if below_since is None else below_since
            if ts - points[below_since][0] >= end_ms:
                spans.append((batch_start, below_since, False))
                in_batch, above_since, below_since = False, None, None
        elif below_since is not None:
            dip_ms = ts - points[below_since][0]
            so_far_ms = points[below_since][0] - points[batch_start][0]
            if dip_ms >= reload_dip_ms and so_far_ms >= min_batch_ms and spike_follows(i):
                # The mill was emptied and loaded again: close this batch and start the next one here.
                spans.append((batch_start, below_since, False))
                batch_start = i
            below_since = None
    if in_batch:
        spans.append((batch_start, n, True))
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
        for span_start, span_end, reached_end in _batch_spans(block, means, machine_id):
            batch_start_ts = block[span_start][0]
            batch_end_ts = block[span_end][0] if span_end < len(block) else block_end_ts
            still_running = reached_end and is_last_block and open_end
            if batch_end_ts - batch_start_ts < min_batch_ms and not still_running:
                continue  # too short to be a batch: left to the idle/off pass below
            if span_start > cursor:
                for state, s_ts, e_ts in _idle_off_runs(block, means, cursor, span_start, batch_start_ts):
                    segments.append(Segment(state, s_ts, e_ts))
            segments.append(Segment(BATCH, batch_start_ts, batch_end_ts, is_open=still_running))
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
    """First sample above the machine's load threshold (default 90 A), else above the fallback (70 A)."""
    for threshold in (iot_config.load_threshold_a(machine_id), iot_config.load_fallback_a()):
        for sample in samples:
            if start_ts <= sample["ts_ms"] < end_ts and (sample.get("avg_current") or 0) > threshold:
                return sample["ts_ms"], threshold
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
    if currents:
        numbers["avg_current"] = round(statistics.fmean(value for _ts, value in currents), 2)
        peak_ts, peak = max(currents, key=lambda item: item[1])
        numbers["peak_current"] = round(peak, 2)
        numbers["time_to_peak_s"] = max(int(round((peak_ts - start_ts) / 1000)), 0)
    load_ts, threshold = find_load_ts(window, start_ts, end_ts, machine_id)
    step = iot_config.curve_step_s()
    numbers["load_ts"] = load_ts
    numbers["load_threshold_a"] = threshold
    numbers["curve"] = resample_curve(window, load_ts if load_ts is not None else start_ts, end_ts, step)
    numbers["curve_step_s"] = step
    return numbers


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


def _after_batch_saved(batch: DetectedBatch, samples: Sequence[Dict[str, Any]]) -> None:
    """Hook for the analysis steps that follow detection (pattern identification and judging)."""


def detect_range(machine_id: str, start_ts: int, end_ts: int, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Segment [start_ts, end_ts), replace its MachineSegment rows and create/update/remove DetectedBatches.

    Safe to repeat: a batch keeps its identity (and any manual tag) as long as it overlaps its old self.
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

    for _segment, batch in kept:
        try:
            _after_batch_saved(batch, samples)
        except Exception:  # analysis must never lose the detection itself
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
