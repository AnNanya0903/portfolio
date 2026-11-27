"""Synthetic mixing-mill telemetry shaped like the live data of 27 Nov 2026 (samples every 2 s).

    OFF 0 A; IDLE ~35-40 A flat.
    AG-7020 (~15-16 min, ~10.2 kWh): loading spike 100-115 A for the first ~2 min, steady ~73 A for ~6 min,
        choppy 45-80 A (cutting/sheeting), ends near 45-50 A.
    AG-FRC 70 (~20 min): no big spike (peak ~77 A), long steady ~68-70 A, choppy end.

Energy: t_kwh_import grows with the current (the same constant all day), scaled so an AG-7020 batch uses
about 10.25 kWh.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Dict, List, Sequence, Tuple

from compounding.models import IST_TIMEZONE

DAY = date(2026, 11, 27)
SOURCE_MACHINE = "extrusion-mill-1"


def ist_ms(hhmm: str, day: date = DAY) -> int:
    """'HH:MM' or 'HH:MM:SS' in IST on `day`, as epoch ms."""
    hour, minute, *rest = (int(part) for part in hhmm.split(":"))
    second = rest[0] if rest else 0
    return int(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST_TIMEZONE).timestamp() * 1000)


def _wiggle(t: int, amplitude: float) -> float:
    """Deterministic noise in [-amplitude, +amplitude]."""
    return ((t * 7919) % 101 / 100.0 * 2 - 1) * amplitude


def off(t: int, duration: int) -> float:
    return 0.0


def idle(t: int, duration: int) -> float:
    return 37.0 + _wiggle(t, 1.5)


def ag7020(t: int, duration: int) -> float:
    if t < 20:
        return 45.0 + t * 2.6
    if t < 140:
        return 100.0 + (t // 2 * 7) % 16  # 100-115 A
    if t < 500:
        return 73.0 + _wiggle(t, 1.5)
    if t < duration - 60:
        return 62.0 + (18.0 if (t // 20) % 2 else -17.0) + _wiggle(t, 2)  # choppy 45-80
    return 47.5 + _wiggle(t, 2)


def agfrc70(t: int, duration: int) -> float:
    if t < 60:
        return 40.0 + t * 0.55
    if t < 120:
        return 77.0 - (t - 60) * 0.12
    if t < duration - 300:
        return 69.0 + _wiggle(t, 1.0)
    if t < duration - 60:
        return 60.0 + (15.0 if (t // 25) % 2 else -14.0) + _wiggle(t, 2)
    return 47.5 + _wiggle(t, 2)


Profile = Callable[[int, int], float]

# (profile, start, end) in IST on 27 Nov 2026, matching the acceptance timeline.
DAY_PLAN: List[Tuple[Profile, str, str]] = [
    (off, "09:30", "09:50"),
    (idle, "09:50", "11:01"),
    (ag7020, "11:01", "11:16:30"),
    (idle, "11:16:30", "11:19"),
    (ag7020, "11:19", "11:34:30"),
    (idle, "11:34:30", "11:41:30"),
    (ag7020, "11:41:30", "11:57"),
    (idle, "11:57", "11:58"),  # only a minute between these two batches
    (ag7020, "11:58", "12:13:30"),
    (idle, "12:13:30", "12:24"),
    (agfrc70, "12:24", "12:44"),
    (idle, "12:44", "12:45"),
    (off, "12:45", "13:30"),
]

# kWh per amp-second: makes a 15.5 min AG-7020 batch use ~10.25 kWh.
_AG7020_AMP_SECONDS = sum(ag7020(t, 930) * 2 for t in range(0, 930, 2))
KWH_PER_AMP_SECOND = 10.25 / _AG7020_AMP_SECONDS


def build_payloads(plan: Sequence[Tuple[Profile, str, str]] = DAY_PLAN, kwh_start: float = 5000.0,
                   day: date = DAY) -> List[Dict]:
    payloads: List[Dict] = []
    kwh = kwh_start
    for profile, start, end in plan:
        start_ms, end_ms = ist_ms(start, day), ist_ms(end, day)
        duration = (end_ms - start_ms) // 1000
        for t in range(0, duration, 2):
            current = round(max(profile(t, duration), 0.0), 2)
            payloads.append({
                "machineId": SOURCE_MACHINE,
                "ts": start_ms + t * 1000,
                "registers": [],
                "raw": {
                    "avg_current": current,
                    "t_kwh_import": round(kwh, 4),
                    "t_watt": round(current * 560, 1),
                    "ch1_temperature": 40.0,
                    "ch2_temperature": 39.5,
                },
            })
            kwh += current * 2 * KWH_PER_AMP_SECOND
    return payloads


def as_rows(payloads: Sequence[Dict]) -> List[Dict]:
    """Payloads as the stored-sample dicts iot_samples.samples_between returns."""
    return [
        {"ts_ms": item["ts"], "avg_current": item["raw"]["avg_current"], "t_kwh_import": item["raw"]["t_kwh_import"]}
        for item in payloads
    ]


def make_detected_batch(profile: Profile, start: str, end: str, *, day: date = DAY, scale: float = 1.0,
                        tag: str = "", machine_id: str = "mixing-mill-2", analyze: bool = True):
    """A DetectedBatch built straight from a profile (no stored samples), for standards/pattern tests."""
    from compounding import iot_patterns, iot_segments
    from compounding.models import DetectedBatch

    payloads = build_payloads([(profile, start, end)], day=day)
    for item in payloads:
        item["raw"]["avg_current"] = round(item["raw"]["avg_current"] * scale, 2)
    start_ts, end_ts = ist_ms(start, day), ist_ms(end, day)
    numbers = iot_segments.batch_numbers(as_rows(payloads), start_ts, end_ts, machine_id)
    batch = DetectedBatch(
        machine_id=machine_id,
        start_at=iot_segments.ms_to_datetime(start_ts),
        end_at=iot_segments.ms_to_datetime(end_ts),
        tagged_compound=tag,
    )
    iot_segments._apply_numbers(batch, numbers)
    batch.save()
    if analyze:
        iot_patterns.analyze_batch(batch)
    return batch
