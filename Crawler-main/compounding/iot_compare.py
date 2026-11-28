"""Compare a detected batch with its compound's standard and say, in plain words, how it varied.

    match_pct   share of the batch's curve (from load_ts) inside the standard band
    deviations  duration, energy, loading peak, time to peak, steady current vs the standard median
    result      OK:              match_pct >= 85 and every value inside its allowed range
                Variation:       match_pct 70-85, or any value outside its range
                Major variation: match_pct < 70, or energy / duration off by more than 20 %
    reasons     e.g. "Mixing 4 min longer than standard", "Energy +18 %", "Loading peak 2 min late",
                "Steady current 8 A low", "Outside band 6-9 min", "Looks like AG-FRC 70, check compound selection"

All of this is arithmetic on stored numbers; the AI summary only puts it into words.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import iot_config, iot_standards
from .models import DetectedBatch, StandardCurve


def _band_flags(curve: Sequence[float], curve_step_s: int, standard: StandardCurve) -> List[Tuple[int, bool]]:
    """(t seconds, inside the band?) for every standard point the batch's curve reaches."""
    flags = []
    for point in standard.points:
        index = point["t"] // max(curve_step_s, 1)
        if index >= len(curve):
            break
        flags.append((point["t"], point["lower"] <= curve[index] <= point["upper"]))
    return flags


def match_pct(curve: Sequence[float], curve_step_s: int, standard: StandardCurve) -> Optional[float]:
    flags = _band_flags(curve, curve_step_s, standard)
    if not flags:
        return None
    return round(100.0 * sum(1 for _t, inside in flags if inside) / len(flags), 1)


def out_of_band_sections(flags: Sequence[Tuple[int, bool]], step_s: int, min_s: int = 60) -> List[List[int]]:
    """Stretches outside the band, as [start_s, end_s], ignoring blips shorter than min_s."""
    sections: List[List[int]] = []
    start = None
    for t, inside in list(flags) + [(None, True)]:
        if not inside and start is None:
            start = t
        elif inside and start is not None:
            end = (t if t is not None else flags[-1][0] + step_s)
            if end - start >= min_s:
                sections.append([start, end])
            start = None
    return sections


def _minutes(seconds: float) -> str:
    """'40 s' under a minute, else whole or half minutes: '2 min', '4.5 min'."""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{int(round(seconds))} s"
    value = round(seconds / 60 * 2) / 2
    return f"{value:g} min"


def _section_text(section: Sequence[int]) -> str:
    return f"Outside band {math.floor(section[0] / 60)}–{max(math.ceil(section[1] / 60), math.floor(section[0] / 60) + 1)} min"


def _outside(value: Optional[float], stat: Optional[Dict[str, float]]) -> bool:
    return value is not None and stat is not None and not (stat["low"] <= value <= stat["high"])


def reset(batch: DetectedBatch) -> None:
    batch.standard = None
    batch.match_pct = None
    batch.duration_dev_s = batch.duration_dev_pct = batch.energy_dev_pct = None
    batch.peak_dev_a = batch.peak_time_dev_s = batch.steady_dev_a = None
    batch.reasons = []
    batch.out_of_band = []
    batch.looks_like = ""


def evaluate(batch: DetectedBatch, standard: StandardCurve) -> None:
    """Fill the comparison fields of `batch` against `standard` (does not save)."""
    stats = standard.stats or {}
    batch.standard = standard
    flags = _band_flags(batch.curve or [], batch.curve_step_s, standard)
    batch.match_pct = round(100.0 * sum(1 for _t, inside in flags if inside) / len(flags), 1) if flags else 0.0
    sections = out_of_band_sections(flags, standard.step_s)
    batch.out_of_band = sections

    def median(name: str) -> Optional[float]:
        return (stats.get(name) or {}).get("median")

    out_of_range: List[Tuple[float, str]] = []  # (severity, reason)
    if median("duration_s"):
        batch.duration_dev_s = int(round(batch.duration_s - median("duration_s")))
        batch.duration_dev_pct = round(batch.duration_dev_s / median("duration_s") * 100, 1)
        if _outside(batch.duration_s, stats.get("duration_s")):
            word = "longer" if batch.duration_dev_s > 0 else "shorter"
            out_of_range.append((abs(batch.duration_dev_pct) / 10,
                                 f"Mixing {_minutes(batch.duration_dev_s)} {word} than standard"))
    if batch.energy_kwh is not None and median("energy_kwh"):
        batch.energy_dev_pct = round((batch.energy_kwh - median("energy_kwh")) / median("energy_kwh") * 100, 1)
        if _outside(batch.energy_kwh, stats.get("energy_kwh")):
            out_of_range.append((abs(batch.energy_dev_pct) / 10, f"Energy {batch.energy_dev_pct:+.0f} %"))
    if batch.time_to_peak_s is not None and median("time_to_peak_s") is not None:
        batch.peak_time_dev_s = int(round(batch.time_to_peak_s - median("time_to_peak_s")))
        if _outside(batch.time_to_peak_s, stats.get("time_to_peak_s")):
            word = "late" if batch.peak_time_dev_s > 0 else "early"
            out_of_range.append((abs(batch.peak_time_dev_s) / 60,
                                 f"Loading peak {_minutes(batch.peak_time_dev_s)} {word}"))
    if batch.peak_current is not None and median("peak_current") is not None:
        batch.peak_dev_a = round(batch.peak_current - median("peak_current"), 1)
        if _outside(batch.peak_current, stats.get("peak_current")):
            word = "higher" if batch.peak_dev_a > 0 else "lower"
            out_of_range.append((abs(batch.peak_dev_a) / 10, f"Loading peak {abs(batch.peak_dev_a):.0f} A {word}"))
    if batch.steady_current is not None and median("steady_current") is not None:
        batch.steady_dev_a = round(batch.steady_current - median("steady_current"), 1)
        if _outside(batch.steady_current, stats.get("steady_current")):
            word = "high" if batch.steady_dev_a > 0 else "low"
            out_of_range.append((abs(batch.steady_dev_a) / 4, f"Steady current {abs(batch.steady_dev_a):.0f} A {word}"))

    major_dev = iot_config.major_deviation_pct()
    major = (
        batch.match_pct < iot_config.major_match_pct()
        or abs(batch.energy_dev_pct or 0) > major_dev
        or abs(batch.duration_dev_pct or 0) > major_dev
    )
    if major:
        batch.result = DetectedBatch.RESULT_MAJOR
    elif batch.match_pct < iot_config.ok_match_pct() or out_of_range:
        batch.result = DetectedBatch.RESULT_VARIATION
    else:
        batch.result = DetectedBatch.RESULT_OK

    reasons = [text for _severity, text in sorted(out_of_range, key=lambda item: item[0], reverse=True)]
    longest = sorted(sections, key=lambda section: section[1] - section[0], reverse=True)[:2]
    reasons += [_section_text(section) for section in sorted(longest)]
    batch.reasons = reasons


def pattern_check(batch: DetectedBatch, own_match: Optional[float]) -> str:
    """Another compound whose standard fits this batch IOT_LOOKS_LIKE_MARGIN_PCT points better, if any."""
    margin = iot_config.looks_like_margin_pct()
    best, best_match = "", None
    for standard in iot_standards.active_standards():
        if standard.compound_code == batch.compound_code:
            continue
        other = match_pct(batch.curve or [], batch.curve_step_s, standard)
        if other is None:
            continue
        if best_match is None or other > best_match:
            best, best_match = standard.compound_code, other
    if best and best_match is not None and best_match >= iot_config.ok_match_pct() * 0.9 \
            and best_match - (own_match or 0.0) >= margin:
        return best
    return ""


COMPARE_FIELDS = [
    "standard", "match_pct", "duration_dev_s", "duration_dev_pct", "energy_dev_pct", "peak_dev_a",
    "peak_time_dev_s", "steady_dev_a", "result", "reasons", "out_of_band", "looks_like", "updated_at",
]


def compare_batch(batch: DetectedBatch, *, save: bool = True) -> DetectedBatch:
    reset(batch)
    standard = iot_standards.active_standard(batch.compound_code)
    if batch.is_open or not batch.curve:
        batch.result = ""
    elif standard is None:
        batch.result = DetectedBatch.RESULT_NO_STANDARD
        if batch.compound_code:
            count, needed = iot_standards.readiness(batch.compound_code)
            batch.reasons = [f"Standard not ready ({min(count, needed)}/{needed})"]
    else:
        evaluate(batch, standard)
    if not batch.is_open and batch.curve:
        looks_like = pattern_check(batch, batch.match_pct)
        if looks_like:
            batch.looks_like = looks_like
            batch.reasons = batch.reasons + [f"Looks like {looks_like}, check compound selection"]
    if save:
        batch.save(update_fields=COMPARE_FIELDS)
    return batch
